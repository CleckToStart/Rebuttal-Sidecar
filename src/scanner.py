"""Manuscript discovery, .gitignore filtering, and Git/archive baseline listing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from proc_utils import run_cmd

DOCUMENTCLASS_RE = re.compile(r"\\documentclass\b")
TRANSLATION_STEM_RE = re.compile(r"(?:^|[_\-])(zh|cn|translated|translation)(?:$|[_\-])")

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
GENERIC_DIR_TOKENS = {"main", "paper", "doc", "docs", "manuscript", "manuscripts", "tex"}


def classify_companion_role(filename: str) -> str:
    stem = Path(filename).stem.lower()
    if TRANSLATION_STEM_RE.search(stem):
        return "Translation"
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


def find_entry_tex(manuscript_dir: Path, preferred_name: str = "") -> Path | None:
    """Locate the main .tex entry file containing \\documentclass."""
    if not manuscript_dir.is_dir():
        return None

    pref = preferred_name.lower()
    candidates = sorted(
        manuscript_dir.glob("*.tex"),
        key=lambda p: (
            bool(pref) and p.name.lower() != pref,
            p.name.lower() != "main.tex",
            classify_companion_role(p.name) != "Companion",
            p.name.lower(),
        ),
    )
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


def _posix_rel_dir(path: Path, git_root: Path) -> str:
    resolved = path.resolve()
    root_resolved = git_root.resolve()
    if resolved == root_resolved:
        return ""
    return resolved.relative_to(root_resolved).as_posix()


def _posix_parent(posix_path: str) -> str:
    return posix_path.rsplit("/", 1)[0] if "/" in posix_path else ""


def _parse_commit_lines(stdout: str, kind: str = "commit") -> list[dict]:
    items: list[dict] = []
    for line in stdout.splitlines():
        parts = line.split("\x00", 3)
        if len(parts) != 4:
            continue
        short_hash, full_hash, date_str, subject = parts
        items.append(
            {
                "type": "git",
                "ref": short_hash,
                "commit": full_hash,
                "date": date_str,
                "kind": kind,
                "label": f"{short_hash} ({date_str}) {subject}",
            }
        )
    return items


def discover_manuscript_history_paths(git_root: Path, manuscript_dir: Path, entry_name: str = "") -> dict:
    """Discover current and pre-relocation Git paths for a manuscript across moves and file splits."""
    rel_dir = _posix_rel_dir(manuscript_dir, git_root)
    entry_tex = find_entry_tex(manuscript_dir, preferred_name=entry_name)
    ename = entry_name or (entry_tex.name if entry_tex else "main.tex")

    dirs: list[str] = []
    root_files: list[str] = []

    def add_dir(d: str, root_file: str = "") -> None:
        if d not in dirs:
            dirs.append(d)
        if d == "" and root_file and root_file not in root_files:
            root_files.append(root_file)

    add_dir(rel_dir, ename if rel_dir == "" else "")
    rel_entry = f"{rel_dir}/{ename}" if rel_dir else ename

    try:
        follow_res = run_cmd(
            ["git", "log", "--follow", "-M15%", "-n", "80", "--name-only", "--pretty=format:", "--", rel_entry],
            cwd=git_root,
            timeout=10,
        )
        if follow_res.returncode == 0:
            for raw in follow_res.stdout.splitlines():
                p = raw.strip()
                if p:
                    parent = _posix_parent(p)
                    add_dir(parent, p if parent == "" else "")

        inspected: set[str] = set()
        for _ in range(3):
            pending = [d for d in dirs if d and d not in inspected]
            if not pending:
                break
            for d in pending:
                inspected.add(d)
                oldest_res = run_cmd(
                    ["git", "log", "--reverse", "-n", "200", "--pretty=format:%H", "--", d],
                    cwd=git_root,
                    timeout=10,
                )
                oldest_lines = [l.strip() for l in oldest_res.stdout.splitlines() if l.strip()] if oldest_res.returncode == 0 else []
                if not oldest_lines:
                    continue
                oldest_commit = oldest_lines[0]
                show_res = run_cmd(
                    ["git", "show", "-M15%", "-C15%", "--name-status", "--pretty=format:", oldest_commit],
                    cwd=git_root,
                    timeout=10,
                )
                matched_move = False
                deleted_main_tex: list[str] = []
                if show_res.returncode == 0:
                    for raw in show_res.stdout.splitlines():
                        cols = raw.split("\t")
                        if len(cols) >= 3 and cols[0].startswith(("R", "C")):
                            old_p, new_p = cols[1], cols[2]
                            if new_p == d or new_p.startswith(f"{d}/"):
                                rel_inside = new_p[len(d) + 1 :] if new_p.startswith(f"{d}/") else ""
                                if "/" in rel_inside and old_p.endswith(f"/{rel_inside}"):
                                    old_dir = old_p[: -len(rel_inside) - 1]
                                elif "/" in rel_inside and old_p == rel_inside:
                                    old_dir = ""
                                else:
                                    old_dir = _posix_parent(old_p)
                                if old_dir != d:
                                    matched_move = True
                                add_dir(old_dir, old_p if old_dir == "" else "")
                        elif len(cols) >= 2 and cols[0] == "D":
                            old_p = cols[1]
                            if old_p.lower().endswith(".tex") and classify_companion_role(Path(old_p).name) == "Companion":
                                deleted_main_tex.append(old_p)

                if not matched_move and deleted_main_tex:
                    tokens = {
                        t
                        for t in re.split(r"[^a-z0-9]+", manuscript_dir.name.lower())
                        if len(t) >= 3 and t not in GENERIC_DIR_TOKENS
                    }
                    by_parent: dict[str, list[str]] = {}
                    for p in deleted_main_tex:
                        by_parent.setdefault(_posix_parent(p), []).append(p)
                    selected_parents = [
                        parent
                        for parent in by_parent
                        if any(tok in parent.lower() or any(tok in Path(p).stem.lower() for p in by_parent[parent]) for tok in tokens)
                    ]
                    if not selected_parents and len(by_parent) == 1:
                        selected_parents = list(by_parent.keys())
                    for parent in selected_parents:
                        add_dir(parent, by_parent[parent][0] if parent == "" else "")
                        matched_move = True

                if not matched_move:
                    parent_tree = run_cmd(
                        ["git", "ls-tree", "-r", "--name-only", f"{oldest_commit}^"],
                        cwd=git_root,
                        timeout=10,
                    )
                    if parent_tree.returncode == 0:
                        target_folder = manuscript_dir.name.lower()
                        for raw in parent_tree.stdout.splitlines():
                            p = raw.strip()
                            if p.lower().endswith(".tex"):
                                parent = _posix_parent(p)
                                if Path(parent).name.lower() == target_folder:
                                    add_dir(parent, p if parent == "" else "")
    except OSError:
        pass

    return {"dirs": dirs, "root_files": root_files}


def resolve_manuscript_tree_at_ref(
    git_root: Path,
    ref: str,
    manuscript_dir: Path,
    entry_name: str = "",
    history: dict | None = None,
) -> str | None:
    """Locate the POSIX subdirectory (or '' for repo root) containing the manuscript at ref."""
    if not ref or ref.startswith("-"):
        return None
    entry_tex = find_entry_tex(manuscript_dir, preferred_name=entry_name)
    ename = entry_name or (entry_tex.name if entry_tex else "main.tex")
    hist = history or discover_manuscript_history_paths(git_root, manuscript_dir, ename)

    try:
        for cand_dir in hist["dirs"]:
            tree_spec = f"{ref}:{cand_dir}" if cand_dir else ref
            res = run_cmd(["git", "ls-tree", "--name-only", tree_spec], cwd=git_root, timeout=10)
            if res.returncode != 0:
                continue
            names = {line.strip() for line in res.stdout.splitlines() if line.strip()}
            if cand_dir:
                if ename in names or any(n.lower().endswith(".tex") for n in names):
                    return cand_dir
            else:
                if ename in names or bool(names & set(hist["root_files"])) or any(
                    n.lower().endswith(".tex") and classify_companion_role(n) == "Companion" for n in names
                ):
                    return ""

        full_res = run_cmd(["git", "ls-tree", "-r", "--name-only", ref], cwd=git_root, timeout=10)
        if full_res.returncode != 0:
            return None
        tex_paths = [
            p.strip()
            for p in full_res.stdout.splitlines()
            if p.strip().lower().endswith(".tex") and classify_companion_role(Path(p.strip()).name) == "Companion"
        ]
        if not tex_paths:
            return None
        folder_lower = manuscript_dir.name.lower()
        exact_folder = [p for p in tex_paths if Path(_posix_parent(p)).name.lower() == folder_lower]
        for pool in (exact_folder, tex_paths):
            with_ename = [p for p in pool if Path(p).name.lower() == ename.lower()]
            if len(with_ename) == 1:
                return _posix_parent(with_ename[0])
            if pool is exact_folder and pool:
                return _posix_parent(sorted(pool, key=lambda x: (Path(x).name.lower() != "main.tex", len(x)))[0])
        if len(tex_paths) == 1:
            return _posix_parent(tex_paths[0])
    except OSError:
        return None
    return None


def verify_git_baseline_ref(ws: WorkspacePaths, manuscript_name: str, ref: str) -> dict:
    """Validate an arbitrary Git ref (hash/branch/tag) and verify manuscript presence in that commit."""
    clean_ref = ref.strip()
    if not ws.git_root:
        raise ValueError("当前工作区未绑定 Git 仓库")
    if not clean_ref or clean_ref.startswith("-"):
        raise ValueError("请输入有效的 Git 提交哈希、分支或 Tag")

    show_res = run_cmd(
        ["git", "show", "-s", "--date=short", "--pretty=format:%h%x00%H%x00%ad%x00%s", f"{clean_ref}^{{commit}}"],
        cwd=ws.git_root,
        timeout=10,
    )
    if show_res.returncode != 0 or not show_res.stdout.strip():
        raise ValueError(f"未找到 Git 引用: {clean_ref}")

    parsed = _parse_commit_lines(show_res.stdout, kind="custom")
    if not parsed:
        raise ValueError(f"无法解析 Git 引用: {clean_ref}")
    item = parsed[0]
    manuscript_dir = (ws.manuscripts_dir / manuscript_name).resolve()
    entry_tex = find_entry_tex(manuscript_dir)
    ename = entry_tex.name if entry_tex else "main.tex"
    tree = resolve_manuscript_tree_at_ref(ws.git_root, item["commit"], manuscript_dir, ename)
    if tree is None:
        raise ValueError(f"提交 '{clean_ref}' 中未找到稿件 {manuscript_name} 的 LaTeX 源文件")

    item["ref"] = clean_ref if len(clean_ref) <= 24 else item["ref"]
    item["label"] = f"{item['ref']} ({item['date']}) {item['label'].split(' ', 2)[2]}"
    return item


def list_baselines(ws: WorkspacePaths, manuscript_name: str, include_all_commits: bool = False) -> dict:
    """Return _archive and Git baselines across directory relocations, optionally including repo-wide commits."""
    archives = list_archive_baselines(ws.manuscripts_dir)
    git_baselines: list[dict] = []
    meta = {"tracked_paths": [], "relocated": False, "all_commits": include_all_commits}

    if not ws.git_root:
        return {"archive": archives, "git": git_baselines, "meta": meta}

    manuscript_dir = (ws.manuscripts_dir / manuscript_name).resolve()
    try:
        manuscript_dir.relative_to(ws.git_root)
    except ValueError:
        return {"archive": archives, "git": git_baselines, "meta": meta}

    entry_tex = find_entry_tex(manuscript_dir)
    ename = entry_tex.name if entry_tex else "main.tex"
    hist = discover_manuscript_history_paths(ws.git_root, manuscript_dir, ename)
    meta["tracked_paths"] = [d or "." for d in hist["dirs"]]
    meta["relocated"] = len(hist["dirs"]) > 1

    pathspecs = [d for d in hist["dirs"] if d]
    if "" in hist["dirs"]:
        pathspecs.extend(hist["root_files"] or [":(top)*.tex", ":(top)*.bib"])

    try:
        tags_res = run_cmd(["git", "tag", "-l", "--sort=-creatordate"], cwd=ws.git_root, timeout=10)
        if tags_res.returncode == 0:
            for tag in tags_res.stdout.splitlines()[:20]:
                tag = tag.strip()
                if not tag or tag.startswith("-"):
                    continue
                if resolve_manuscript_tree_at_ref(ws.git_root, tag, manuscript_dir, ename, history=hist) is not None:
                    git_baselines.append({"type": "git", "ref": tag, "kind": "tag", "label": f"Tag: {tag}"})

        scoped_commits: list[dict] = []
        earliest_ms_commit = ""
        if pathspecs:
            log_res = run_cmd(
                ["git", "log", "-n", "50", "--date=short", "--pretty=format:%h%x00%H%x00%ad%x00%s", "--", *pathspecs],
                cwd=ws.git_root,
                timeout=10,
            )
            if log_res.returncode == 0:
                scoped_commits = _parse_commit_lines(log_res.stdout, kind="commit")

            earliest_res = run_cmd(
                ["git", "log", "--reverse", "--pretty=format:%H", "--", *pathspecs],
                cwd=ws.git_root,
                timeout=10,
            )
            if earliest_res.returncode == 0:
                earliest_lines = [l.strip() for l in earliest_res.stdout.splitlines() if l.strip()]
                if earliest_lines:
                    earliest_ms_commit = earliest_lines[0]

        if include_all_commits:
            scoped_hashes = {c["commit"] for c in scoped_commits}
            all_res = run_cmd(
                ["git", "log", "-n", "60", "--date=short", "--pretty=format:%h%x00%H%x00%ad%x00%s"],
                cwd=ws.git_root,
                timeout=10,
            )
            if all_res.returncode == 0:
                for item in _parse_commit_lines(all_res.stdout, kind="commit"):
                    if item["commit"] not in scoped_hashes:
                        item["label"] = f"[全库] {item['label']}"
                    git_baselines.append(item)
                    if earliest_ms_commit and item["commit"] == earliest_ms_commit:
                        break
        else:
            git_baselines.extend(scoped_commits)
    except OSError:
        pass

    return {"archive": archives, "git": git_baselines, "meta": meta}
