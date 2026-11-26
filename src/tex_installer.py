"""Built-in MiKTeX & TeX Live installer and macro package provisioner."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

from proc_utils import CREATE_NO_WINDOW, build_tex_env, scan_local_tex_installations, which_tool

CTAN_MIRRORS = {
    "tuna": {
        "label": "清华大学 TUNA 镜像",
        "url": "https://mirrors.tuna.tsinghua.edu.cn/CTAN/systems/win32/tlnet",
    },
    "ustc": {
        "label": "中国科大 USTC 镜像",
        "url": "https://mirrors.ustc.edu.cn/CTAN/systems/win32/tlnet",
    },
    "sjtu": {
        "label": "上海交大 SJTU 镜像",
        "url": "https://mirrors.sjtug.sjtu.edu.cn/ctan/systems/win32/tlnet",
    },
    "ctan": {
        "label": "CTAN 官方自动路由",
        "url": "https://mirror.ctan.org/systems/win32/tlnet",
    },
}

CORE_MIKTEX_PACKAGES = (
    "latexmk",
    "latexdiff",
    "ulem",
    "xcolor",
    "epstopdf-pkg",
    "grfext",
    "booktabs",
    "cite",
    "ieeetran",
    "elsarticle",
)


class TexInstallerManager:
    """Runs MiKTeX / TeX Live installation tasks with live SSE log streaming."""

    def __init__(
        self,
        on_event: Callable[[str, dict], None] | None = None,
        on_bin_detected: Callable[[str], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_bin_detected = on_bin_detected
        self._lock = threading.Lock()
        self._cancel_flag = threading.Event()
        self._active_proc: subprocess.Popen | None = None
        self._status: dict = {
            "state": "idle",
            "action": "",
            "progress": "",
            "logs": [],
            "detected_bin_dir": "",
            "message": "",
        }

    def get_status(self) -> dict:
        with self._lock:
            return {**self._status, "logs": list(self._status["logs"])}

    def _emit(self) -> None:
        if self._on_event:
            self._on_event("TEX_INSTALL_STATUS", self.get_status())

    def _append_log(self, line: str, progress: str | None = None) -> None:
        clean = line.rstrip()
        if not clean and progress is None:
            return
        with self._lock:
            if clean:
                logs = self._status["logs"]
                logs.append(clean)
                if len(logs) > 120:
                    del logs[: len(logs) - 120]
            if progress is not None:
                self._status["progress"] = progress
        self._emit()

    def _finish(self, state: str, message: str, detected_bin: str = "") -> None:
        with self._lock:
            self._status["state"] = state
            self._status["message"] = message
            self._status["progress"] = "100%" if state == "success" else self._status["progress"]
            if detected_bin:
                self._status["detected_bin_dir"] = detected_bin
            self._active_proc = None
        self._emit()
        if detected_bin and self._on_bin_detected:
            self._on_bin_detected(detected_bin)

    def cancel(self) -> dict:
        self._cancel_flag.set()
        with self._lock:
            proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._append_log("[已请求取消安装任务]")
        return self.get_status()

    def start_action(
        self,
        action: str,
        options: dict | None = None,
        compiler_settings: dict | None = None,
    ) -> dict:
        valid_actions = {"install_miktex", "install_miktex_packages", "install_texlive"}
        if action not in valid_actions:
            raise ValueError(f"Unsupported installer action: {action}")

        with self._lock:
            if self._status["state"] == "running":
                raise RuntimeError("已有 TeX 安装或宏包任务正在运行")
            self._cancel_flag.clear()
            self._status = {
                "state": "running",
                "action": action,
                "progress": "准备中...",
                "logs": [f"[开始任务] {action}"],
                "detected_bin_dir": "",
                "message": "任务运行中",
            }
        self._emit()

        opts = dict(options or {})
        settings = dict(compiler_settings or {})
        t = threading.Thread(target=self._run_worker, args=(action, opts, settings), daemon=True)
        t.start()
        return self.get_status()

    def _stream_process(self, cmd: list[str], env: dict[str, str] | None = None) -> int:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
            "creationflags": CREATE_NO_WINDOW if os.name == "nt" else 0,
        }
        proc = subprocess.Popen(cmd, **kwargs)
        with self._lock:
            self._active_proc = proc

        if proc.stdout is not None:
            for raw in proc.stdout:
                if self._cancel_flag.is_set():
                    proc.terminate()
                    break
                text = raw.strip()
                if text:
                    self._append_log(text)
        rc = proc.wait()
        with self._lock:
            self._active_proc = None
        return rc

    def _run_worker(self, action: str, options: dict, compiler_settings: dict) -> None:
        try:
            if action == "install_miktex":
                self._do_install_miktex()
            elif action == "install_miktex_packages":
                self._do_install_miktex_packages(compiler_settings)
            elif action == "install_texlive":
                self._do_install_texlive(options)
        except Exception as exc:
            self._append_log(f"[错误] {exc}")
            self._finish("error", str(exc))

    def _do_install_miktex(self) -> None:
        winget_exe = shutil.which("winget")
        if winget_exe:
            self._append_log("正在调用 Windows winget 安装 MiKTeX (MiKTeX.MiKTeX)...", "安装 MiKTeX 中")
            rc = self._stream_process(
                [
                    winget_exe,
                    "install",
                    "--id",
                    "MiKTeX.MiKTeX",
                    "-e",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ]
            )
            if self._cancel_flag.is_set():
                self._finish("error", "用户已取消 MiKTeX 安装")
                return
            installs = scan_local_tex_installations(force_refresh=True)
            miktex_bin = next((i["bin_dir"] for i in installs if i["distro"] == "MiKTeX"), "")
            if rc == 0 or miktex_bin:
                self._append_log(f"MiKTeX 就绪: {miktex_bin or '已安装'}")
                self._finish("success", "MiKTeX 安装完成并已识别", detected_bin=miktex_bin)
                return
            self._append_log(f"winget 返回码 {rc}，正在为您打开 MiKTeX 官方下载页面作为备选...")
        else:
            self._append_log("未检测到 winget，正在为您打开 MiKTeX 官方下载页面...")

        webbrowser.open("https://miktex.org/download")
        self._finish("error", "自动安装未完成，已打开 MiKTeX 官方下载页，请手动安装后点击「自动扫描」")

    def _do_install_miktex_packages(self, compiler_settings: dict) -> None:
        env = build_tex_env(compiler_settings)
        miktex_exe = which_tool("miktex", compiler_settings)
        if not miktex_exe:
            for item in scan_local_tex_installations(force_refresh=True):
                candidate = Path(item["bin_dir"]) / ("miktex.exe" if os.name == "nt" else "miktex")
                if candidate.is_file():
                    miktex_exe = str(candidate)
                    break
        if not miktex_exe:
            raise RuntimeError("未检测到 MiKTeX 管理器，请先安装或配置 MiKTeX 目录。")

        self._append_log("正在刷新 MiKTeX 宏包数据库...", "刷新宏包索引")
        self._stream_process([miktex_exe, "packages", "update-package-database"], env=env)

        total = len(CORE_MIKTEX_PACKAGES)
        installed_ok = 0
        for idx, pkg in enumerate(CORE_MIKTEX_PACKAGES, start=1):
            if self._cancel_flag.is_set():
                self._finish("error", "用户已取消宏包安装")
                return
            pct = f"安装宏包 {idx}/{total}: {pkg}"
            self._append_log(f"-> 检查并安装宏包 {pkg}...", pct)
            rc = self._stream_process([miktex_exe, "packages", "install", pkg], env=env)
            if rc == 0:
                installed_ok += 1
        self._finish("success", f"已完成常用宏包配置（成功/已存在 {installed_ok}/{total} 个）")

    def _download_file(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": "RebuttalSidecar/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            last_pct = -10
            with tmp_dest.open("wb") as f:
                while True:
                    if self._cancel_flag.is_set():
                        raise RuntimeError("用户取消下载")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    mb = downloaded / (1024 * 1024)
                    if total > 0:
                        pct = int(downloaded * 100 / total)
                        if pct - last_pct >= 10 or downloaded == total:
                            last_pct = pct
                            self._append_log(
                                f"已下载 {mb:.1f} / {total / (1024 * 1024):.1f} MB ({pct}%)",
                                f"下载安装器 {pct}%",
                            )
                    else:
                        self._append_log(f"已下载 {mb:.1f} MB...", f"下载 {mb:.1f} MB")
        os.replace(tmp_dest, dest)

    def _do_install_texlive(self, options: dict) -> None:
        mirror_key = str(options.get("mirror", "tuna")).strip().lower()
        mirror = CTAN_MIRRORS.get(mirror_key)
        if not mirror:
            raise ValueError(f"未知的 TeX Live 镜像源: {mirror_key}")

        mirror_url = mirror["url"]
        mode = str(options.get("mode", "gui")).strip().lower()
        installer_dir = Path(tempfile.gettempdir()) / "RebuttalSidecar" / "installers"
        installer_exe = installer_dir / "install-tl-windows.exe"

        self._append_log(f"使用镜像源 [{mirror['label']}]: {mirror_url}", "准备下载 TeX Live 安装器")
        self._download_file(f"{mirror_url}/install-tl-windows.exe", installer_exe)

        if self._cancel_flag.is_set():
            self._finish("error", "用户已取消 TeX Live 安装")
            return

        if mode == "unattended":
            target_dir = str(options.get("install_dir") or (Path.home() / "texlive")).strip()
            profile_path = installer_dir / "texlive.profile"
            profile_lines = [
                f"# Rebuttal Sidecar TeX Live unattended profile",
                "selected_scheme scheme-medium",
                f"TEXDIR {target_dir}/2026",
                f"TEXMFCONFIG ~/.texlive2026/texmf-config",
                f"TEXMFVAR ~/.texlive2026/texmf-var",
                "option_doc 0",
                "option_src 0",
            ]
            profile_path.write_text("\n".join(profile_lines) + "\n", encoding="utf-8")
            self._append_log(f"启动无人值守中等方案安装 (scheme-medium -> {target_dir})，耗时较长请耐心等待...", "静默安装中")
            rc = self._stream_process(
                [
                    str(installer_exe),
                    "-no-gui",
                    "-profile",
                    str(profile_path),
                    "-repository",
                    mirror_url,
                ]
            )
            installs = scan_local_tex_installations(force_refresh=True)
            tl_bin = next((i["bin_dir"] for i in installs if i["distro"] == "TeX Live"), "")
            if rc != 0 and not tl_bin:
                raise RuntimeError(f"TeX Live 安装退出码: {rc}")
            self._finish("success", "TeX Live 安装完成并已识别", detected_bin=tl_bin)
            return

        # GUI mode: launch interactive TeX Live installer pre-configured with fast CTAN mirror
        self._append_log("正在启动 TeX Live 官方图形安装向导（已预填高速镜像源）...", "已拉起安装向导")
        subprocess.Popen([str(installer_exe), "-repository", mirror_url])
        self._finish("success", "已启动 TeX Live 官方安装向导；完成安装后请点击上方「自动扫描」绑定路径")
