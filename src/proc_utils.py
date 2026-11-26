"""Subprocess execution and TeX/Perl toolchain resolution helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

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


def build_tex_env(compiler_settings: dict | None = None) -> dict[str, str]:
    """Build child environment with configured TeX bin dir and Perl fallback."""
    settings = compiler_settings or {}
    env = os.environ.copy()
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]

    tex_bin_dir = str(settings.get("tex_bin_dir", "")).strip()
    if tex_bin_dir and Path(tex_bin_dir).is_dir():
        path_parts.insert(0, tex_bin_dir)

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


MIKTEX_FLAG_TOOLS = {"xelatex", "pdflatex", "lualatex", "latexmk", "latexdiff", "bibtex"}


def miktex_installer_flags(exe_path: str | None, compiler_settings: dict | None = None) -> list[str]:
    """Return MiKTeX non-interactive installer flag when running a MiKTeX binary."""
    if not exe_path or "miktex" not in exe_path.lower():
        return []
    if Path(exe_path).stem.lower() not in MIKTEX_FLAG_TOOLS:
        return []
    auto_install = bool((compiler_settings or {}).get("miktex_auto_install", False))
    return ["--miktex-enable-installer"] if auto_install else ["--miktex-disable-installer"]


def check_toolchain(compiler_settings: dict | None = None) -> dict:
    """Probe availability and resolved paths for TeX, Perl, and Git tools."""
    env = build_tex_env(compiler_settings)
    tools = {}
    for name in TOOLCHAIN_BINARIES:
        resolved = shutil.which(name, path=env.get("PATH"))
        available = resolved is not None
        note = ""
        if resolved and miktex_installer_flags(resolved):
            probe_arg = "-v" if name == "latexmk" else "--version"
            try:
                res = run_cmd([resolved, "--miktex-disable-installer", probe_arg], timeout=5, env=env)
                if res.returncode != 0:
                    available = False
                    note = "MiKTeX stub (package not installed)"
            except (OSError, subprocess.TimeoutExpired):
                available = False
                note = "MiKTeX probe failed"
        tools[name] = {
            "available": available,
            "path": resolved or "",
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
