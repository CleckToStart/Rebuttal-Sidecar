"""Tests for atomic .rebuttal_todo.json CRUD and response export."""

from pathlib import Path

from todo_manager import TODO_FILENAME, TodoManager


def test_todo_crud_atomic_write_and_export(tmp_path: Path) -> None:
    mgr = TodoManager()
    initial = mgr.load(tmp_path)
    assert initial == {
        "manuscript": tmp_path.name,
        "active_baseline": None,
        "engine": "auto",
        "items": [],
    }

    doc = mgr.add_item(
        tmp_path,
        {
            "reviewer": "Reviewer #1",
            "summary": "Add ablation table",
            "target_location": "Section 4.3, Page 6",
            "response_draft": "We added Table 3.",
        },
    )
    assert len(doc["items"]) == 1
    assert doc["items"][0]["id"] == "R1-C1"
    assert (tmp_path / TODO_FILENAME).is_file()
    assert not (tmp_path / f"{TODO_FILENAME}.tmp").exists()
    assert mgr.was_written_recently(tmp_path / TODO_FILENAME)

    doc = mgr.update_item(tmp_path, "R1-C1", {"status": "agent_done"})
    assert doc["items"][0]["status"] == "agent_done"

    doc = mgr.set_preferences(
        tmp_path,
        active_baseline={"type": "git", "ref": "v1-sub"},
        engine="xelatex",
    )
    assert doc["active_baseline"] == {"type": "git", "ref": "v1-sub"}
    assert doc["engine"] == "xelatex"

    md = mgr.export_response_letter(tmp_path, fmt="md")
    assert "## Reviewer #1" in md
    assert "### [R1-C1] Add ablation table" in md
    assert "We added Table 3." in md

    tex = mgr.export_response_letter(tmp_path, fmt="tex")
    assert "\\subsection*{Reviewer #1}" in tex
    assert "\\paragraph{[R1-C1] Add ablation table" in tex

    doc = mgr.delete_item(tmp_path, "R1-C1")
    assert doc["items"] == []
