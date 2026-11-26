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
from proc_utils import check_toolchain
from scanner import WorkspacePaths, list_baselines, resolve_workspace, scan_workspace
from todo_manager import TodoManager
from watcher import ManuscriptWatcher

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

        self.broker = SSEBroker()
        self.todo_mgr = TodoManager()
        self.coordinator = CompileCoordinator(on_event=self._on_compile_event)
        self.watcher = ManuscriptWatcher(
            on_auto_compile=self._on_watcher_auto_compile,
            on_source_dirty=self._on_watcher_dirty,
            on_todo_changed=self._on_watcher_todo,
            was_self_todo_write=self.todo_mgr.was_written_recently,
        )

        self._state_lock = threading.Lock()
        self.workspace: WorkspacePaths | None = None
        self.workspace_info: dict = self._empty_workspace_info()
        self.active_manuscript: str = ""

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
            name = request.query.get("manuscript", "").strip()
            try:
                self._manuscript_dir(name)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            with self._state_lock:
                ws = self.workspace
            return _json_response(list_baselines(ws, name))

        @app.get("/api/todos")
        def get_todos():
            name = request.query.get("manuscript", "").strip()
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
            name = request.query.get("manuscript", "").strip()
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
            name = request.query.get("manuscript", "").strip()
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
            name = request.query.get("manuscript", "").strip()
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
            name = request.query.get("manuscript", "").strip()
            fmt = request.query.get("format", "md").strip().lower()
            try:
                mdir = self._manuscript_dir(name)
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)
            content = self.todo_mgr.export_response_letter(mdir, fmt=fmt if fmt in ("md", "tex") else "md")
            return _json_response({"manuscript": name, "format": fmt, "content": content})

        @app.put("/api/baseline")
        def put_baseline():
            name = request.query.get("manuscript", "").strip()
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
            return _json_response({"compiler": self.config.compiler})

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
            if request.query.get("tex_bin_dir") is not None:
                preview["tex_bin_dir"] = request.query.get("tex_bin_dir", "").strip()
            if request.query.get("perl_path") is not None:
                preview["perl_path"] = request.query.get("perl_path", "").strip()
            return _json_response(check_toolchain(preview))

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
            name = request.query.get("manuscript", self.active_manuscript).strip()
            filename = request.query.get("file", "").strip()
            try:
                mdir = self._manuscript_dir(name)
                return _json_response(read_companion_content(mdir, filename))
            except (ValueError, FileNotFoundError) as exc:
                return _json_response({"error": str(exc)}, status=400)

        @app.post("/api/companions")
        def post_companion():
            name = request.query.get("manuscript", self.active_manuscript).strip()
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
            name = request.query.get("manuscript", self.active_manuscript).strip()
            with self._state_lock:
                ws = self.workspace
            return _json_response(self.coordinator.get_status(name, ws=ws))

        @app.get("/api/pdf/<kind>")
        def get_pdf(kind: str):
            if kind not in ("clean", "diff", "companion"):
                return _json_response({"error": "Invalid PDF kind"}, status=400)
            name = request.query.get("manuscript", self.active_manuscript).strip()
            with self._state_lock:
                ws = self.workspace
            status = self.coordinator.get_status(name, ws=ws)
            if kind == "clean":
                pdf_str = status.get("clean_pdf", "")
            elif kind == "diff":
                pdf_str = status.get("diff_pdf", "")
            else:
                file_name = request.query.get("file", "").strip()
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
        self.coordinator.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
