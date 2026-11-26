"""Manuscript discovery, .gitignore filtering, and Git/archive baseline listing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from proc_utils import run_cmd

DOCUMENTCLASS_RE = re.compile(r"\\documentclass\b")

COMPANION_ROLES = (
    (("title_page", "titlepage", "title"), "Title Page"),
    (("highlight",), "Highlights"),
    (("cover_letter", "coverletter", "cover"), "Cover Letter"),
    (("response", "rebuttal", "reply"), "Response Letter"),
    (("declaration", "conflict", "competing"), "Declaration"),
    (("credit", "author"), "Author Statement"),
    (("supplement", "appendix", "si"), "Supplement"),
)
COMPANION_TEXT_KEYWORDS = ("title", "highlight", "cover", "response", "rebuttal", "declaration", "credit", "author", "abstract")


def classify_companion_role(filename: str) -> str:
    stem = Path(filename).stem.lower()
    for keys, label in COMPANION_ROLES:
        if any(k in stem for k in keys):
            return label
    return "Companion"


def scan_companion_files(manuscript_dir: Path, entry_tex_name: str) -> list[dict]:
    """Discover non-main companion files (title page, highlights, cover letter, etc.)."""
    if not manuscript_dir.is_dir():
        return []

    companions: list[dict] = []
    for item in sorted(manuscript_dir.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_file() or item.name.startswith(".") or item.name == entry_tex_name:
            continue
        suffix = item.suffix.lower()
        if suffix == ".tex":
            try:
                text = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(DOCUMENTCLASS_RE.search(_strip_tex_comment(line)) for line in text.splitlines()):
                companions.append(
                    {
                        "name": item.name,
                        "role": classify_companion_role(item.name),
                        "compilable": True,
                        "kind": "tex",
                    }
                )
        elif suffix in (".md", ".txt"):
            stem = item.stem.lower()
            if any(k in stem for k in COMPANION_TEXT_KEYWORDS):
                companions.append(
                    {
                        "name": item.name,
                        "role": classify_companion_role(item.name),
                        "compilable": False,
                        "kind": "text",
                    }
                )
    return companions


@dataclass(frozen=True)
class WorkspacePaths:
    project_root: Path
    manuscripts_dir: Path
    git_root: Path | None
    single_manuscript: Path | None = None


def _strip_tex_comment(line: str) -> str:
    """Return line portion before the first unescaped '%' character."""
    for idx, ch in enumerate(line):
        if ch == "%" and (idx == 0 or line[idx - 1] != "\\"):
            return line[:idx]
    return line


def find_entry_tex(manuscript_dir: Path) -> Path | None:
    """Locate the main .tex entry file containing \\documentclass."""
    if not manuscript_dir.is_dir():
        return None

    candidates = sorted(manuscript_dir.glob("*.tex"), key=lambda p: (p.name != "main.tex", p.name.lower()))
    for tex_file in candidates:
        try:
            text = tex_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if DOCUMENTCLASS_RE.search(_strip_tex_comment(line)):
                return tex_file
    return None


def _find_git_root(start_dir: Path) -> Path | None:
    try:
        res = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=start_dir, timeout=10)
    except OSError:
        return None
    if res.returncode == 0 and res.stdout.strip():
        return Path(res.stdout.strip()).resolve()
    return None


def resolve_workspace(path_str: str) -> WorkspacePaths:
    """Resolve project_root, manuscripts_dir, and optional git_root from a user path."""
    target = Path(path_str).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"Workspace directory not found: {target}")

    nested = target / "docs" / "manuscripts"
    single_manuscript: Path | None = None
    if nested.is_dir():
        project_root = target
        manuscripts_dir = nested
    elif target.name == "manuscripts" and target.parent.name == "docs":
        project_root = target.parent.parent
        manuscripts_dir = target
    elif find_entry_tex(target) is not None:
        project_root = target
        manuscripts_dir = target.parent
        single_manuscript = target
    else:
        project_root = target
        manuscripts_dir = target

    git_root = _find_git_root(project_root)
    return WorkspacePaths(
        project_root=project_root,
        manuscripts_dir=manuscripts_dir,
        git_root=git_root,
        single_manuscript=single_manuscript,
    )


def is_git_ignored(path: Path, git_root: Path | None) -> bool:
    """Return True if path is ignored by the repository's .gitignore rules."""
    if not git_root:
        return False
    try:
        res = run_cmd(["git", "check-ignore", "-q", str(path)], cwd=git_root, timeout=10)
    except OSError:
        return False
    return res.returncode == 0


def list_archive_baselines(manuscripts_dir: Path) -> list[dict]:
    """Scan _archive/ subdirectories for valid manuscript snapshots."""
    archive_dir = manuscripts_dir / "_archive"
    if not archive_dir.is_dir():
        return []

    archives = []
    for sub in sorted(archive_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        entry = find_entry_tex(sub)
        if entry:
            archives.append(
                {
                    "type": "archive",
                    "ref": sub.name,
                    "label": f"Archive: {sub.name}",
                    "entry_tex": entry.name,
                }
            )
    return archives


def scan_workspace(path_str: str) -> dict:
    """Scan manuscripts_dir for active manuscripts and _archive baselines."""
    ws = resolve_workspace(path_str)
    manuscripts = []
    if ws.single_manuscript is not None:
        entry = find_entry_tex(ws.single_manuscript)
        if entry:
            manuscripts.append(
                {
                    "name": ws.single_manuscript.name,
                    "path": str(ws.single_manuscript),
                    "entry_tex": entry.name,
                    "has_todo": (ws.single_manuscript / ".rebuttal_todo.json").is_file(),
                    "companions": scan_companion_files(ws.single_manuscript, entry.name),
                }
            )
    else:
        for sub in sorted(ws.manuscripts_dir.iterdir(), key=lambda p: p.name.lower()):
            if not sub.is_dir() or sub.name.startswith((".", "_")):
                continue
            if is_git_ignored(sub, ws.git_root):
                continue
            entry = find_entry_tex(sub)
            if not entry:
                continue
            manuscripts.append(
                {
                    "name": sub.name,
                    "path": str(sub),
                    "entry_tex": entry.name,
                    "has_todo": (sub / ".rebuttal_todo.json").is_file(),
                    "companions": scan_companion_files(sub, entry.name),
                }
            )

    return {
        "project_root": str(ws.project_root),
        "manuscripts_dir": str(ws.manuscripts_dir),
        "git_root": str(ws.git_root) if ws.git_root else "",
        "manuscripts": manuscripts,
        "archive_baselines": list_archive_baselines(ws.manuscripts_dir),
    }


def list_baselines(ws: WorkspacePaths, manuscript_name: str) -> dict:
    """Return available _archive and Git tag/commit baselines for a manuscript."""
    archives = list_archive_baselines(ws.manuscripts_dir)
    git_baselines: list[dict] = []

    if not ws.git_root:
        return {"archive": archives, "git": git_baselines}

    manuscript_dir = (ws.manuscripts_dir / manuscript_name).resolve()
    try:
        rel_posix = manuscript_dir.relative_to(ws.git_root).as_posix()
    except ValueError:
        return {"archive": archives, "git": git_baselines}

    try:
        tags_res = run_cmd(["git", "tag", "-l", "--sort=-creatordate"], cwd=ws.git_root, timeout=10)
        if tags_res.returncode == 0:
            for tag in tags_res.stdout.splitlines()[:20]:
                tag = tag.strip()
                if not tag:
                    continue
                exists = run_cmd(["git", "cat-file", "-e", f"{tag}:{rel_posix}"], cwd=ws.git_root, timeout=10)
                if exists.returncode == 0:
                    git_baselines.append(
                        {
                            "type": "git",
                            "ref": tag,
                            "kind": "tag",
                            "label": f"Tag: {tag}",
                        }
                    )

        log_res = run_cmd(
            ["git", "log", "-n", "30", "--date=short", "--pretty=format:%h%x00%H%x00%ad%x00%s", "--", rel_posix],
            cwd=ws.git_root,
            timeout=10,
        )
        if log_res.returncode == 0:
            for line in log_res.stdout.splitlines():
                parts = line.split("\x00", 3)
                if len(parts) != 4:
                    continue
                short_hash, full_hash, date_str, subject = parts
                git_baselines.append(
                    {
                        "type": "git",
                        "ref": short_hash,
                        "commit": full_hash,
                        "date": date_str,
                        "kind": "commit",
                        "label": f"{short_hash} ({date_str}) {subject}",
                    }
                )
    except OSError:
        pass

    return {"archive": archives, "git": git_baselines}
