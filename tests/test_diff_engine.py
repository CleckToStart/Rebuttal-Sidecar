"""Tests for TeX flattener, compiler config resolution, and sandboxed Clean+Diff build."""

from pathlib import Path

from diff_engine import (
    _parse_extra_args,
    builtin_tex_diff,
    detect_tex_engine,
    flatten_tex,
    parse_latex_errors,
    run_compile_job,
)
from proc_utils import check_toolchain, run_cmd
from scanner import resolve_workspace


def test_flatten_tex_handles_nested_input_comments_and_cycles(tmp_path: Path) -> None:
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "intro.tex").write_text("Hello from intro.\n\\input{sections/nested}\n", encoding="utf-8")
    (sections / "nested.tex").write_text("Nested text.\n\\input{main}\n", encoding="utf-8")
    main = tmp_path / "main.tex"
    main.write_text(
        "\\documentclass{article}\n"
        "% \\input{sections/does_not_exist}\n"
        "\\begin{document}\n"
        "\\input{sections/intro}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    flat = flatten_tex(main, tmp_path)
    assert "Hello from intro." in flat
    assert "Nested text." in flat
    assert "Skipped circular include: main.tex" in flat
    assert "% \\input{sections/does_not_exist}" in flat


def test_detect_tex_engine_and_error_parser() -> None:
    assert detect_tex_engine("\\usepackage{ctex}", preferred="auto", default_engine="auto") == "xelatex"
    assert detect_tex_engine("% !TEX program = lualatex\n\\documentclass{article}") == "lualatex"
    assert detect_tex_engine("\\usepackage{ctex}", preferred="pdflatex", default_engine="xelatex") == "pdflatex"
    assert detect_tex_engine("\\documentclass{article}", preferred="auto", default_engine="lualatex") == "lualatex"
    assert _parse_extra_args('-shell-escape "--synctex=1"') == ["-shell-escape", "--synctex=1"]

    errs = parse_latex_errors("./main.tex:42: Undefined control sequence.\n! LaTeX Error: Missing \\begin{document}.")
    assert errs[0] == {"file": "./main.tex", "line": 42, "message": "Undefined control sequence."}


def test_builtin_tex_diff_highlights_prose_and_preserves_commands() -> None:
    old_tex = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\author{Alice\\\\Bob}\n"
        "Rank \\& Sort improves 50\\% at \\$10 \\cite{a,b}.\n"
        "\\caption{Score $p$ ($q_i$ in text).}\n"
        "\\begin{tabular}{ll}A & B\\\\\\end{tabular}\n"
        "Initial baseline text.\n"
        "\\end{document}\n"
    )
    new_tex = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\author{Alice\\\\Bob}\n"
        "Rank \\& Sort improves 50\\% at \\$10 \\cite{a,b}.\n"
        "\\caption{Updated score $p$ ($q_i$ in text).}\n"
        "\\begin{tabular}{ll}A & B\\\\\\end{tabular}\n"
        "Updated rebuttal text.\n"
        "\\end{document}\n"
    )
    diff = builtin_tex_diff(old_tex, new_tex)
    assert "\\DIFdel{" in diff
    assert "\\DIFadd{" in diff
    assert "\\cite{a,b}" in diff
    assert "Rank \\& Sort" in diff
    assert "Rank & Sort" not in diff
    assert "Alice\\\\Bob" in diff
    assert "50\\% at \\$10" in diff
    assert "\\begin{tabular}{ll}A & B\\\\\\end{tabular}" in diff
    assert diff.count("{") == diff.count("}")
    assert "\\IfFileExists{ulem.sty}" in diff


def test_end_to_end_sandboxed_clean_and_diff_compilation(tmp_path: Path) -> None:
    tools = check_toolchain()["tools"]
    if not tools["pdflatex"]["available"]:
        return

    run_cmd(["git", "init"], cwd=tmp_path)
    run_cmd(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    run_cmd(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)

    manuscripts = tmp_path / "docs" / "manuscripts"
    paper = manuscripts / "demo_paper"
    sections = paper / "sections"
    sections.mkdir(parents=True)

    (manuscripts / "references.bib").write_text(
        "@article{smith2024, title={Demo}, author={Smith}, journal={Test}, year={2024}}\n",
        encoding="utf-8",
    )
    (sections / "intro.tex").write_text(
        "Rank \\& Sort initial baseline text \\cite{smith2024}.\n",
        encoding="utf-8",
    )
    (paper / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\author{Author One\\\\Line Two}\n"
        "\\begin{document}\n"
        "\\input{sections/intro}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{../references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    run_cmd(["git", "add", "."], cwd=tmp_path)
    run_cmd(["git", "commit", "-m", "v1 submitted"], cwd=tmp_path)
    run_cmd(["git", "tag", "v1"], cwd=tmp_path)

    # Modify section in working tree without polluting paper/ with diff.tex or .aux
    (sections / "intro.tex").write_text(
        "Rank \\& Sort updated rebuttal text with new evidence \\cite{smith2024}.\n",
        encoding="utf-8",
    )

    ws = resolve_workspace(str(tmp_path))
    result = run_compile_job(
        ws=ws,
        manuscript_name="demo_paper",
        baseline={"type": "git", "ref": "v1"},
        preferred_engine="pdflatex",
        compiler_settings={"recipe": "auto", "compile_timeout": 120},
    )

    assert Path(result["clean_pdf"]).is_file()
    assert Path(result["diff_pdf"]).is_file()
    # Ensure host manuscript directory remains completely clean (no .aux/.log/diff.tex)
    assert not (paper / "main.aux").exists()
    assert not (paper / "diff.tex").exists()


def test_compile_coordinator_non_deadlocking_enqueue_and_error(tmp_path: Path) -> None:
    import time
    from diff_engine import CompileCoordinator

    events: list[str] = []
    coord = CompileCoordinator(on_event=lambda ev, _: events.append(ev))
    try:
        coord.mark_dirty("missing_paper", "main.tex")
        assert coord.get_status("missing_paper")["dirty"] is True

        ws = resolve_workspace(str(tmp_path))
        coord.enqueue(ws, "missing_paper", None, "pdflatex", {})
        for _ in range(20):
            st = coord.get_status("missing_paper")
            if st["state"] == "error":
                break
            time.sleep(0.05)
        assert coord.get_status("missing_paper")["state"] == "error"
        assert "COMPILE_START" in events and "COMPILE_ERROR" in events
    finally:
        coord.stop()
