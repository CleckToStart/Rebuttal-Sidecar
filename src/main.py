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
        folder_mode = getattr(webview, "FOLDER_DIALOG", None) or webview.FileDialog.FOLDER
        selected = window_holder[0].create_file_dialog(folder_mode)
        return selected[0] if selected else None

    app.folder_dialog_fn = choose_folder
    window = webview.create_window(
        "Rebuttal Sidecar",
        url,
        width=1440,
        height=900,
        min_size=(1024, 680),
    )
    window_holder.append(window)
    window.events.closed += app.stop
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
