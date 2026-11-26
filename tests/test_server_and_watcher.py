"""Tests for debounced watcher (auto-compile off by default) and multi-threaded HTTP/SSE server."""

import json
import threading
import time
import urllib.request
from pathlib import Path

from config import AppConfig
from server import SidecarApp
from watcher import ManuscriptWatcher


def test_watcher_respects_auto_compile_flag_and_debounces(tmp_path: Path) -> None:
    manuscript = tmp_path / "eswa_mainline"
    manuscript.mkdir()
    tex_file = manuscript / "main.tex"
    tex_file.write_text("\\documentclass{article}", encoding="utf-8")

    compile_calls: list[str] = []
    dirty_calls: list[tuple[str, str]] = []
    todo_calls: list[str] = []

    watcher = ManuscriptWatcher(
        on_auto_compile=lambda m: compile_calls.append(m),
        on_source_dirty=lambda m, f: dirty_calls.append((m, f)),
        on_todo_changed=lambda m: todo_calls.append(m),
        was_self_todo_write=lambda p: False,
    )
    try:
        # Default auto_compile=False: editing .tex marks dirty but triggers 0 compiles
        watcher.configure(tmp_path, "eswa_mainline", auto_compile=False, debounce_ms=300)
        time.sleep(0.25)
        tex_file.write_text("\\documentclass{article}\n% edit 1", encoding="utf-8")
        (manuscript / "main.aux").write_text("ignored", encoding="utf-8")
        time.sleep(0.55)
        assert compile_calls == []
        assert any(f == "main.tex" for _, f in dirty_calls)

        # Turn ON auto_compile: rapid saves coalesce into 1 compile call
        watcher.update_options(auto_compile=True, debounce_ms=300)
        for idx in range(4):
            tex_file.write_text(f"\\documentclass{{article}}\n% burst {idx}", encoding="utf-8")
            time.sleep(0.05)
        time.sleep(0.6)
        assert len(compile_calls) == 1

        # External .rebuttal_todo.json edit syncs regardless
        (manuscript / ".rebuttal_todo.json").write_text('{"items":[]}', encoding="utf-8")
        time.sleep(0.45)
        assert len(todo_calls) >= 1
    finally:
        watcher.stop()


def test_server_settings_todo_crud_and_nonblocking_sse(tmp_path: Path) -> None:
    paper = tmp_path / "docs" / "manuscripts" / "eswa_mainline"
    paper.mkdir(parents=True)
    (paper / "main.tex").write_text("\\documentclass{article}\n\\begin{document}Hi\\end{document}\n", encoding="utf-8")

    cfg = AppConfig(path=tmp_path / "app_config.json")
    app = SidecarApp(config=cfg, initial_workspace=str(tmp_path))
    port = app.start(port=0)
    base = f"http://127.0.0.1:{port}"

    received_events: list[str] = []
    sse_ready = threading.Event()
    stop_sse = threading.Event()

    def read_sse() -> None:
        with urllib.request.urlopen(f"{base}/api/events", timeout=5) as resp:
            sse_ready.set()
            while not stop_sse.is_set():
                line = resp.readline().decode("utf-8", errors="replace")
                if not line:
                    break
                if line.startswith("event:"):
                    received_events.append(line.strip())
                    if "TODO_UPDATED" in line:
                        break

    t = threading.Thread(target=read_sse, daemon=True)
    t.start()
    assert sse_ready.wait(timeout=3)

    try:
        # Check default auto_compile is False while SSE stream is actively open
        with urllib.request.urlopen(f"{base}/api/settings", timeout=3) as resp:
            settings = json.loads(resp.read().decode("utf-8"))
        assert settings["compiler"]["auto_compile"] is False

        # Update compiler settings
        req = urllib.request.Request(
            f"{base}/api/settings",
            data=json.dumps({"auto_compile": True, "default_engine": "xelatex", "extra_args": "-shell-escape"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            updated = json.loads(resp.read().decode("utf-8"))
        assert updated["compiler"]["auto_compile"] is True
        assert updated["compiler"]["default_engine"] == "xelatex"
        assert updated["compiler"]["extra_args"] == "-shell-escape"
        assert app.watcher.auto_compile is True

        # Toolchain check endpoint
        with urllib.request.urlopen(f"{base}/api/settings/compiler/check", timeout=3) as resp:
            tc = json.loads(resp.read().decode("utf-8"))
        assert "xelatex" in tc["tools"]

        # Create Todo item and verify SSE broadcast does not block
        req_todo = urllib.request.Request(
            f"{base}/api/todos?manuscript=eswa_mainline",
            data=json.dumps({"reviewer": "Reviewer #1", "summary": "Clarify Fig 2"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_todo, timeout=3) as resp:
            todo_doc = json.loads(resp.read().decode("utf-8"))
        assert todo_doc["items"][0]["id"] == "R1-C1"

        t.join(timeout=3)
        assert any("TODO_UPDATED" in ev for ev in received_events)
    finally:
        stop_sse.set()
        app.stop()


def test_demo_workspace_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = AppConfig(path=tmp_path / "app_config.json")
    app = SidecarApp(config=cfg)
    port = app.start(port=0)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/workspace/demo", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["active_manuscript"] == "demo_paper"
        assert len(payload["manuscripts"]) == 1
    finally:
        app.stop()


def test_workspace_history_remember_manuscript_pin_and_delete(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo_a"
    for name in ("paper1", "paper2"):
        p = repo_a / "docs" / "manuscripts" / name
        p.mkdir(parents=True)
        (p / "main.tex").write_text("\\documentclass{article}\\begin{document}Hi\\end{document}", encoding="utf-8")

    repo_b = tmp_path / "repo_b"
    pb = repo_b / "docs" / "manuscripts" / "paper_b"
    pb.mkdir(parents=True)
    (pb / "main.tex").write_text("\\documentclass{article}\\begin{document}B\\end{document}", encoding="utf-8")

    cfg = AppConfig(path=tmp_path / "cfg.json")
    app = SidecarApp(config=cfg, initial_workspace=str(repo_a))
    port = app.start(port=0)
    base = f"http://127.0.0.1:{port}"
    try:
        # Select paper2 in repo_a
        urllib.request.urlopen(f"{base}/api/todos?manuscript=paper2", timeout=3).read()

        # Switch to repo_b, then back to repo_a and verify paper2 is remembered
        for target in (repo_b, repo_a):
            req = urllib.request.Request(
                f"{base}/api/workspace/open",
                data=json.dumps({"path": str(target)}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            payload = json.loads(urllib.request.urlopen(req, timeout=3).read().decode("utf-8"))
        assert payload["active_manuscript"] == "paper2"
        assert len(payload["workspace_history"]) == 2

        # Pin repo_b so it moves ahead of repo_a
        req_pin = urllib.request.Request(
            f"{base}/api/workspace/history",
            data=json.dumps({"path": str(repo_b), "pinned": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        pinned_res = json.loads(urllib.request.urlopen(req_pin, timeout=3).read().decode("utf-8"))
        assert pinned_res["workspace_history"][0]["path"] == str(repo_b.resolve())
        assert pinned_res["workspace_history"][0]["pinned"] is True

        # Remove repo_b from history
        req_del = urllib.request.Request(
            f"{base}/api/workspace/history",
            data=json.dumps({"path": str(repo_b)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        del_res = json.loads(urllib.request.urlopen(req_del, timeout=3).read().decode("utf-8"))
        assert [h["path"] for h in del_res["workspace_history"]] == [str(repo_a.resolve())]
    finally:
        app.stop()
