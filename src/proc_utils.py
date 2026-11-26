"""Subprocess execution and TeX/Perl toolchain resolution helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

if os.name == "nt":
    import winreg
else:
    winreg = None

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TOOLCHAIN_BINARIES = (
    "xelatex",
    "pdflatex",
    "lualatex",
    "latexmk",
    "latexdiff",
    "bibtex",
    "biber",
    "perl",
    "git",
)


@lru_cache(maxsize=1)
def _discover_git_perl_dir() -> str | None:
    """Find Git for Windows bundled usr/bin directory if perl.exe exists there."""
    git_exe = shutil.which("git")
    if not git_exe:
        return None
    # Typical layout: <GitRoot>/cmd/git.exe -> <GitRoot>/usr/bin/perl.exe
    git_path = Path(git_exe).resolve()
    for parent in (git_path.parent.parent, git_path.parent):
        candidate = parent / "usr" / "bin"
        if (candidate / "perl.exe").is_file() or (candidate / "perl").is_file():
            return str(candidate)
    return None


_AUTO_SCAN_CACHE: tuple[float, list[dict]] | None = None


def _read_windows_registry_paths() -> list[str]:
    if winreg is None:
        return []
    found: list[str] = []
    targets = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for hive, subkey in targets:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                val, _ = winreg.QueryValueEx(key, "Path")
                for part in str(val).split(os.pathsep):
                    clean = part.strip().strip('"')
                    if clean and ("miktex" in clean.lower() or "texlive" in clean.lower()):
                        found.append(clean)
        except OSError:
            continue
    return found


def scan_local_tex_installations(force_refresh: bool = False) -> list[dict]:
    """Discover local MiKTeX / TeX Live bin directories from registry PATH and standard locations."""
    global _AUTO_SCAN_CACHE
    now = time.monotonic()
    if not force_refresh and _AUTO_SCAN_CACHE is not None and (now - _AUTO_SCAN_CACHE[0]) < 5.0:
        return _AUTO_SCAN_CACHE[1]

    candidates: list[Path] = [Path(p) for p in _read_windows_registry_paths()]

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64")

    home = Path.home()
    for base in (home / "texlive",):
        if base.is_dir():
            for year_dir in sorted(base.iterdir(), key=lambda p: p.name, reverse=True):
                candidates.append(year_dir / "bin" / "windows")

    drives = ["C:", "D:", "E:", "F:"] if os.name == "nt" else []
    for drv in drives:
        root = Path(f"{drv}\\")
        if not root.exists():
            continue
        candidates.extend(
            [
                root / "Program Files" / "MiKTeX" / "miktex" / "bin" / "x64",
                root / "MiKTeX" / "miktex" / "bin" / "x64",
                root / "MiKTeX29" / "miktex" / "bin" / "x64",
            ]
        )
        tl_root = root / "texlive"
        if tl_root.is_dir():
            for year_dir in sorted(tl_root.iterdir(), key=lambda p: p.name, reverse=True):
                candidates.append(year_dir / "bin" / "windows")

    if os.name != "nt":
        for p in ("/usr/bin", "/usr/local/bin", "/Library/TeX/texbin"):
            candidates.append(Path(p))

    seen: set[str] = set()
    results: list[dict] = []
    for cand in candidates:
        try:
            resolved = str(cand.resolve())
        except OSError:
            continue
        norm = resolved.lower()
        if norm in seen or not cand.is_dir():
            continue
        has_pdf = bool(shutil.which("pdflatex", path=resolved))
        has_xe = bool(shutil.which("xelatex", path=resolved))
        if not (has_pdf or has_xe):
            continue
        seen.add(norm)
        distro = "MiKTeX" if "miktex" in norm else ("TeX Live" if "texlive" in norm else "TeX")
        results.append(
            {
                "distro": distro,
                "bin_dir": resolved,
                "has_pdflatex": has_pdf,
                "has_xelatex": has_xe,
                "has_latexmk": bool(shutil.which("latexmk", path=resolved)),
            }
        )

    _AUTO_SCAN_CACHE = (now, results)
    return results


def build_tex_env(compiler_settings: dict | None = None) -> dict[str, str]:
    """Build child environment with configured TeX bin dir, auto-scanned fallback, and Perl fallback."""
    settings = compiler_settings or {}
    env = os.environ.copy()
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]

    tex_bin_dir = str(settings.get("tex_bin_dir", "")).strip()
    if tex_bin_dir and Path(tex_bin_dir).is_dir():
        path_parts.insert(0, tex_bin_dir)
    elif not shutil.which("pdflatex", path=os.pathsep.join(path_parts)):
        for found in scan_local_tex_installations():
            path_parts.insert(0, found["bin_dir"])
            break

    perl_path = str(settings.get("perl_path", "")).strip()
    if perl_path:
        perl_p = Path(perl_path)
        perl_dir = str(perl_p.parent if perl_p.is_file() else perl_p)
        if Path(perl_dir).is_dir():
            path_parts.insert(0, perl_dir)
    elif not shutil.which("perl", path=os.pathsep.join(path_parts)):
        # Append at end so Git's usr/bin utilities (find, sort) never shadow Windows commands.
        git_perl_dir = _discover_git_perl_dir()
        if git_perl_dir and git_perl_dir not in path_parts:
            path_parts.append(git_perl_dir)

    env["PATH"] = os.pathsep.join(path_parts)
    return env


def which_tool(name: str, compiler_settings: dict | None = None) -> str | None:
    """Resolve executable path using the configured TeX environment."""
    env = build_tex_env(compiler_settings)
    return shutil.which(name, path=env.get("PATH"))


MIKTEX_FLAG_TOOLS = {"xelatex", "pdflatex", "lualatex", "latexmk", "latexdiff", "latexdiff-so", "bibtex"}


def miktex_installer_flags(exe_path: str | None, compiler_settings: dict | None = None) -> list[str]:
    """Return MiKTeX non-interactive installer flag when running a MiKTeX binary."""
    if not exe_path or "miktex" not in exe_path.lower():
        return []
    if Path(exe_path).stem.lower() not in MIKTEX_FLAG_TOOLS:
        return []
    auto_install = bool((compiler_settings or {}).get("miktex_auto_install", False))
    return ["--miktex-enable-installer"] if auto_install else ["--miktex-disable-installer"]


def _probe_binary(name: str, env: dict[str, str]) -> tuple[bool, str, str]:
    resolved = shutil.which(name, path=env.get("PATH"))
    if not resolved:
        return False, "", ""
    if miktex_installer_flags(resolved):
        probe_arg = "-v" if name == "latexmk" else "--version"
        try:
            res = run_cmd([resolved, "--miktex-disable-installer", probe_arg], timeout=5, env=env)
            if res.returncode != 0:
                return False, resolved, "MiKTeX stub (package not installed)"
        except (OSError, subprocess.TimeoutExpired):
            return False, resolved, "MiKTeX probe failed"
    return True, resolved, ""


def resolve_latexdiff(compiler_settings: dict | None = None) -> str | None:
    """Return a working latexdiff executable, preferring standalone latexdiff-so."""
    env = build_tex_env(compiler_settings)
    for candidate in ("latexdiff-so", "latexdiff"):
        ok, path, _ = _probe_binary(candidate, env)
        if ok:
            return path
    return None


def check_toolchain(compiler_settings: dict | None = None) -> dict:
    """Probe availability and resolved paths for TeX, Perl, and Git tools."""
    env = build_tex_env(compiler_settings)
    tools = {}
    for name in TOOLCHAIN_BINARIES:
        if name == "latexdiff":
            ok, resolved, note = _probe_binary("latexdiff-so", env)
            if not ok:
                ok, resolved, note = _probe_binary("latexdiff", env)
        else:
            ok, resolved, note = _probe_binary(name, env)
        tools[name] = {
            "available": ok,
            "path": resolved,
            "note": note,
        }
    return {"tools": tools}


def run_cmd(
    args: list[str],
    cwd: Path | str | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a subprocess safely without console windows or stdin blocking."""
    kwargs: dict = {
        "cwd": str(cwd) if cwd else None,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "timeout": timeout,
        "creationflags": CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    if text:
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    return subprocess.run(args, **kwargs)
