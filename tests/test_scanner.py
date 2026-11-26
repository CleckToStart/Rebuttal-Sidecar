"""Tests for manuscript scanner and baseline discovery."""

from pathlib import Path

from proc_utils import run_cmd
from scanner import find_entry_tex, list_baselines, resolve_workspace, scan_workspace


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
