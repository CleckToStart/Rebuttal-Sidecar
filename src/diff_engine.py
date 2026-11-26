"""Sandboxed TeX flattener, baseline extraction, latexdiff, and configurable LaTeX compiler."""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from config import DEFAULT_COMPILER_SETTINGS
from proc_utils import build_tex_env, miktex_installer_flags, resolve_latexdiff, run_cmd, which_tool
from scanner import WorkspacePaths, find_entry_tex, resolve_manuscript_tree_at_ref

INPUT_CMD_RE = re.compile(r"\\(input|include)\s*\{([^}]+)\}")
TEX_PROGRAM_RE = re.compile(r"^\s*%\s*!TEX\s+(?:TS-)?program\s*=\s*(\w+)", re.IGNORECASE)
XELATEX_PKG_RE = re.compile(r"\\(?:usepackage|documentclass)(?:\[[^\]]*\])?\{[^}]*\b(ctex|xeCJK|fontspec|unicode-math)\b")
FILE_LINE_ERR_RE = re.compile(r"^(.+?):(\d+):\s*(.+)$")

SHARED_SUFFIXES = {".bib", ".bst", ".cls", ".sty"}
BUILD_ARTIFACT_SUFFIXES = {
    ".aux",
    ".log",
    ".out",
    ".toc",
    ".lof",
    ".lot",
    ".fls",
    ".fdb_latexmk",
    ".blg",
    ".bcf",
    ".nav",
    ".snm",
    ".vrb",
    ".tmp",
    ".synctex",
}
ASSET_DIR_NAMES = {"figures", "figure", "figs", "images", "img", "assets", "plots"}


class CompileError(RuntimeError):
    """Raised when baseline extraction, latexdiff, or LaTeX compilation fails."""

    def __init__(
        self,
        message: str,
        errors: list[dict] | None = None,
        log_tail: str = "",
        clean_pdf: str = "",
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.log_tail = log_tail
        self.clean_pdf = clean_pdf


@dataclass(frozen=True)
class SandboxPaths:
    root: Path
    old_dir: Path
    new_dir: Path
    build_clean: Path
    build_diff: Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_comment_tail(line: str) -> tuple[str, str]:
    for idx, ch in enumerate(line):
        if ch == "%" and (idx == 0 or line[idx - 1] != "\\"):
            return line[:idx], line[idx:]
        if ch == "\n":
            break
    return line, ""


def _resolve_tex_target(raw_target: str, base_dir: Path, entry_dir: Path, strip_prefix: str = "") -> Path | None:
    target = raw_target.strip()
    if not target or "\\" in target or "#" in target:
        return None
    raw_variants = [target]
    if strip_prefix and target.startswith(f"{strip_prefix}/"):
        raw_variants.append(target[len(strip_prefix) + 1 :])
    candidates: list[str] = []
    for variant in raw_variants:
        candidates.extend([variant] if Path(variant).suffix else [f"{variant}.tex", variant])
    for parent in (base_dir, entry_dir):
        for rel in candidates:
            candidate = (parent / rel).resolve()
            if candidate.is_file():
                return candidate
    return None


def flatten_tex(
    entry_tex: Path,
    root_dir: Path | None = None,
    visited: set[Path] | None = None,
    strip_prefix: str = "",
) -> str:
    """Recursively expand \\input and \\include directives into a single TeX string."""
    resolved_entry = entry_tex.resolve()
    base_dir = (root_dir or resolved_entry.parent).resolve()
    seen = set(visited or set())
    if resolved_entry in seen:
        return f"% [RebuttalSidecar] Skipped circular include: {resolved_entry.name}\n"
    seen.add(resolved_entry)

    content = resolved_entry.read_text(encoding="utf-8", errors="replace")
    out_lines: list[str] = []

    for line in content.splitlines(keepends=True):
        code, comment = _strip_comment_tail(line)
        matches = list(INPUT_CMD_RE.finditer(code))
        if not matches:
            out_lines.append(line)
            continue

        rebuilt: list[str] = []
        cursor = 0
        for match in matches:
            rebuilt.append(code[cursor : match.start()])
            cmd, raw_target = match.group(1), match.group(2)
            target_path = _resolve_tex_target(raw_target, base_dir, resolved_entry.parent, strip_prefix=strip_prefix)
            if target_path is None:
                rebuilt.append(match.group(0))
            else:
                expanded = flatten_tex(target_path, base_dir, seen, strip_prefix=strip_prefix)
                if cmd == "include":
                    rebuilt.append(f"\\clearpage\n{expanded}\n\\clearpage\n")
                else:
                    rebuilt.append(expanded)
            cursor = match.end()
        rebuilt.append(code[cursor:])
        if comment:
            rebuilt.append(comment)
        out_lines.append("".join(rebuilt))

    return "".join(out_lines)


def detect_tex_engine(
    tex_source: str,
    preferred: str = "auto",
    default_engine: str = "auto",
) -> str:
    """Choose xelatex, pdflatex, or lualatex based on overrides and preamble."""
    if preferred in ("xelatex", "pdflatex", "lualatex"):
        return preferred
    if default_engine in ("xelatex", "pdflatex", "lualatex"):
        return default_engine

    lines = tex_source.splitlines()
    for line in lines[:25]:
        match = TEX_PROGRAM_RE.match(line)
        if match:
            prog = match.group(1).lower()
            if prog in ("xelatex", "pdflatex", "lualatex"):
                return prog

    preamble = "\n".join(lines[:150])
    if XELATEX_PKG_RE.search(preamble):
        return "xelatex"
    return "pdflatex"


def parse_latex_errors(output: str, log_path: Path | None = None) -> list[dict]:
    """Extract structured file:line and TeX bang (!) errors from build output/log."""
    text = output or ""
    if log_path and log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if len(log_text) > len(text):
            text = log_text

    errors: list[dict] = []
    seen: set[str] = set()
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        m = FILE_LINE_ERR_RE.match(line.strip())
        if m:
            msg = m.group(3).strip()
            key = f"{m.group(1)}:{m.group(2)}:{msg}"
            if key not in seen:
                seen.add(key)
                errors.append({"file": m.group(1), "line": int(m.group(2)), "message": msg})
            continue
        if line.startswith("! "):
            context = " | ".join(l.strip() for l in lines[idx : idx + 3] if l.strip())
            if context not in seen:
                seen.add(context)
                errors.append({"file": "", "line": None, "message": context})
    return errors[:20]


def _clear_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        # Remove junctions cleanly without deleting the target directory's contents.
        if child.is_symlink() or (os.name == "nt" and child.is_dir() and child.is_junction()):
            child.rmdir()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _link_or_copy_dir(src: Path, dst: Path) -> None:
    if os.name == "nt":
        res = run_cmd(["cmd", "/c", "mklink", "/J", str(dst), str(src)], timeout=15)
        if res.returncode == 0 and dst.exists():
            return
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _is_build_artifact(path: Path, entry_stem: str = "") -> bool:
    lower = path.name.lower()
    if lower in {"clean.pdf", "diff.pdf"} or (entry_stem and lower == f"{entry_stem.lower()}.pdf"):
        return True
    if lower.endswith((".synctex.gz", ".run.xml")):
        return True
    return path.suffix.lower() in BUILD_ARTIFACT_SUFFIXES


def _sync_manuscript_to_build(src_dir: Path, build_dir: Path, entry_stem: str = "") -> None:
    _clear_dir(build_dir)
    ignore_fn = shutil.ignore_patterns(
        "*.aux", "*.log", "*.fls", "*.fdb_latexmk", "*.blg", "*.bcf", "*.tmp", "*.synctex.gz", "*.run.xml"
    )
    for item in src_dir.iterdir():
        if item.name.startswith("."):
            continue
        dst = build_dir / item.name
        if item.is_dir():
            if item.name.lower() in ASSET_DIR_NAMES:
                _link_or_copy_dir(item, dst)
            else:
                shutil.copytree(item, dst, ignore=ignore_fn)
        elif not _is_build_artifact(item, entry_stem):
            shutil.copy2(item, dst)


def _backfill_missing_assets(old_dir: Path, build_diff: Path) -> None:
    """Copy assets present in historical baseline but removed/renamed in working tree."""
    for src in old_dir.rglob("*"):
        if not src.is_file() or src.suffix.lower() == ".tex" or _is_build_artifact(src):
            continue
        rel = src.relative_to(old_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        dst = build_diff / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _sandbox_root(ws: WorkspacePaths, manuscript_name: str) -> Path:
    ws_hash = hashlib.sha1(str(ws.manuscripts_dir).encode("utf-8")).hexdigest()[:12]
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "RebuttalSidecar" / "cache"
    return base / ws_hash / manuscript_name


def _sync_shared_files(ws: WorkspacePaths, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for item in ws.manuscripts_dir.iterdir():
        if item.is_file() and item.suffix.lower() in SHARED_SUFFIXES:
            shutil.copy2(item, root / item.name)


def _load_compile_meta(root: Path) -> dict:
    meta_path = root / "compile_meta.json"
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_compile_meta(root: Path, meta: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    meta_path = root / "compile_meta.json"
    tmp_path = root / "compile_meta.json.tmp"
    tmp_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, meta_path)


def prepare_sandbox(ws: WorkspacePaths, manuscript_name: str, entry_stem: str = "") -> SandboxPaths:
    """Prepare isolated cache sandbox directories preserving ../references.bib paths."""
    root = _sandbox_root(ws, manuscript_name)
    old_dir = root / "old"
    new_dir = root / "new"
    build_clean = root / "build_clean"
    build_diff = root / "build_diff"

    root.mkdir(parents=True, exist_ok=True)
    old_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)

    _sync_shared_files(ws, root)
    manuscript_dir = ws.manuscripts_dir / manuscript_name
    _sync_manuscript_to_build(manuscript_dir, build_clean, entry_stem=entry_stem)
    _sync_manuscript_to_build(manuscript_dir, build_diff, entry_stem=entry_stem)
    return SandboxPaths(root=root, old_dir=old_dir, new_dir=new_dir, build_clean=build_clean, build_diff=build_diff)


def _safe_extract_tar(tar_bytes: bytes, dest_dir: Path) -> None:
    dest_resolved = dest_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        for member in tf.getmembers():
            target = (dest_resolved / member.name).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                continue
            tf.extract(member, dest_resolved, filter="data")


_ROOT_TEX_EXTS = {".tex", ".bib", ".bst", ".cls", ".sty", ".txt", ".csv"}
_NON_MANUSCRIPT_ROOT_DIRS = {
    "src", "lib", "app", "tests", "test", "data", "datasets", "checkpoints", "models",
    "scripts", "notebooks", "node_modules", "experiments", "results", "outputs",
    "configs", "third_party", "vendor", ".github", ".vscode",
}


def _select_root_archive_paths(git_root: Path, ref: str) -> list[str]:
    ls_res = run_cmd(["git", "ls-tree", ref], cwd=git_root, timeout=15)
    if ls_res.returncode != 0:
        return []
    selected: list[str] = []
    for line in ls_res.stdout.splitlines():
        meta, _, name = line.partition("\t")
        if not name:
            continue
        obj_type = meta.split()[1] if len(meta.split()) >= 2 else ""
        if obj_type == "blob" and Path(name).suffix.lower() in _ROOT_TEX_EXTS:
            selected.append(name)
        elif obj_type == "tree" and not name.startswith(".") and name.lower() not in _NON_MANUSCRIPT_ROOT_DIRS:
            selected.append(name)
    return selected


def extract_baseline(ws: WorkspacePaths, manuscript_name: str, baseline: dict, old_dir: Path) -> Path:
    """Extract historical baseline snapshot into old_dir and write flattened flat_old.tex."""
    _clear_dir(old_dir)
    btype = baseline["type"]
    ref = str(baseline["ref"]).strip()
    if not ref or ref.startswith("-"):
        raise CompileError(f"Invalid baseline reference: {ref}")

    manuscript_dir = (ws.manuscripts_dir / manuscript_name).resolve()
    cur_entry = find_entry_tex(manuscript_dir)
    preferred_entry = cur_entry.name if cur_entry else "main.tex"
    strip_prefix = ""

    if btype == "archive":
        archive_src = ws.manuscripts_dir / "_archive" / ref
        if not archive_src.is_dir():
            raise CompileError(f"Archive baseline not found: {ref}")
        shutil.copytree(archive_src, old_dir, dirs_exist_ok=True)
    elif btype == "git":
        if not ws.git_root:
            raise CompileError("Git repository not found for git baseline.")
        tree_posix = resolve_manuscript_tree_at_ref(ws.git_root, ref, manuscript_dir, preferred_entry)
        if tree_posix is None:
            raise CompileError(f"Manuscript '{manuscript_name}' not found in git baseline '{ref}'.")
        strip_prefix = tree_posix
        if tree_posix:
            cmd = ["git", "archive", "--format=tar", f"{ref}:{tree_posix}"]
        else:
            root_paths = _select_root_archive_paths(ws.git_root, ref)
            cmd = ["git", "archive", "--format=tar", ref, *(["--", *root_paths] if root_paths else [])]
        res = run_cmd(cmd, cwd=ws.git_root, timeout=30, text=False)
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace") if isinstance(res.stderr, bytes) else res.stderr
            raise CompileError(f"Failed to extract git baseline '{ref}': {err.strip()}")
        _safe_extract_tar(res.stdout, old_dir)
    else:
        raise CompileError(f"Unsupported baseline type: {btype}")

    entry = find_entry_tex(old_dir, preferred_name=preferred_entry)
    if not entry:
        raise CompileError(f"No \\documentclass entry file found in baseline '{ref}'.")

    flat_old = old_dir / "flat_old.tex"
    flat_old.write_text(flatten_tex(entry, old_dir, strip_prefix=strip_prefix), encoding="utf-8")
    return flat_old


# Call \normalem after loading ulem without [normalem] so epstopdf-base does not see leftover \@curroptions.
_ULEM_SAFE_PREAMBLE = (
    "\\IfFileExists{ulem.sty}{\\usepackage{ulem}\\normalem}{}\n"
    "\\providecommand{\\uline}[1]{#1}\n"
    "\\providecommand{\\uwave}[1]{#1}\n"
    "\\providecommand{\\sout}[1]{[DEL: #1]}\n"
)

DIF_PREAMBLE = (
    "\\IfFileExists{xcolor.sty}{\\usepackage{xcolor}}{}\n"
    + _ULEM_SAFE_PREAMBLE
    + "\\providecommand{\\DIFadd}[1]{{\\protect\\textcolor{blue}{\\uline{#1}}}}\n"
    + "\\providecommand{\\DIFdel}[1]{{\\protect\\textcolor{red}{\\sout{#1}}}}\n"
)

_OPAQUE_ENVS = (
    r"equation\*?|align\*?|gather\*?|multline\*?|flalign\*?|eqnarray\*?"
    r"|math|displaymath|tabular\*?|tabularx|tabulary|array"
    r"|matrix|pmatrix|bmatrix|vmatrix|cases"
    r"|verbatim|lstlisting|minted|tikzpicture|algorithmic"
)
_OPAQUE_CMDS = (
    r"cite[a-zA-Z*]*|[cC]?ref|eqref|autoref|pageref|label"
    r"|includegraphics|bibliography|bibliographystyle"
    r"|usepackage|RequirePackage|documentclass|begin|end"
    r"|vspace\*?|hspace\*?|vskip|hskip|IEEEtriggeratref"
    r"|pagenumbering|thispagestyle|pagestyle"
)

# Lossless partition tokenizer: trailing `|.` ensures escaped symbols like `\&` or `\\` are never dropped.
_TOKEN_RE = re.compile(
    rf"(?<!\\)%[^\n]*"
    rf"|\\begin\{{({_OPAQUE_ENVS})\}}.*?\\end\{{\1\}}"
    r"|\\\[.*?\\\]"
    r"|\$\$.*?\$\$"
    r"|\\\(.*?\\\)"
    r"|\$(?:\\.|[^$\\])+\$"
    rf"|\\(?:{_OPAQUE_CMDS})\b(?:\s*\[[^\]]*\])*(?:\s*\{{[^{{}}]*\}})*"
    r"|\\(?:[&%$#_])"
    r"|\\[a-zA-Z@]+\*?"
    r"|\\[^a-zA-Z@]"
    r"|\n[ \t]*\n+"
    r"|[ \t\r\n]+"
    r"|[^\s\\$%{}&~^_#\[\]]+"
    r"|.",
    re.DOTALL,
)

_SAFE_ESCAPES = {r"\&", r"\%", r"\$", r"\#", r"\_"}
_STRUCTURAL_CHARS = set("{}&~^_#[]\\$%")


def _is_structural_token(tok: str) -> bool:
    if not tok:
        return True
    if tok in _SAFE_ESCAPES:
        return False
    if tok.isspace():
        return tok.count("\n") >= 2
    return any(ch in _STRUCTURAL_CHARS for ch in tok)


def _wrap_prose_tokens(tokens: list[str], macro: str) -> str:
    out: list[str] = []
    prose_buf: list[str] = []

    def flush() -> None:
        if not prose_buf:
            return
        chunk = "".join(prose_buf)
        prose_buf.clear()
        if chunk.strip():
            lead = chunk[: len(chunk) - len(chunk.lstrip())]
            trail = chunk[len(chunk.rstrip()) :]
            core = chunk.strip()
            out.append(f"{lead}\\{macro}{{{core}}}{trail}")
        else:
            out.append(chunk)

    for tok in tokens:
        if _is_structural_token(tok):
            flush()
            if macro == "DIFadd":
                out.append(tok)
        else:
            prose_buf.append(tok)
    flush()
    return "".join(out)


def builtin_tex_diff(old_tex: str, new_tex: str) -> str:
    """Pure-Python fallback diff highlighting prose additions/deletions safely."""
    begin_tag = "\\begin{document}"
    end_tag = "\\end{document}"
    new_b = new_tex.find(begin_tag)
    new_e = new_tex.rfind(end_tag)
    old_b = old_tex.find(begin_tag)
    old_e = old_tex.rfind(end_tag)
    if new_b == -1 or new_e == -1 or old_b == -1 or old_e == -1:
        return new_tex

    preamble = new_tex[:new_b] + DIF_PREAMBLE + begin_tag
    suffix = new_tex[new_e:]
    old_body = old_tex[old_b + len(begin_tag) : old_e]
    new_body = new_tex[new_b + len(begin_tag) : new_e]

    old_tokens = [m.group(0) for m in _TOKEN_RE.finditer(old_body)]
    new_tokens = [m.group(0) for m in _TOKEN_RE.finditer(new_body)]
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)

    parts: list[str] = [preamble]
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append("".join(new_tokens[j1:j2]))
        elif tag == "delete":
            parts.append(_wrap_prose_tokens(old_tokens[i1:i2], "DIFdel"))
        elif tag == "insert":
            parts.append(_wrap_prose_tokens(new_tokens[j1:j2], "DIFadd"))
        elif tag == "replace":
            parts.append(_wrap_prose_tokens(old_tokens[i1:i2], "DIFdel"))
            parts.append(_wrap_prose_tokens(new_tokens[j1:j2], "DIFadd"))
    parts.append(suffix)
    return "".join(parts)


def generate_latexdiff(
    flat_old: Path,
    flat_new: Path,
    diff_tex: Path,
    compiler_settings: dict,
) -> None:
    """Generate diff_tex via latexdiff-so/latexdiff, falling back to builtin_tex_diff."""
    latexdiff_exe = resolve_latexdiff(compiler_settings)
    if latexdiff_exe:
        env = build_tex_env(compiler_settings)
        timeout = int(compiler_settings.get("compile_timeout", 180))
        flags = miktex_installer_flags(latexdiff_exe, compiler_settings)
        res = run_cmd(
            [
                latexdiff_exe,
                *flags,
                "--encoding=utf8",
                "--math-markup=coarse",
                "--graphics-markup=none",
                str(flat_old),
                str(flat_new),
            ],
            cwd=diff_tex.parent,
            timeout=timeout,
            env=env,
        )
        if res.returncode == 0 and res.stdout.strip():
            output = res.stdout
            for needle in ("\\RequirePackage[normalem]{ulem}", "\\usepackage[normalem]{ulem}"):
                output = output.replace(needle, _ULEM_SAFE_PREAMBLE.rstrip("\n"))
            diff_tex.write_text(output, encoding="utf-8")
            return

    old_text = flat_old.read_text(encoding="utf-8", errors="replace")
    new_text = flat_new.read_text(encoding="utf-8", errors="replace")
    diff_tex.write_text(builtin_tex_diff(old_text, new_text), encoding="utf-8")


def _parse_extra_args(extra_args: str) -> list[str]:
    if not extra_args.strip():
        return []
    tokens = shlex.split(extra_args, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'") else t for t in tokens]


def _run_engine_once(
    engine_exe: str,
    tex_name: str,
    cwd: Path,
    extra_args: list[str],
    timeout: int,
    env: dict[str, str],
    compiler_settings: dict,
) -> tuple[int, str]:
    flags = miktex_installer_flags(engine_exe, compiler_settings)
    cmd = [engine_exe, *flags, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", *extra_args, tex_name]
    res = run_cmd(cmd, cwd=cwd, timeout=timeout, env=env)
    return res.returncode, f"{res.stdout}\n{res.stderr}"


def compile_tex_file(
    tex_file: Path,
    engine: str,
    compiler_settings: dict | None = None,
) -> Path:
    """Compile tex_file using configured recipe and engine, returning output PDF path."""
    settings = {**DEFAULT_COMPILER_SETTINGS, **(compiler_settings or {})}
    env = build_tex_env(settings)
    timeout = int(settings.get("compile_timeout", 180))
    extra_args = _parse_extra_args(str(settings.get("extra_args", "")))
    recipe = settings.get("recipe", "auto")
    cwd = tex_file.parent
    stem = tex_file.stem
    log_path = cwd / f"{stem}.log"
    pdf_path = cwd / f"{stem}.pdf"

    latexmk_exe = which_tool("latexmk", settings)
    use_latexmk = recipe == "latexmk" or (recipe == "auto" and latexmk_exe is not None)

    if use_latexmk:
        if not latexmk_exe:
            raise CompileError("latexmk executable not found. Configure TeX bin dir or switch recipe.")
        mode_flag = {"xelatex": "-xelatex", "lualatex": "-lualatex"}.get(engine, "-pdf")
        mk_flags = miktex_installer_flags(latexmk_exe, settings)
        latex_opts = [f"-latexoption={flag}" for flag in mk_flags]
        cmd = [
            latexmk_exe,
            *mk_flags,
            *latex_opts,
            mode_flag,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            *extra_args,
            tex_file.name,
        ]
        res = run_cmd(cmd, cwd=cwd, timeout=timeout, env=env)
        combined = f"{res.stdout}\n{res.stderr}"
        if res.returncode != 0 or not pdf_path.is_file():
            raise CompileError(
                f"LaTeX compilation failed ({tex_file.name})",
                errors=parse_latex_errors(combined, log_path),
                log_tail=combined[-3000:],
            )
        return pdf_path

    engine_exe = which_tool(engine, settings)
    if not engine_exe:
        raise CompileError(f"Compiler '{engine}' not found. Check TeX bin directory in settings.")

    rc, output = _run_engine_once(engine_exe, tex_file.name, cwd, extra_args, timeout, env, settings)
    if rc != 0:
        raise CompileError(
            f"LaTeX compilation failed ({tex_file.name})",
            errors=parse_latex_errors(output, log_path),
            log_tail=output[-3000:],
        )

    bcf_path = cwd / f"{stem}.bcf"
    aux_path = cwd / f"{stem}.aux"
    aux_text = aux_path.read_text(encoding="utf-8", errors="replace") if aux_path.is_file() else ""

    if recipe == "engine_biber" or (recipe == "auto" and bcf_path.is_file()):
        biber_exe = which_tool("biber", settings)
        if biber_exe and bcf_path.is_file():
            run_cmd([biber_exe, stem], cwd=cwd, timeout=timeout, env=env)
            rc, output = _run_engine_once(engine_exe, tex_file.name, cwd, extra_args, timeout, env, settings)
    elif recipe == "engine_bibtex" or (recipe == "auto" and "\\bibdata" in aux_text):
        bibtex_exe = which_tool("bibtex", settings)
        if bibtex_exe and aux_path.is_file():
            bib_flags = miktex_installer_flags(bibtex_exe, settings)
            run_cmd([bibtex_exe, *bib_flags, stem], cwd=cwd, timeout=timeout, env=env)
            rc, output = _run_engine_once(engine_exe, tex_file.name, cwd, extra_args, timeout, env, settings)

    if rc == 0 and recipe != "engine_only":
        rc, output = _run_engine_once(engine_exe, tex_file.name, cwd, extra_args, timeout, env, settings)

    if rc != 0 or not pdf_path.is_file():
        raise CompileError(
            f"LaTeX compilation failed ({tex_file.name})",
            errors=parse_latex_errors(output, log_path),
            log_tail=output[-3000:],
        )
    return pdf_path


def _existing_companion_pdfs(raw_map: dict | None) -> dict[str, str]:
    if not isinstance(raw_map, dict):
        return {}
    return {k: str(v) for k, v in raw_map.items() if isinstance(v, str) and Path(v).is_file()}


def _hydrate_from_disk(ws: WorkspacePaths, manuscript_name: str) -> dict | None:
    """Restore previously compiled PDFs from persistent cache or pre-built host PDFs."""
    manuscript_dir = ws.manuscripts_dir / manuscript_name
    if not manuscript_dir.is_dir():
        return None
    root = _sandbox_root(ws, manuscript_name)
    meta = _load_compile_meta(root)

    clean_pdf = meta.get("clean_pdf", "")
    if not clean_pdf or not Path(clean_pdf).is_file():
        clean_pdf = ""
        entry_tex = find_entry_tex(manuscript_dir)
        if entry_tex:
            prebuilt = manuscript_dir / f"{entry_tex.stem}.pdf"
            if prebuilt.is_file():
                cached_clean = root / "build_clean" / "clean.pdf"
                cached_clean.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(prebuilt, cached_clean)
                clean_pdf = str(cached_clean)

    diff_pdf = meta.get("diff_pdf", "")
    if not diff_pdf or not Path(diff_pdf).is_file():
        diff_pdf = ""

    companions = _existing_companion_pdfs(meta.get("companion_pdfs"))
    for tex in manuscript_dir.glob("*.tex"):
        if tex.name not in companions:
            prebuilt_comp = manuscript_dir / f"{tex.stem}.pdf"
            entry_tex = find_entry_tex(manuscript_dir)
            if prebuilt_comp.is_file() and (not entry_tex or tex.name != entry_tex.name):
                cached_comp = root / "build_companions" / f"{tex.stem}.pdf"
                cached_comp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(prebuilt_comp, cached_comp)
                companions[tex.name] = str(cached_comp)

    if not clean_pdf and not diff_pdf and not companions:
        return None

    is_dirty = False
    if clean_pdf:
        pdf_mtime = Path(clean_pdf).stat().st_mtime
        src_mtimes = [
            p.stat().st_mtime
            for p in manuscript_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {".tex", ".bib", ".cls", ".sty"}
        ]
        if src_mtimes and max(src_mtimes) > pdf_mtime + 1.0:
            is_dirty = True

    return {
        "manuscript": manuscript_name,
        "state": "success",
        "dirty": is_dirty,
        "engine": meta.get("engine", "cached"),
        "has_clean": bool(clean_pdf),
        "has_diff": bool(diff_pdf),
        "clean_pdf": clean_pdf,
        "diff_pdf": diff_pdf,
        "companion_pdfs": companions,
        "compiling_target": "",
        "errors": [],
        "log_tail": "",
        "message": "Loaded cached PDF",
        "compiled_at": meta.get("compiled_at", ""),
    }


def run_compile_job(
    ws: WorkspacePaths,
    manuscript_name: str,
    baseline: dict | None,
    preferred_engine: str = "auto",
    compiler_settings: dict | None = None,
    target_tex: str = "",
) -> dict:
    """Execute full sandbox preparation, Clean/Diff PDF build, or companion .tex build."""
    settings = {**DEFAULT_COMPILER_SETTINGS, **(compiler_settings or {})}
    manuscript_dir = ws.manuscripts_dir / manuscript_name
    entry_tex = find_entry_tex(manuscript_dir)
    if not entry_tex:
        raise CompileError(f"No \\documentclass entry file found in {manuscript_name}.")

    root = _sandbox_root(ws, manuscript_name)
    prev_meta = _load_compile_meta(root)
    companions = _existing_companion_pdfs(prev_meta.get("companion_pdfs"))

    if target_tex and target_tex != entry_tex.name:
        comp_src = (manuscript_dir / target_tex).resolve()
        if not comp_src.is_file() or comp_src.parent != manuscript_dir.resolve():
            raise CompileError(f"Companion TeX file not found: {target_tex}")
        _sync_shared_files(ws, root)
        build_dir = root / "build_companions" / comp_src.stem
        _sync_manuscript_to_build(manuscript_dir, build_dir, entry_stem=comp_src.stem)
        comp_text = flatten_tex(comp_src, manuscript_dir)
        engine = detect_tex_engine(
            comp_text,
            preferred=preferred_engine,
            default_engine=settings.get("default_engine", "auto"),
        )
        built_pdf = compile_tex_file(build_dir / comp_src.name, engine, settings)
        out_pdf = root / "build_companions" / f"{comp_src.stem}.pdf"
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built_pdf, out_pdf)
        companions[target_tex] = str(out_pdf)

        clean_pdf_str = prev_meta.get("clean_pdf", "")
        if not clean_pdf_str or not Path(clean_pdf_str).is_file():
            clean_pdf_str = ""
        diff_pdf_str = prev_meta.get("diff_pdf", "")
        if not diff_pdf_str or not Path(diff_pdf_str).is_file():
            diff_pdf_str = ""
        compiled_at = _utc_now_iso()
        result = {
            "manuscript": manuscript_name,
            "engine": engine,
            "baseline": prev_meta.get("baseline", baseline),
            "clean_pdf": clean_pdf_str,
            "diff_pdf": diff_pdf_str,
            "companion_pdfs": companions,
            "compiled_at": compiled_at,
        }
        _save_compile_meta(root, result)
        return result

    sandbox = prepare_sandbox(ws, manuscript_name, entry_stem=entry_tex.stem)
    flat_new_text = flatten_tex(entry_tex, manuscript_dir)
    flat_new = sandbox.new_dir / "flat_new.tex"
    flat_new.write_text(flat_new_text, encoding="utf-8")

    engine = detect_tex_engine(
        flat_new_text,
        preferred=preferred_engine,
        default_engine=settings.get("default_engine", "auto"),
    )

    clean_entry = sandbox.build_clean / entry_tex.name
    built_clean = compile_tex_file(clean_entry, engine, settings)
    clean_pdf = sandbox.build_clean / "clean.pdf"
    if built_clean.resolve() != clean_pdf.resolve():
        shutil.copy2(built_clean, clean_pdf)

    diff_pdf: Path | None = None
    if baseline and baseline.get("type") in ("git", "archive") and baseline.get("ref"):
        try:
            flat_old = extract_baseline(ws, manuscript_name, baseline, sandbox.old_dir)
            _backfill_missing_assets(sandbox.old_dir, sandbox.build_diff)
            diff_tex = sandbox.build_diff / "diff.tex"
            generate_latexdiff(flat_old, flat_new, diff_tex, settings)
            diff_pdf = compile_tex_file(diff_tex, engine, settings)
        except CompileError as exc:
            exc.clean_pdf = str(clean_pdf)
            _save_compile_meta(
                root,
                {
                    "manuscript": manuscript_name,
                    "engine": engine,
                    "baseline": baseline,
                    "clean_pdf": str(clean_pdf),
                    "diff_pdf": "",
                    "companion_pdfs": companions,
                    "compiled_at": _utc_now_iso(),
                },
            )
            raise

    result = {
        "manuscript": manuscript_name,
        "engine": engine,
        "baseline": baseline,
        "clean_pdf": str(clean_pdf),
        "diff_pdf": str(diff_pdf) if diff_pdf else "",
        "companion_pdfs": companions,
        "compiled_at": _utc_now_iso(),
    }
    _save_compile_meta(root, result)
    return result


class CompileCoordinator:
    """Single-worker background compile queue with latest-wins coalescing and dedup lock."""

    def __init__(self, on_event: Callable[[str, dict], None] | None = None) -> None:
        self._on_event = on_event
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pending_job: dict | None = None
        self._active_key: tuple[str, str] | None = None
        self._stopped = False
        self._statuses: dict[str, dict] = {}
        self._worker = threading.Thread(target=self._loop, name="compile-worker", daemon=True)
        self._worker.start()

    def _emit(self, event: str, payload: dict) -> None:
        if self._on_event:
            self._on_event(event, payload)

    def _default_status(self, manuscript_name: str) -> dict:
        return {
            "manuscript": manuscript_name,
            "state": "idle",
            "dirty": False,
            "engine": "",
            "has_clean": False,
            "has_diff": False,
            "clean_pdf": "",
            "diff_pdf": "",
            "companion_pdfs": {},
            "compiling_target": "",
            "errors": [],
            "log_tail": "",
            "message": "",
            "compiled_at": "",
        }

    def get_status(self, manuscript_name: str, ws: WorkspacePaths | None = None) -> dict:
        with self._lock:
            current = self._statuses.get(manuscript_name)
            if current:
                return dict(current)
            if ws is not None and manuscript_name:
                hydrated = _hydrate_from_disk(ws, manuscript_name)
                if hydrated:
                    self._statuses[manuscript_name] = hydrated
                    return dict(hydrated)
            return self._default_status(manuscript_name)

    def mark_dirty(self, manuscript_name: str, changed_file: str = "") -> None:
        with self._lock:
            st = dict(self._statuses.get(manuscript_name) or self._default_status(manuscript_name))
            st["dirty"] = True
            if changed_file:
                st["message"] = f"Changed: {changed_file}"
            self._statuses[manuscript_name] = st

    def enqueue(
        self,
        ws: WorkspacePaths,
        manuscript_name: str,
        baseline: dict | None,
        preferred_engine: str,
        compiler_settings: dict,
        target_tex: str = "",
    ) -> dict:
        target_key = target_tex or "main"
        with self._lock:
            current = self._statuses.get(manuscript_name)
            if current and current.get("state") == "compiling":
                if self._active_key == (manuscript_name, target_key) and self._pending_job is None:
                    return dict(current)
                if self._pending_job and (
                    self._pending_job["manuscript_name"] == manuscript_name
                    and (self._pending_job.get("target_tex") or "main") == target_key
                ):
                    return dict(current)

            prev = dict(current or self._default_status(manuscript_name))
            prev.update(
                {
                    "state": "compiling",
                    "compiling_target": target_key,
                    "errors": [],
                    "message": "Compiling...",
                }
            )
            self._statuses[manuscript_name] = prev
            self._pending_job = {
                "ws": ws,
                "manuscript_name": manuscript_name,
                "baseline": baseline,
                "preferred_engine": preferred_engine,
                "compiler_settings": dict(compiler_settings),
                "target_tex": target_tex,
            }
            self._cond.notify()
            return dict(prev)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._cond.notify_all()
        self._worker.join(timeout=3)

    def _loop(self) -> None:
        while True:
            with self._lock:
                while self._pending_job is None and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                job = self._pending_job
                self._pending_job = None

                name = job["manuscript_name"]
                target_tex = job.get("target_tex", "")
                self._active_key = (name, target_tex or "main")
                prev = dict(self._statuses.get(name) or self._default_status(name))
                prev.update(
                    {
                        "state": "compiling",
                        "compiling_target": target_tex or "main",
                        "errors": [],
                        "message": "Compiling...",
                    }
                )
                self._statuses[name] = prev

            self._emit("COMPILE_START", {"manuscript": name, "baseline": job["baseline"], "target_tex": target_tex})

            try:
                result = run_compile_job(
                    ws=job["ws"],
                    manuscript_name=name,
                    baseline=job["baseline"],
                    preferred_engine=job["preferred_engine"],
                    compiler_settings=job["compiler_settings"],
                    target_tex=target_tex,
                )
                status = {
                    "manuscript": name,
                    "state": "success",
                    "dirty": False,
                    "engine": result["engine"],
                    "has_clean": bool(result["clean_pdf"]),
                    "has_diff": bool(result["diff_pdf"]),
                    "clean_pdf": result["clean_pdf"],
                    "diff_pdf": result["diff_pdf"],
                    "companion_pdfs": result.get("companion_pdfs", {}),
                    "compiling_target": "",
                    "errors": [],
                    "log_tail": "",
                    "message": "Compile succeeded",
                    "compiled_at": result["compiled_at"],
                }
                with self._lock:
                    self._active_key = None
                    self._statuses[name] = status
                self._emit("COMPILE_SUCCESS", status)
            except Exception as exc:
                errors = exc.errors if isinstance(exc, CompileError) else []
                log_tail = exc.log_tail if isinstance(exc, CompileError) else ""
                clean_pdf = exc.clean_pdf if isinstance(exc, CompileError) else ""
                with self._lock:
                    self._active_key = None
                    prev = dict(self._statuses.get(name) or self._default_status(name))
                    prev.update(
                        {
                            "state": "error",
                            "has_clean": bool(clean_pdf) or prev.get("has_clean", False),
                            "clean_pdf": clean_pdf or prev.get("clean_pdf", ""),
                            "compiling_target": "",
                            "errors": errors,
                            "log_tail": log_tail,
                            "message": str(exc),
                        }
                    )
                    status = dict(prev)
                    self._statuses[name] = status
                self._emit("COMPILE_ERROR", status)
