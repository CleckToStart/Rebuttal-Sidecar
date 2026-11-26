"""Desktop entrypoint for Rebuttal Sidecar (PyWebView + local ThreadingWSGIServer)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from config import AppConfig
from server import SidecarApp


def _resolve_web_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "web"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuttal Sidecar — Local LaTeX Rebuttal Companion")
    parser.add_argument("workspace", nargs="?", default="", help="Project root or docs/manuscripts directory")
    parser.add_argument("--headless", action="store_true", help="Run local HTTP/SSE server without PyWebView window")
    parser.add_argument("--port", type=int, default=0, help="Local port to bind (default: random free port)")
    args = parser.parse_args(argv)

    app = SidecarApp(
        config=AppConfig(),
        web_dir=_resolve_web_dir(),
        initial_workspace=args.workspace,
    )
    port = app.start(host="127.0.0.1", port=args.port)
    url = f"http://127.0.0.1:{port}"

    if args.headless:
        print(f"Rebuttal Sidecar headless server listening on {url}", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            app.stop()
        return 0

    import webview

    window_holder: list[webview.Window] = []

    def choose_folder() -> str | None:
        if not window_holder:
            return None
        win = window_holder[0]
        initial_dir = app.workspace_info.get("project_root") or str(Path.home())
        folder_mode = getattr(webview, "FOLDER_DIALOG", None) or webview.FileDialog.FOLDER
        result_box: list[str | None] = [None]

        def _open_on_sta() -> None:
            selected = win.create_file_dialog(folder_mode, directory=initial_dir)
            result_box[0] = selected[0] if selected else None

        # WinForms IFileDialog requires execution on the GUI STA thread.
        try:
            from System import Action
            from webview.platforms.winforms import BrowserView

            form = BrowserView.instances.get(win.uid)
            if form is not None and form.InvokeRequired:
                form.Invoke(Action(_open_on_sta))
                return result_box[0]
        except Exception:
            pass

        _open_on_sta()
        return result_box[0]

    app.folder_dialog_fn = choose_folder
    window = webview.create_window(
        "Rebuttal Sidecar",
        url,
        width=1440,
        height=900,
        min_size=(1024, 680),
        text_select=True,
    )
    window_holder.append(window)
    window.events.closed += app.stop
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
