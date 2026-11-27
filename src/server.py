"""Multi-threaded local Bottle HTTP API and SSE event broker."""

from __future__ import annotations

import json
import mimetypes
import queue
import socketserver
import threading
import wsgiref.simple_server
from pathlib import Path
from typing import Callable

from bottle import Bottle, HTTPResponse, request, response

from companion_templates import COMPANION_TEMPLATES, create_companion_file, read_companion_content
from config import AppConfig
from demo_workspace import ensure_demo_workspace
from diff_engine import CompileCoordinator
from docx_engine import export_markdown_to_docx
from patent_manager import IMAGE_SUFFIXES, PatentManager, ensure_demo_patent
from patent_timeline import (
    collect_all_patent_alerts,
    get_windows_reminder_task_status,
    run_reminder_check,
    send_smtp_reminder,
    set_windows_reminder_task,
)
from patent_vcs import (
    create_or_bind_gitea_repo,
    create_snapshot,
    list_snapshots,
    sync_gitea,
    test_gitea_connection,
)
from proc_utils import check_toolchain, scan_local_tex_installations
from scanner import WorkspacePaths, list_baselines, resolve_workspace, scan_workspace, verify_git_baseline_ref
from tex_installer import CTAN_MIRRORS, TexInstallerManager
from todo_manager import TodoManager
from watcher import ManuscriptWatcher, PatentWatcher

MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
}


class ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class QuietWSGIHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


class SSEBroker:
    """Thread-safe Server-Sent Events pub/sub broker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


def _json_response(payload: dict, status: int = 200) -> HTTPResponse:
    return HTTPResponse(
        body=json.dumps(payload, ensure_ascii=False),
        status=status,
        headers={"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
    )


def _qstr(key: str, default: str = "") -> str:
    """Decode UTF-8 query parameter reliably under Bottle WSGI latin-1 defaults."""
    val = request.query.getunicode(key)
    return val.strip() if val is not None else default


def _utf8_route_param(raw: str) -> str:
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


class SidecarApp:
    """Coordinates workspace state, Todo CRUD, compiler queue, watcher, and HTTP server."""

    def __init__(
        self,
        config: AppConfig | None = None,
        web_dir: Path | None = None,
        initial_workspace: str = "",
        folder_dialog_fn: Callable[[], str | None] | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.web_dir = web_dir or (Path(__file__).resolve().parent.parent / "web")
        self.folder_dialog_fn = folder_dialog_fn
        self.file_dialog_fn: Callable[[], str | None] | None = None

        self.broker = SSEBroker()
        self.todo_mgr = TodoManager()
        self.patent_mgr = PatentManager()
        self.coordinator = CompileCoordinator(on_event=self._on_compile_event)
        self.tex_installer = TexInstallerManager(
            on_event=self._on_compile_event,
            on_bin_detected=self._on_tex_bin_detected,
        )
        self.watcher = ManuscriptWatcher(
            on_auto_compile=self._on_watcher_auto_compile,
            on_source_dirty=self._on_watcher_dirty,
            on_todo_changed=self._on_watcher_todo,
            was_self_todo_write=self.todo_mgr.was_written_recently,
        )
        self.patent_watcher = PatentWatcher(
            on_file_changed=self._on_patent_file_changed,
            on_data_changed=self._on_patent_data_changed,
            was_self_write=self.patent_mgr.was_written_recently,
        )

        self._state_lock = threading.Lock()
        self.workspace: WorkspacePaths | None = None
        self.workspace_info: dict = self._empty_workspace_info()
        self.active_manuscript: str = ""
        self.active_patent_dir: str = ""

        self._httpd: ThreadingWSGIServer | None = None
        self._server_thread: threading.Thread | None = None
        self.port: int = 0

        self.bottle = Bottle()
        self._register_routes()

        startup_ws = initial_workspace or self.config.last_workspace
        if startup_ws and Path(startup_ws).is_dir():
            try:
                self.open_workspace(startup_ws, emit_event=False)
            except OSError:
                pass

        if self.config.last_patent and Path(self.config.last_patent).is_dir():
            try:
                self.open_patent(self.config.last_patent, emit_event=False)
            except OSError:
                pass

    def _empty_workspace_info(self) -> dict:
        return {
            "project_root": "",
            "manuscripts_dir": "",
            "git_root": "",
            "manuscripts": [],
            "archive_baselines": [],
        }

    def _on_compile_event(self, event: str, payload: dict) -> None:
        self.broker.publish(event, payload)

    def _on_tex_bin_detected(self, bin_dir: str) -> None:
        if not bin_dir or self.config.compiler.get("tex_bin_dir"):
            return
        updated = self.config.update_compiler({"tex_bin_dir": bin_dir})
        self.broker.publish("SETTINGS_UPDATED", {"compiler": updated})

    def _on_watcher_dirty(self, manuscript: str, filename: str) -> None:
        self.coordinator.mark_dirty(manuscript, filename)
        self.broker.publish("FILES_CHANGED", {"manuscript": manuscript, "file": filename})

    def _on_watcher_auto_compile(self, manuscript: str) -> None:
        self.trigger_compile(manuscript)

    def _on_watcher_todo(self, manuscript: str) -> None:
        with self._state_lock:
            ws = self.workspace
        if not ws:
            return
        doc = self.todo_mgr.load(ws.manuscripts_dir / manuscript)
        self.broker.publish("TODO_UPDATED", doc)

    def _on_patent_file_changed(self, filename: str) -> None:
        self.broker.publish("PATENT_FILES_CHANGED", {"file": filename})

    def _on_patent_data_changed(self, filename: str) -> None:
        if not self.active_patent_dir:
            return
        self.broker.publish("PATENT_UPDATED", self.get_patent_state_payload())

    def _active_patent_path(self) -> Path:
        with self._state_lock:
            p = self.active_patent_dir
        if not p or not Path(p).is_dir():
            raise ValueError("请先新建或打开一个专利项目")
        return Path(p).resolve()

    def _reminders_view(self) -> dict:
        rem = self.config.reminders
        rem["task_supported"] = hasattr(self, "folder_dialog_fn") or True
        try:
            rem["task_registered"] = get_windows_reminder_task_status()
        except OSError:
            rem["task_registered"] = False
        return rem

    def get_patent_state_payload(self) -> dict:
        with self._state_lock:
            pdir = self.active_patent_dir
        if pdir and Path(pdir).is_dir():
            core = self.patent_mgr.get_patent_payload(
                Path(pdir),
                advance_days=int(self.config.reminders.get("advance_days", 30)),
            )
        else:
            core = {
                "patent_dir": "",
                "meta": {},
                "feedback": {"patent": "", "raw_batches": [], "items": []},
                "documents": [],
                "figures": [],
                "schedule": {"active_alerts": [], "fee_schedule": []},
                "snapshots": [],
                "remote_url": "",
                "numeral_check": {"items": [], "undeclared": [], "missing_count": 0},
            }
        return {
            **core,
            "patent_history": self.config.patent_history,
            "gitea": self.config.gitea,
            "reminders": self._reminders_view(),
            "alerts": collect_all_patent_alerts(self.config),
        }

    def open_patent(
        self,
        path_str: str,
        emit_event: bool = True,
        title: str = "",
        patent_type: str = "invention",
    ) -> dict:
        payload = self.patent_mgr.init_or_scan_patent(
            path_str,
            title=title,
            patent_type=patent_type,
            advance_days=int(self.config.reminders.get("advance_days", 30)),
        )
        pdir = payload["patent_dir"]
        with self._state_lock:
            self.active_patent_dir = pdir

        meta = payload["meta"]
        self.config.record_patent(
            pdir,
            name=str(meta.get("title") or Path(pdir).name),
            patent_type=str(meta.get("patent_type") or "invention"),
            stage=str(meta.get("stage") or "drafting"),
        )
        self.patent_watcher.configure(Path(pdir))
        full = self.get_patent_state_payload()
        if emit_event:
            self.broker.publish("PATENT_UPDATED", full)
        return full

    def _manuscript_dir(self, name: str) -> Path:
        with self._state_lock:
            ws = self.workspace
        if not ws:
            raise ValueError("No workspace opened")
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"Invalid manuscript name: {name}")
        target = (ws.manuscripts_dir / name).resolve()
        if not target.is_dir() or target.parent != ws.manuscripts_dir.resolve():
            raise FileNotFoundError(f"Manuscript not found: {name}")
        return target

    def open_workspace(self, path_str: str, emit_event: bool = True, custom_name: str = "") -> dict:
        info = scan_workspace(path_str)
        ws = resolve_workspace(path_str)
        manuscripts = info["manuscripts"]
        remembered = self.config.get_last_manuscript(info["project_root"])
        names = [m["name"] for m in manuscripts]
        active = remembered if remembered in names else (names[0] if names else "")

        with self._state_lock:
            self.workspace = ws
            self.workspace_info = info
            self.active_manuscript = active

        self.config.record_workspace(
            info["project_root"],
            manuscript_count=len(manuscripts),
            active_manuscript=active,
            custom_name=custom_name,
        )
        compiler = self.config.compiler
        self.watcher.configure(
            manuscripts_dir=ws.manuscripts_dir,
            active_manuscript=active,
            auto_compile=compiler["auto_compile"],
            debounce_ms=compiler["debounce_ms"],
        )

        payload = self.get_workspace_payload()
        if emit_event:
            self.broker.publish("WORKSPACE_UPDATED", payload)
        return payload

    def get_workspace_payload(self) -> dict:
        with self._state_lock:
            info = dict(self.workspace_info)
            active = self.active_manuscript
        return {
            **info,
            "active_manuscript": active,
            "recent_workspaces": self.config.recent_workspaces,
            "workspace_history": self.config.workspace_history,
        }

    def trigger_compile(
        self,
        manuscript_name: str,
        baseline: dict | None = ...,
        engine: str | None = None,
        target_tex: str = "",
    ) -> dict:
        manuscript_dir = self._manuscript_dir(manuscript_name)
        todo_doc = self.todo_mgr.load(manuscript_dir)
        resolved_baseline = todo_doc["active_baseline"] if baseline is ... else baseline
        resolved_engine = engine or todo_doc["engine"]
        with self._state_lock:
            ws = self.workspace
        if not ws:
            raise ValueError("No workspace opened")

        return self.coordinator.enqueue(
            ws=ws,
            manuscript_name=manuscript_name,
            baseline=resolved_baseline,
            preferred_engine=resolved_engine,
            compiler_settings=self.config.compiler,
            target_tex=target_tex,
        )

    def _register_routes(self) -> None:
        app = self.bottle

        @app.get("/")
        def index():
            return self._serve_static("index.html")

        @app.get("/api/workspace")
        def get_workspace():
            return _json_response(self.get_workspace_payload())

        @app.post("/api/workspace/open")
        def post_workspace_open():
            body = request.json or {}
            path_str = str(body.get("path", "")).strip()
            custom_name = str(body.get("name", "")).strip()
            if not path_str:
                return _json_response({"error": "Missing workspace path"}, status=400)
            try:
                return _json_response(self.open_workspace(path_str, custom_name=custom_name))
            except FileNotFoundError as exc:
                return _json_response({"error": str(exc)}, status=404)

        @app.post("/api/workspace/pick-path")
        def post_workspace_pick_path():
            if not self.folder_dialog_fn:
                return _json_response({"error": "Native folder dialog unavailable in headless mode"}, status=400)
            try:
                selected = self.folder_dialog_fn()
            except Exception as exc:
                return _json_response({"error": str(exc)}, status=500)
            if not selected:
                return _json_response({"cancelled": True})
            info = scan_workspace(selected)
            return _json_response(
                {
                    "cancelled": False,
                    "path": info["project_root"],
                    "suggested_name": Path(info["project_root"]).name or info["project_root"],
                    "manuscript_count": len(info["manuscripts"]),
                }
            )

        @app.post("/api/workspace/select")
        def post_workspace_select():
            if not self.folder_dialog_fn:
                return _json_response({"error": "Native folder dialog unavailable in headless mode"}, status=400)
            try:
                selected = self.folder_dialog_fn()
            except Exception as exc:
                return _json_response({"error": str(exc)}, status=500)
            if not selected:
                return _json_response({"cancelled": True, **self.get_workspace_payload()})
            return _json_response(self.open_workspace(selected))

        @app.post("/api/workspace/demo")
        def post_workspace_demo():
            demo_root = ensure_demo_workspace()
            return _json_response(self.open_workspace(str(demo_root)))

        @app.patch("/api/workspace/history")
        def patch_workspace_history():
            body = request.json or {}
            path_str = str(body.get("path", "")).strip()
            if not path_str:
                return _json_response({"error": "Missing workspace path"}, status=400)
            if "name" in body:
                self.config.rename_workspace(path_str, str(body["name"]))
            if "pinned" in body:
                self.config.set_workspace_pinned(path_str, bool(body["pinned"]))
            return _json_response({"workspace_history": self.config.workspace_history})

        @app.delete("/api/workspace/history")
        def delete_workspace_history():
            body = request.json or {}
            path_str = str(body.get("path", "")).strip()
            if not path_str:
                return _json_response({"error": "Missing workspace path"}, status=400)
            history = self.config.remove_workspace(path_str)
            return _json_response({"workspace_history": history})

        @app.get("/api/baselines")
        def get_baselines():
            name = _qstr("manuscript")
            all_commits = _qstr("all_commits", "0").lower() in ("1", "true", "yes")
            try:
                self._manuscript_dir(name)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            with self._state_lock:
                ws = self.workspace
            return _json_response(list_baselines(ws, name, include_all_commits=all_commits))

        @app.get("/api/baselines/verify")
        def get_baselines_verify():
            name = _qstr("manuscript")
            ref = _qstr("ref")
            try:
                self._manuscript_dir(name)
                with self._state_lock:
                    ws = self.workspace
                item = verify_git_baseline_ref(ws, name, ref)
                return _json_response({"baseline": item})
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/todos")
        def get_todos():
            name = _qstr("manuscript")
            try:
                mdir = self._manuscript_dir(name)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            with self._state_lock:
                self.active_manuscript = name
                root_path = self.workspace_info.get("project_root", "")
            if root_path:
                self.config.set_last_manuscript(root_path, name)
            self.watcher.set_active_manuscript(name)
            return _json_response(self.todo_mgr.load(mdir))

        @app.post("/api/todos")
        def post_todos():
            name = _qstr("manuscript")
            body = request.json or {}
            try:
                mdir = self._manuscript_dir(name)
                if "items" in body and isinstance(body["items"], list):
                    doc = self.todo_mgr.save(mdir, body)
                else:
                    doc = self.todo_mgr.add_item(mdir, body)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            self.broker.publish("TODO_UPDATED", doc)
            return _json_response(doc)

        @app.patch("/api/todos/<item_id>")
        def patch_todo(item_id: str):
            name = _qstr("manuscript")
            body = request.json or {}
            try:
                mdir = self._manuscript_dir(name)
                doc = self.todo_mgr.update_item(mdir, item_id, body)
            except KeyError as exc:
                return _json_response({"error": str(exc)}, status=404)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            self.broker.publish("TODO_UPDATED", doc)
            return _json_response(doc)

        @app.delete("/api/todos/<item_id>")
        def delete_todo(item_id: str):
            name = _qstr("manuscript")
            try:
                mdir = self._manuscript_dir(name)
                doc = self.todo_mgr.delete_item(mdir, item_id)
            except KeyError as exc:
                return _json_response({"error": str(exc)}, status=404)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            self.broker.publish("TODO_UPDATED", doc)
            return _json_response(doc)

        @app.get("/api/todos/export")
        def export_todos():
            name = _qstr("manuscript")
            fmt = _qstr("format", "md").lower()
            try:
                mdir = self._manuscript_dir(name)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            content = self.todo_mgr.export_response_letter(mdir, fmt=fmt if fmt in ("md", "tex") else "md")
            return _json_response({"manuscript": name, "format": fmt, "content": content})

        @app.put("/api/baseline")
        def put_baseline():
            name = _qstr("manuscript")
            body = request.json or {}
            try:
                mdir = self._manuscript_dir(name)
                baseline = body["active_baseline"] if "active_baseline" in body else ...
                engine = body.get("engine")
                doc = self.todo_mgr.set_preferences(mdir, active_baseline=baseline, engine=engine)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

            self.broker.publish("TODO_UPDATED", doc)
            if body.get("compile_now") is True or self.config.compiler["auto_compile"]:
                self.trigger_compile(name)
            return _json_response(doc)

        @app.get("/api/settings")
        def get_settings():
            return _json_response(
                {
                    "compiler": self.config.compiler,
                    "desktop_shell": bool(self.folder_dialog_fn),
                    "active_workbench": self.config.active_workbench,
                    "patent_alerts": collect_all_patent_alerts(self.config),
                }
            )

        @app.put("/api/workbench")
        def put_workbench():
            body = request.json or {}
            wb = self.config.set_active_workbench(str(body.get("workbench", "paper")).strip())
            return _json_response({"active_workbench": wb})

        @app.put("/api/settings")
        def put_settings():
            body = request.json or {}
            patch = body.get("compiler", body)
            updated = self.config.update_compiler(patch)
            self.watcher.update_options(
                auto_compile=updated["auto_compile"],
                debounce_ms=updated["debounce_ms"],
            )
            payload = {"compiler": updated}
            self.broker.publish("SETTINGS_UPDATED", payload)
            return _json_response(payload)

        @app.get("/api/settings/compiler/check")
        def get_compiler_check():
            preview = dict(self.config.compiler)
            if request.query.getunicode("tex_bin_dir") is not None:
                preview["tex_bin_dir"] = _qstr("tex_bin_dir")
            if request.query.getunicode("perl_path") is not None:
                preview["perl_path"] = _qstr("perl_path")
            return _json_response(check_toolchain(preview))

        @app.get("/api/settings/compiler/scan")
        def get_compiler_scan():
            return _json_response(
                {
                    "installations": scan_local_tex_installations(force_refresh=True),
                    "mirrors": CTAN_MIRRORS,
                }
            )

        @app.get("/api/settings/compiler/install/status")
        def get_compiler_install_status():
            return _json_response(self.tex_installer.get_status())

        @app.post("/api/settings/compiler/install")
        def post_compiler_install():
            body = request.json or {}
            action = str(body.get("action", "")).strip()
            options = body.get("options") if isinstance(body.get("options"), dict) else {}
            try:
                status = self.tex_installer.start_action(
                    action=action,
                    options=options,
                    compiler_settings=self.config.compiler,
                )
                return _json_response(status, status=202)
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)
            except RuntimeError as exc:
                return _json_response({"error": str(exc)}, status=409)

        @app.post("/api/settings/compiler/install/cancel")
        def post_compiler_install_cancel():
            return _json_response(self.tex_installer.cancel())

        @app.post("/api/settings/select-dir")
        def post_select_dir():
            if not self.folder_dialog_fn:
                return _json_response({"error": "Native folder dialog unavailable in headless mode"}, status=400)
            try:
                selected = self.folder_dialog_fn()
            except Exception as exc:
                return _json_response({"error": str(exc)}, status=500)
            return _json_response({"path": selected or ""})

        @app.get("/api/companions/templates")
        def get_companion_templates():
            templates = [
                {"key": k, "default_filename": v["default_filename"], "label": v["label"]}
                for k, v in COMPANION_TEMPLATES.items()
            ]
            return _json_response({"templates": templates})

        @app.get("/api/companions/content")
        def get_companion_content():
            name = _qstr("manuscript", self.active_manuscript)
            filename = _qstr("file")
            try:
                mdir = self._manuscript_dir(name)
                return _json_response(read_companion_content(mdir, filename))
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/companions")
        def post_companion():
            name = _qstr("manuscript", self.active_manuscript)
            body = request.json or {}
            template_key = str(body.get("template", "title_page")).strip()
            filename = str(body.get("filename", "")).strip()
            try:
                mdir = self._manuscript_dir(name)
                created = create_companion_file(mdir, template_key, filename)
            except FileExistsError as exc:
                return _json_response({"error": str(exc)}, status=409)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

            with self._state_lock:
                root_path = self.workspace_info.get("project_root", "")
                if root_path:
                    self.workspace_info = scan_workspace(root_path)
            ws_payload = self.get_workspace_payload()
            self.broker.publish("WORKSPACE_UPDATED", ws_payload)
            return _json_response({"created": created, "workspace": ws_payload}, status=201)

        @app.post("/api/compile")
        def post_compile():
            body = request.json or {}
            name = str(body.get("manuscript") or self.active_manuscript).strip()
            baseline = body["baseline"] if "baseline" in body else ...
            engine = body.get("engine")
            target_tex = str(body.get("target_tex", "")).strip()
            try:
                status = self.trigger_compile(name, baseline=baseline, engine=engine, target_tex=target_tex)
                return _json_response(status, status=202)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/compile/status")
        def get_compile_status():
            name = _qstr("manuscript", self.active_manuscript)
            with self._state_lock:
                ws = self.workspace
            return _json_response(self.coordinator.get_status(name, ws=ws))

        @app.get("/api/pdf/<kind>")
        def get_pdf(kind: str):
            if kind not in ("clean", "diff", "companion"):
                return _json_response({"error": "Invalid PDF kind"}, status=400)
            name = _qstr("manuscript", self.active_manuscript)
            with self._state_lock:
                ws = self.workspace
            status = self.coordinator.get_status(name, ws=ws)
            if kind == "clean":
                pdf_str = status.get("clean_pdf", "")
            elif kind == "diff":
                pdf_str = status.get("diff_pdf", "")
            else:
                file_name = _qstr("file")
                pdf_str = status.get("companion_pdfs", {}).get(file_name, "")
            if not pdf_str or not Path(pdf_str).is_file():
                return _json_response({"error": f"{kind}.pdf not compiled yet"}, status=404)
            data = Path(pdf_str).read_bytes()
            return HTTPResponse(
                body=data,
                status=200,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": str(len(data)),
                    "Cache-Control": "no-store",
                },
            )

        @app.get("/api/events")
        def sse_events():
            response.content_type = "text/event-stream; charset=utf-8"
            response.set_header("Cache-Control", "no-cache")
            response.set_header("X-Accel-Buffering", "no")

            client_q = self.broker.subscribe()

            def stream():
                try:
                    yield ": connected\n\n"
                    while True:
                        try:
                            msg = client_q.get(timeout=15)
                            yield msg
                        except queue.Empty:
                            yield ": keepalive\n\n"
                finally:
                    self.broker.unsubscribe(client_q)

            return stream()

        @app.get("/api/events")
        def sse_events():
            response.content_type = "text/event-stream; charset=utf-8"
            response.set_header("Cache-Control", "no-cache")
            response.set_header("X-Accel-Buffering", "no")

            client_q = self.broker.subscribe()

            def stream():
                try:
                    yield ": connected\n\n"
                    while True:
                        try:
                            msg = client_q.get(timeout=15)
                            yield msg
                        except queue.Empty:
                            yield ": keepalive\n\n"
                finally:
                    self.broker.unsubscribe(client_q)

            return stream()

        # ---------------- Patent Management Routes ----------------

        @app.get("/api/patents/state")
        def get_patent_state():
            return _json_response(self.get_patent_state_payload())

        @app.post("/api/patents/open")
        def post_patent_open():
            body = request.json or {}
            path_str = str(body.get("path", "")).strip()
            if not path_str:
                return _json_response({"error": "Missing patent directory path"}, status=400)
            return _json_response(
                self.open_patent(
                    path_str,
                    title=str(body.get("name", "")).strip(),
                    patent_type=str(body.get("patent_type", "invention")).strip(),
                )
            )

        @app.post("/api/patents/create")
        def post_patent_create():
            body = request.json or {}
            parent_dir = str(body.get("parent_dir", "")).strip()
            folder_name = str(body.get("folder_name", "")).strip()
            title = str(body.get("title", "")).strip() or folder_name
            patent_type = str(body.get("patent_type", "invention")).strip()
            if not parent_dir or not folder_name or any(sep in folder_name for sep in ("/", "\\", "..")):
                return _json_response({"error": "无效的父目录或专利文件夹名"}, status=400)
            target = Path(parent_dir).expanduser().resolve() / folder_name
            return _json_response(self.open_patent(str(target), title=title, patent_type=patent_type), status=201)

        @app.post("/api/patents/demo")
        def post_patent_demo():
            demo_dir = ensure_demo_patent(self.patent_mgr)
            return _json_response(self.open_patent(str(demo_dir)))

        @app.patch("/api/patents/history")
        def patch_patent_history():
            body = request.json or {}
            path_str = str(body.get("path", "")).strip()
            if not path_str:
                return _json_response({"error": "Missing patent path"}, status=400)
            if "name" in body:
                self.config.rename_patent(path_str, str(body["name"]))
            if "pinned" in body:
                self.config.set_patent_pinned(path_str, bool(body["pinned"]))
            return _json_response({"patent_history": self.config.patent_history})

        @app.delete("/api/patents/history")
        def delete_patent_history():
            body = request.json or {}
            path_str = str(body.get("path", "")).strip()
            if not path_str:
                return _json_response({"error": "Missing patent path"}, status=400)
            history = self.config.remove_patent(path_str)
            with self._state_lock:
                if self.active_patent_dir and Path(self.active_patent_dir).resolve() == Path(path_str).resolve():
                    self.active_patent_dir = ""
                    self.patent_watcher.configure(None)
            return _json_response({"patent_history": history, "alerts": collect_all_patent_alerts(self.config)})

        @app.put("/api/patents/meta")
        def put_patent_meta():
            body = request.json or {}
            try:
                pdir = self._active_patent_path()
                meta = self.patent_mgr.save_meta(pdir, body)
                self.config.record_patent(
                    str(pdir),
                    name=str(meta.get("title") or pdir.name),
                    patent_type=str(meta.get("patent_type") or "invention"),
                    stage=str(meta.get("stage") or "drafting"),
                )
                state = self.get_patent_state_payload()
                self.broker.publish("PATENT_UPDATED", state)
                return _json_response(state)
            except (ValueError, OSError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/document")
        def get_patent_document():
            rel = _qstr("rel", "disclosure.md")
            rev = _qstr("rev")
            try:
                pdir = self._active_patent_path()
                return _json_response(self.patent_mgr.preview_document(pdir, rel, rev))
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.put("/api/patents/markdown")
        def put_patent_markdown():
            body = request.json or {}
            rel = str(body.get("rel", "disclosure.md")).strip().replace("\\", "/").lstrip("/")
            content = str(body.get("content", ""))
            try:
                pdir = self._active_patent_path()
                target = (pdir / rel).resolve()
                if pdir not in target.parents or target.suffix.lower() != ".md":
                    raise ValueError("仅支持保存当前专利目录内的 .md 文件")
                target.write_text(content, encoding="utf-8")
                doc = self.patent_mgr.preview_document(pdir, rel)
                numerals = self.patent_mgr.check_reference_numerals(pdir)
                return _json_response({"document": doc, "numeral_check": numerals})
            except (ValueError, OSError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/export-docx")
        def post_patent_export_docx():
            body = request.json or {}
            rel_md = str(body.get("source_md", "disclosure.md")).strip()
            out_name = str(body.get("filename", "")).strip()
            do_snapshot = bool(body.get("snapshot", False))
            try:
                pdir = self._active_patent_path()
                src_md = (pdir / rel_md).resolve()
                if not src_md.is_file():
                    raise FileNotFoundError(f"未找到 Markdown 源稿: {rel_md}")
                meta = self.patent_mgr.load_meta(pdir)
                if not out_name:
                    stem = (meta.get("title") or pdir.name).strip()[:40]
                    for bad in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
                        stem = stem.replace(bad, "_")
                    out_name = f"{stem}_交底书.docx"
                if not out_name.lower().endswith(".docx"):
                    out_name += ".docx"
                out_path = (pdir / "submissions" / Path(out_name).name).resolve()
                export_markdown_to_docx(src_md.read_text(encoding="utf-8", errors="replace"), pdir, out_path, meta=meta)
                if do_snapshot:
                    create_snapshot(pdir, f"export: 生成交底书 {out_path.name}")
                state = self.get_patent_state_payload()
                self.broker.publish("PATENT_UPDATED", state)
                return _json_response({"exported_rel": f"submissions/{out_path.name}", "state": state})
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/diff")
        def get_patent_diff():
            left_rel = _qstr("left_rel", "disclosure.md")
            left_rev = _qstr("left_rev")
            right_rel = _qstr("right_rel", "disclosure.md")
            right_rev = _qstr("right_rev")
            try:
                pdir = self._active_patent_path()
                return _json_response(self.patent_mgr.diff_documents(pdir, left_rel, left_rev, right_rel, right_rev))
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/snapshots")
        def get_patent_snapshots():
            try:
                pdir = self._active_patent_path()
                return _json_response({"snapshots": list_snapshots(pdir)})
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/snapshots")
        def post_patent_snapshot():
            body = request.json or {}
            msg = str(body.get("message", "")).strip() or "chore(patent): save snapshot"
            tag = str(body.get("tag", "")).strip()
            auto_push = bool(body.get("push", False))
            try:
                pdir = self._active_patent_path()
                res = create_snapshot(pdir, msg, tag=tag)
                if auto_push and self.config.gitea.get("token"):
                    sync_gitea(pdir, self.config.gitea, direction="push")
                state = self.get_patent_state_payload()
                self.broker.publish("PATENT_UPDATED", state)
                return _json_response({"snapshot": res, "state": state})
            except (ValueError, RuntimeError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.put("/api/patents/gitea/config")
        def put_gitea_config():
            body = request.json or {}
            updated = self.config.update_gitea(body)
            return _json_response({"gitea": updated})

        @app.post("/api/patents/gitea/test")
        def post_gitea_test():
            body = request.json or {}
            cfg = {**self.config.gitea, **body} if body else self.config.gitea
            try:
                return _json_response(test_gitea_connection(cfg))
            except (ValueError, RuntimeError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/gitea/init-repo")
        def post_gitea_init_repo():
            body = request.json or {}
            try:
                pdir = self._active_patent_path()
                meta = self.patent_mgr.load_meta(pdir)
                repo_name = str(body.get("repo_name", "")).strip() or pdir.name
                res = create_or_bind_gitea_repo(
                    pdir,
                    repo_name=repo_name,
                    description=str(meta.get("title") or pdir.name),
                    gitea_cfg=self.config.gitea,
                )
                return _json_response({**res, "state": self.get_patent_state_payload()})
            except (ValueError, RuntimeError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/gitea/sync")
        def post_gitea_sync():
            body = request.json or {}
            direction = str(body.get("direction", "push")).strip().lower()
            try:
                pdir = self._active_patent_path()
                res = sync_gitea(pdir, self.config.gitea, direction=direction)
                return _json_response({**res, "state": self.get_patent_state_payload()})
            except (ValueError, RuntimeError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/figures/<filename>")
        def get_patent_figure(filename: str):
            try:
                pdir = self._active_patent_path()
                figs_dir = (pdir / "figures").resolve()
                target = (figs_dir / _utf8_route_param(filename)).resolve()
                if figs_dir not in target.parents or not target.is_file():
                    return HTTPResponse(body="Not Found", status=404)
                ctype = MIME_OVERRIDES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return HTTPResponse(
                    body=target.read_bytes(),
                    status=200,
                    headers={"Content-Type": ctype, "Cache-Control": "no-store"},
                )
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/figures/import")
        def post_patent_figure_import():
            import shutil

            body = request.json or {}
            try:
                pdir = self._active_patent_path()
                figs_dir = pdir / "figures"
                figs_dir.mkdir(exist_ok=True)
                src_path_str = str(body.get("source_path", "")).strip()
                if body.get("pick_dialog"):
                    if not self.file_dialog_fn:
                        return _json_response({"error": "原生文件选择框在无头模式下不可用"}, status=400)
                    picked = self.file_dialog_fn()
                    if not picked:
                        return _json_response({"cancelled": True})
                    src_path_str = picked

                if not src_path_str:
                    return _json_response({"error": "请选择有效图片文件"}, status=400)
                src = Path(src_path_str).expanduser().resolve()
                if not src.is_file() or src.suffix.lower() not in IMAGE_SUFFIXES:
                    return _json_response({"error": "仅支持 PNG/JPG/SVG/WEBP/BMP 格式图片"}, status=400)
                dst = figs_dir / src.name
                if src != dst.resolve():
                    shutil.copy2(src, dst)
                meta = self.patent_mgr.load_meta(pdir)
                figs = list(meta.get("figures") or [])
                if not any(isinstance(f, dict) and f.get("filename") == dst.name for f in figs):
                    figs.append({"filename": dst.name, "caption": str(body.get("caption") or f"图{len(figs) + 1} {dst.stem}")})
                    self.patent_mgr.save_meta(pdir, {"figures": figs})
                return _json_response({"cancelled": False, "filename": dst.name, "state": self.get_patent_state_payload()})
            except (ValueError, OSError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/numerals/check")
        def get_patent_numerals_check():
            try:
                pdir = self._active_patent_path()
                return _json_response(self.patent_mgr.check_reference_numerals(pdir))
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/feedback/raw")
        def post_patent_feedback_raw():
            body = request.json or {}
            try:
                pdir = self._active_patent_path()
                doc = self.patent_mgr.add_raw_feedback_batch(
                    pdir,
                    source_label=str(body.get("source_label", "")),
                    raw_text=str(body.get("raw_text", "")),
                )
                return _json_response({"feedback": doc})
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/feedback/import-docx")
        def post_patent_feedback_import_docx():
            body = request.json or {}
            rel = str(body.get("rel", "")).strip()
            try:
                pdir = self._active_patent_path()
                return _json_response(self.patent_mgr.import_docx_comments_to_feedback(pdir, rel))
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/feedback/items")
        def post_patent_feedback_item():
            body = request.json or {}
            try:
                pdir = self._active_patent_path()
                doc = self.patent_mgr.add_feedback_item(pdir, body)
                return _json_response({"feedback": doc})
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.patch("/api/patents/feedback/items/<item_id>")
        def patch_patent_feedback_item(item_id: str):
            body = request.json or {}
            try:
                pdir = self._active_patent_path()
                doc = self.patent_mgr.update_feedback_item(pdir, item_id, body)
                return _json_response({"feedback": doc})
            except KeyError as exc:
                return _json_response({"error": str(exc)}, status=404)
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.delete("/api/patents/feedback/items/<item_id>")
        def delete_patent_feedback_item(item_id: str):
            try:
                pdir = self._active_patent_path()
                doc = self.patent_mgr.delete_feedback_item(pdir, item_id)
                return _json_response({"feedback": doc})
            except KeyError as exc:
                return _json_response({"error": str(exc)}, status=404)
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/feedback/export")
        def get_patent_feedback_export():
            fmt = _qstr("format", "md").lower()
            try:
                pdir = self._active_patent_path()
                md = self.patent_mgr.export_reply_sheet_md(pdir)
                if fmt == "docx":
                    out = pdir / "submissions" / "代理沟通确认单.docx"
                    export_markdown_to_docx(md, pdir, out)
                    return _json_response({"format": "docx", "exported_rel": f"submissions/{out.name}", "content": md})
                return _json_response({"format": "md", "content": md})
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.get("/api/patents/alerts")
        def get_patent_alerts():
            return _json_response(
                {
                    "alerts": collect_all_patent_alerts(self.config),
                    "reminders": self._reminders_view(),
                }
            )

        @app.put("/api/patents/reminders/settings")
        def put_patent_reminder_settings():
            body = request.json or {}
            task_info = None
            try:
                if "task_enabled" in body:
                    daily_time = str(body.get("daily_time") or self.config.reminders.get("daily_time") or "09:30")
                    task_info = set_windows_reminder_task(bool(body["task_enabled"]), daily_time=daily_time)
                updated = self.config.update_reminders(body)
                return _json_response(
                    {
                        "reminders": self._reminders_view(),
                        "task": task_info,
                        "alerts": collect_all_patent_alerts(self.config),
                    }
                )
            except (ValueError, RuntimeError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/patents/reminders/test")
        def post_patent_reminder_test():
            body = request.json or {}
            if body.get("email_only"):
                try:
                    smtp_cfg = {**self.config.reminders.get("smtp", {}), **(body.get("smtp") or {})}
                    send_smtp_reminder(
                        smtp_cfg,
                        "Rebuttal Sidecar · 专利邮件提醒测试",
                        "您好，这是一封来自 Rebuttal Sidecar 专利管理工作台的 SMTP 测试邮件。",
                    )
                    return _json_response({"email_sent": True})
                except Exception as exc:
                    return _json_response({"error": str(exc)}, status=400)
            res = run_reminder_check(self.config, force=True)
            return _json_response(res)

        @app.get("/<filepath:path>")
        def static_files(filepath: str):
            return self._serve_static(filepath)

    def _serve_static(self, rel_path: str) -> HTTPResponse:
        web_root = self.web_dir.resolve()
        target = (web_root / rel_path).resolve()
        if web_root not in target.parents and target != web_root:
            return HTTPResponse(body="Forbidden", status=403)
        if not target.is_file():
            return HTTPResponse(body="Not Found", status=404)

        suffix = target.suffix.lower()
        content_type = MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        return HTTPResponse(
            body=data,
            status=200,
            headers={"Content-Type": content_type, "Cache-Control": "no-store"},
        )

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._httpd = ThreadingWSGIServer((host, port), QuietWSGIHandler)
        self._httpd.set_app(self.bottle)
        self.port = self._httpd.server_address[1]
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="sidecar-http",
            daemon=True,
        )
        self._server_thread.start()
        return self.port

    def stop(self) -> None:
        self.watcher.stop()
        self.patent_watcher.stop()
        self.coordinator.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
