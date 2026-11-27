"""Desktop entrypoint for Rebuttal Sidecar (Frameless PyWebView + local ThreadingWSGIServer)."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from config import AppConfig
from patent_timeline import run_reminder_check
from server import SidecarApp


def _resolve_resource_path(rel: str) -> Path:
    """Locate bundled web/ or assets/ resources in dev, PyInstaller, and Nuitka modes."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / rel)
    here = Path(__file__).resolve().parent
    candidates.append(here / rel)
    candidates.append(here.parent / rel)
    candidates.append(Path(sys.executable).resolve().parent / rel)
    for cand in candidates:
        if cand.exists():
            return cand
    return here.parent / rel


class WindowBridge:
    """JS-accessible window controls and DPI-aware frameless edge resize API."""

    def __init__(self, win_getter) -> None:
        self._get_win = win_getter
        self._resize_ctx: dict | None = None

    def _run_on_form(self, fn):
        win = self._get_win()
        if win is None:
            return None
        try:
            from System import Action
            from webview.platforms.winforms import BrowserView

            form = BrowserView.instances.get(win.uid)
            if form is None:
                return None
            if form.InvokeRequired:
                box: list = [None]
                form.Invoke(Action(lambda: box.__setitem__(0, fn(form))))
                return box[0]
            return fn(form)
        except Exception:
            return None

    def minimize(self) -> None:
        win = self._get_win()
        if win is not None:
            win.minimize()

    def toggle_maximize(self) -> dict:
        def _toggle(form) -> bool:
            import System.Windows.Forms as WinForms

            if form.WindowState == WinForms.FormWindowState.Maximized:
                form.WindowState = WinForms.FormWindowState.Normal
                return False
            form.MaximizedBounds = WinForms.Screen.FromControl(form).WorkingArea
            form.WindowState = WinForms.FormWindowState.Maximized
            return True

        maximized = self._run_on_form(_toggle)
        return {"maximized": bool(maximized)}

    def get_state(self) -> dict:
        def _state_fn(form) -> bool:
            import System.Windows.Forms as WinForms

            return form.WindowState == WinForms.FormWindowState.Maximized

        maximized = self._run_on_form(_state_fn)
        return {"maximized": bool(maximized)}

    def close(self) -> None:
        win = self._get_win()
        if win is not None:
            win.destroy()

    def begin_resize(self, edge: str) -> bool:
        if edge not in ("n", "s", "e", "w", "nw", "ne", "sw", "se"):
            return False

        def _capture(form) -> dict | None:
            import System.Windows.Forms as WinForms

            if form.WindowState == WinForms.FormWindowState.Maximized:
                return None
            pt = wintypes.POINT()
            rect = wintypes.RECT()
            hwnd = form.Handle.ToInt32()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return {
                "edge": edge,
                "hwnd": hwnd,
                "cx": pt.x,
                "cy": pt.y,
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "min_w": int(form.MinimumSize.Width) or 900,
                "min_h": int(form.MinimumSize.Height) or 600,
            }

        self._resize_ctx = self._run_on_form(_capture)
        return self._resize_ctx is not None

    def step_resize(self) -> bool:
        ctx = self._resize_ctx
        if not ctx:
            return False
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        dx = pt.x - ctx["cx"]
        dy = pt.y - ctx["cy"]

        left, top, right, bottom = ctx["left"], ctx["top"], ctx["right"], ctx["bottom"]
        edge = ctx["edge"]
        min_w, min_h = ctx["min_w"], ctx["min_h"]

        if "w" in edge:
            left = min(left + dx, right - min_w)
        if "e" in edge:
            right = max(right + dx, left + min_w)
        if "n" in edge:
            top = min(top + dy, bottom - min_h)
        if "s" in edge:
            bottom = max(bottom + dy, top + min_h)

        ctypes.windll.user32.MoveWindow(ctx["hwnd"], left, top, right - left, bottom - top, True)
        return True

    def end_resize(self) -> None:
        self._resize_ctx = None


def _configure_seamless_winforms(win, icon_path: Path) -> None:
    """Enable native WebView2 drag-regions, Win11 rounded corners, and custom icon."""
    try:
        from System import Action
        from System.Drawing import Icon
        from webview.platforms.winforms import BrowserView, DwmSetWindowAttribute

        form = BrowserView.instances.get(win.uid)
        if form is None:
            return

        def _apply() -> None:
            hwnd = form.Handle.ToInt32()
            # DWMWA_WINDOW_CORNER_PREFERENCE (33) = DWMWCP_ROUND (2) on Win11
            try:
                DwmSetWindowAttribute(hwnd, 33, 2)
            except Exception:
                pass
            if icon_path.is_file():
                try:
                    form.Icon = Icon(str(icon_path))
                except Exception:
                    pass

            wv = getattr(form, "webview", None)
            if wv is None:
                return
            cwv2 = getattr(wv, "CoreWebView2", None)
            if cwv2 is not None:
                cwv2.Settings.IsNonClientRegionSupportEnabled = True
            else:
                def _on_ready(sender, args) -> None:
                    if getattr(args, "IsSuccess", False) and sender.CoreWebView2 is not None:
                        sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True

                wv.CoreWebView2InitializationCompleted += _on_ready

        if form.InvokeRequired:
            form.Invoke(Action(_apply))
        else:
            _apply()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    parser = argparse.ArgumentParser(description="Rebuttal Sidecar — Local LaTeX Rebuttal & Patent Companion")
    parser.add_argument("workspace", nargs="?", default="", help="Project root or docs/manuscripts directory")
    parser.add_argument("--headless", action="store_true", help="Run local HTTP/SSE server without PyWebView window")
    parser.add_argument("--port", type=int, default=0, help="Local port to bind (default: random free port)")
    parser.add_argument("--check-reminders", action="store_true", help="Check patent deadlines and send due reminders")
    args = parser.parse_args(argv)

    if args.check_reminders:
        summary = run_reminder_check(AppConfig(), force=False)
        print(f"Patent reminder check: checked={summary['checked']} notified={summary['notified']}", flush=True)
        return 0

    web_dir = _resolve_resource_path("web")
    icon_path = _resolve_resource_path("assets/icon.ico")

    app = SidecarApp(
        config=AppConfig(),
        web_dir=web_dir,
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

    webview.settings["ALLOW_DOWNLOADS"] = True
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

    def choose_image_file() -> str | None:
        if not window_holder:
            return None
        win = window_holder[0]
        initial_dir = app.active_patent_dir or app.workspace_info.get("project_root") or str(Path.home())
        open_mode = getattr(webview, "OPEN_DIALOG", None) or webview.FileDialog.OPEN
        result_box: list[str | None] = [None]

        def _open_on_sta() -> None:
            selected = win.create_file_dialog(
                open_mode,
                directory=initial_dir,
                file_types=("Image files (*.png;*.jpg;*.jpeg;*.svg;*.webp;*.bmp)", "All files (*.*)"),
            )
            result_box[0] = selected[0] if selected else None

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
    app.file_dialog_fn = choose_image_file
    bridge = WindowBridge(lambda: window_holder[0] if window_holder else None)
    window = webview.create_window(
        "Rebuttal Sidecar",
        url,
        width=1440,
        height=900,
        min_size=(1024, 680),
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#EEF2F6",
        text_select=True,
        js_api=bridge,
    )
    window_holder.append(window)
    window.events.shown += lambda: _configure_seamless_winforms(window, icon_path)
    window.events.closed += app.stop
    start_kwargs: dict = {}
    if icon_path.is_file():
        start_kwargs["icon"] = str(icon_path)
    webview.start(**start_kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
