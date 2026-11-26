"""Application configuration persistence (%APPDATA%/RebuttalSidecar/config.json)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

VALID_ENGINES = ("auto", "xelatex", "pdflatex", "lualatex")
VALID_RECIPES = ("auto", "latexmk", "engine_bibtex", "engine_biber", "engine_only")

DEFAULT_COMPILER_SETTINGS: dict = {
    "auto_compile": False,
    "debounce_ms": 800,
    "tex_bin_dir": "",
    "perl_path": "",
    "default_engine": "auto",
    "recipe": "auto",
    "extra_args": "",
    "compile_timeout": 180,
}


def default_config_path() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / "RebuttalSidecar" / "config.json"


def _normalize_compiler(raw: dict | None) -> dict:
    merged = dict(DEFAULT_COMPILER_SETTINGS)
    if not raw:
        return merged

    if "auto_compile" in raw:
        merged["auto_compile"] = bool(raw["auto_compile"])
    if "debounce_ms" in raw:
        merged["debounce_ms"] = max(200, min(10000, int(raw["debounce_ms"])))
    if "tex_bin_dir" in raw:
        merged["tex_bin_dir"] = str(raw["tex_bin_dir"]).strip()
    if "perl_path" in raw:
        merged["perl_path"] = str(raw["perl_path"]).strip()
    if raw.get("default_engine") in VALID_ENGINES:
        merged["default_engine"] = raw["default_engine"]
    if raw.get("recipe") in VALID_RECIPES:
        merged["recipe"] = raw["recipe"]
    if "extra_args" in raw:
        merged["extra_args"] = str(raw["extra_args"]).strip()
    if "compile_timeout" in raw:
        merged["compile_timeout"] = max(30, min(900, int(raw["compile_timeout"])))
    return merged


class AppConfig:
    """Manages persistent user settings and recent workspaces."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()
        self.data = self._load()

    def _load(self) -> dict:
        default = {
            "last_workspace": "",
            "recent_workspaces": [],
            "compiler": dict(DEFAULT_COMPILER_SETTINGS),
        }
        if not self.path.exists():
            return default
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

        recent = [str(p) for p in raw.get("recent_workspaces", []) if str(p).strip()]
        return {
            "last_workspace": str(raw.get("last_workspace", "")),
            "recent_workspaces": recent[:10],
            "compiler": _normalize_compiler(raw.get("compiler")),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)

    @property
    def compiler(self) -> dict:
        return deepcopy(self.data["compiler"])

    @property
    def last_workspace(self) -> str:
        return self.data["last_workspace"]

    @property
    def recent_workspaces(self) -> list[str]:
        return list(self.data["recent_workspaces"])

    def set_workspace(self, workspace_path: str) -> None:
        normalized = str(Path(workspace_path).resolve())
        recent = [p for p in self.data["recent_workspaces"] if p.lower() != normalized.lower()]
        recent.insert(0, normalized)
        self.data["last_workspace"] = normalized
        self.data["recent_workspaces"] = recent[:10]
        self.save()

    def update_compiler(self, patch: dict) -> dict:
        combined = dict(self.data["compiler"])
        combined.update(patch)
        self.data["compiler"] = _normalize_compiler(combined)
        self.save()
        return self.compiler
