"""Tests for manuscript scanner and baseline discovery."""

from pathlib import Path

from proc_utils import run_cmd
from scanner import find_entry_tex, list_baselines, resolve_workspace, scan_workspace, verify_git_baseline_ref


def _init_git_repo(repo_dir: Path) -> None:
    run_cmd(["git", "init"], cwd=repo_dir)
    run_cmd(["git", "config", "user.name", "Test User"], cwd=repo_dir)
    run_cmd(["git", "config", "user.email", "test@example.com"], cwd=repo_dir)


def test_scan_workspace_filters_and_discovers(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    manuscripts = tmp_path / "docs" / "manuscripts"
    manuscripts.mkdir(parents=True)

    # Active manuscript
    eswa = manuscripts / "eswa_mainline"
    eswa.mkdir()
    (eswa / "main.tex").write_text("\\documentclass{article}\n\\begin{document}Hi\\end{document}\n", encoding="utf-8")

    # Archive snapshot
    arch = manuscripts / "_archive" / "eswa_v1"
    arch.mkdir(parents=True)
    (arch / "main.tex").write_text("\\documentclass{article}\n\\begin{document}Old\\end{document}\n", encoding="utf-8")

    # Ignored by prefix
    (manuscripts / ".hidden_dir").mkdir()
    (manuscripts / ".hidden_dir" / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
    (manuscripts / "_scratch").mkdir()
    (manuscripts / "_scratch" / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")

    # Ignored by .gitignore
    (tmp_path / ".gitignore").write_text("docs/manuscripts/vendor_template/\n", encoding="utf-8")
    vendor = manuscripts / "vendor_template"
    vendor.mkdir()
    (vendor / "template.tex").write_text("\\documentclass{article}", encoding="utf-8")

    # Folder without \documentclass (only commented out)
    notes = manuscripts / "notes_only"
    notes.mkdir()
    (notes / "draft.tex").write_text("% \\documentclass{article}\nJust notes\n", encoding="utf-8")

    result = scan_workspace(str(tmp_path))
    names = [m["name"] for m in result["manuscripts"]]
    assert names == ["eswa_mainline"]
    assert result["manuscripts"][0]["entry_tex"] == "main.tex"
    assert [a["ref"] for a in result["archive_baselines"]] == ["eswa_v1"]
    assert find_entry_tex(notes) is None


def test_list_baselines_returns_git_tags_and_commits(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    eswa = tmp_path / "docs" / "manuscripts" / "eswa_mainline"
    eswa.mkdir(parents=True)
    (eswa / "main.tex").write_text("\\documentclass{article}\n\\begin{document}v1\\end{document}\n", encoding="utf-8")

    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "v1 submitted"], cwd=tmp_path)
    run_cmd(["git", "tag", "paper/eswa/v1-sub"], cwd=tmp_path)

    (eswa / "main.tex").write_text("\\documentclass{article}\n\\begin{document}v2\\end{document}\n", encoding="utf-8")
    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "v2 revision"], cwd=tmp_path)

    ws = resolve_workspace(str(tmp_path / "docs" / "manuscripts"))
    baselines = list_baselines(ws, "eswa_mainline")
    git_items = baselines["git"]
    tags = [b["ref"] for b in git_items if b["kind"] == "tag"]
    commits = [b for b in git_items if b["kind"] == "commit"]

    assert "paper/eswa/v1-sub" in tags
    assert len(commits) == 2
    assert "v2 revision" in commits[0]["label"]


def test_scan_workspace_accepts_direct_single_paper_folder(tmp_path: Path) -> None:
    paper = tmp_path / "my_paper"
    paper.mkdir()
    (paper / "main.tex").write_text("\\documentclass{article}\n\\begin{document}Single\\end{document}\n", encoding="utf-8")
    sibling = tmp_path / "unrelated_paper"
    sibling.mkdir()
    (sibling / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")

    result = scan_workspace(str(paper))
    assert [m["name"] for m in result["manuscripts"]] == ["my_paper"]
    assert result["project_root"] == str(paper.resolve())


def test_list_baselines_tracks_folder_relocation_file_split_and_all_commits(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    legacy = tmp_path / "legacy_ipm"
    legacy.mkdir()
    (legacy / "paper.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nBaseline manuscript draft for IPM.\n\\end{document}\n",
        encoding="utf-8",
    )
    (legacy / "refs.bib").write_text("@article{a, title={A}}\n", encoding="utf-8")
    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "initial ipm submission draft"], cwd=tmp_path)
    run_cmd(["git", "tag", "v0.1"], cwd=tmp_path)

    # Commit 2 at legacy path
    (legacy / "paper.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nExpanded experimental discussion for IPM.\n\\end{document}\n",
        encoding="utf-8",
    )
    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "expand ipm experiments"], cwd=tmp_path)

    # Commit 3: relocate to docs/manuscripts/ipm_manuscript, split paper.tex -> main.tex + sections/intro.tex, add companions
    new_dir = tmp_path / "docs" / "manuscripts" / "ipm_manuscript"
    (new_dir / "sections").mkdir(parents=True)
    (new_dir / "sections" / "intro.tex").write_text("Expanded experimental discussion for IPM.\n", encoding="utf-8")
    (new_dir / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n\\end{document}\n",
        encoding="utf-8",
    )
    (new_dir / "title_page.tex").write_text("\\documentclass{article}\n\\begin{document}Title\\end{document}\n", encoding="utf-8")
    (new_dir / "highlights.tex").write_text("\\documentclass{article}\n\\begin{document}HL\\end{document}\n", encoding="utf-8")
    (new_dir / "main_zh.tex").write_text("\\documentclass{article}\n\\begin{document}ZH\\end{document}\n", encoding="utf-8")
    (new_dir / "refs.bib").write_text("@article{a, title={A}}\n", encoding="utf-8")
    run_cmd(["git", "rm", "-rf", "legacy_ipm"], cwd=tmp_path)
    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "chore(docs): relocate IPM manuscript under docs/manuscripts"], cwd=tmp_path)

    # Commit 4: non-TeX code commit
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "model.py").write_text("def forward(x):\n    return x\n", encoding="utf-8")
    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "feat: improve KGQA retriever"], cwd=tmp_path)

    scanned = scan_workspace(str(tmp_path))
    ms = scanned["manuscripts"][0]
    assert ms["entry_tex"] == "main.tex"
    companions = {c["name"]: c["role"] for c in ms["companions"]}
    assert companions["main_zh.tex"] == "Translation"

    ws = resolve_workspace(str(tmp_path))
    scoped = list_baselines(ws, "ipm_manuscript", include_all_commits=False)
    assert scoped["meta"]["relocated"] is True
    scoped_tags = [b["ref"] for b in scoped["git"] if b["kind"] == "tag"]
    scoped_labels = [b["label"] for b in scoped["git"] if b["kind"] == "commit"]
    assert "v0.1" in scoped_tags
    assert len(scoped_labels) == 3
    assert any("relocate IPM manuscript" in lbl for lbl in scoped_labels)
    assert any("expand ipm experiments" in lbl for lbl in scoped_labels)
    assert any("initial ipm submission draft" in lbl for lbl in scoped_labels)

    repo_wide = list_baselines(ws, "ipm_manuscript", include_all_commits=True)
    all_labels = [b["label"] for b in repo_wide["git"] if b["kind"] == "commit"]
    assert len(all_labels) == 4
    assert any("improve KGQA retriever" in lbl for lbl in all_labels)

    verified = verify_git_baseline_ref(ws, "ipm_manuscript", "HEAD~2")
    assert verified["type"] == "git"
    assert verified["ref"] == "HEAD~2"
