"""Application configuration persistence (%APPDATA%/RebuttalSidecar/config.json)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

VALID_ENGINES = ("auto", "xelatex", "pdflatex", "lualatex")
VALID_RECIPES = ("auto", "latexmk", "engine_bibtex", "engine_biber", "engine_only")
MAX_HISTORY = 25


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sort_history(items: list[dict]) -> list[dict]:
    pinned = [item for item in items if item.get("pinned")]
    regular = [item for item in items if not item.get("pinned")]
    return (pinned + regular)[:MAX_HISTORY]


def _normalize_history_item(raw: dict) -> dict | None:
    path_str = str(raw.get("path", "")).strip()
    if not path_str:
        return None
    p = Path(path_str)
    return {
        "path": path_str,
        "name": str(raw.get("name") or p.name or path_str),
        "last_manuscript": str(raw.get("last_manuscript", "")),
        "manuscript_count": max(0, int(raw.get("manuscript_count", 0))),
        "pinned": bool(raw.get("pinned", False)),
        "updated_at": str(raw.get("updated_at") or _utc_now_iso()),
    }

DEFAULT_COMPILER_SETTINGS: dict = {
    "auto_compile": False,
    "debounce_ms": 800,
    "tex_bin_dir": "",
    "perl_path": "",
    "default_engine": "auto",
    "recipe": "auto",
    "extra_args": "",
    "compile_timeout": 180,
    "miktex_auto_install": False,
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
    if "miktex_auto_install" in raw:
        merged["miktex_auto_install"] = bool(raw["miktex_auto_install"])
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
            "workspace_history": [],
            "compiler": dict(DEFAULT_COMPILER_SETTINGS),
        }
        if not self.path.exists():
            return default
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

        history: list[dict] = []
        raw_history = raw.get("workspace_history")
        if isinstance(raw_history, list):
            for entry in raw_history:
                if isinstance(entry, dict):
                    norm = _normalize_history_item(entry)
                    if norm:
                        history.append(norm)
        else:
            for p in raw.get("recent_workspaces", []):
                norm = _normalize_history_item({"path": str(p)})
                if norm:
                    history.append(norm)

        history = _sort_history(history)
        return {
            "last_workspace": str(raw.get("last_workspace", "")),
            "recent_workspaces": [item["path"] for item in history][:10],
            "workspace_history": history,
            "compiler": _normalize_compiler(raw.get("compiler")),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["recent_workspaces"] = [item["path"] for item in self.data["workspace_history"]][:10]
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
        return [item["path"] for item in self.data["workspace_history"][:10]]

    @property
    def workspace_history(self) -> list[dict]:
        return deepcopy(self.data["workspace_history"])

    def get_last_manuscript(self, workspace_path: str) -> str:
        normalized = str(Path(workspace_path).resolve()).lower()
        for item in self.data["workspace_history"]:
            if item["path"].lower() == normalized:
                return item.get("last_manuscript", "")
        return ""

    def record_workspace(
        self,
        workspace_path: str,
        manuscript_count: int = 0,
        active_manuscript: str = "",
        custom_name: str = "",
    ) -> None:
        normalized = str(Path(workspace_path).resolve())
        existing = next(
            (item for item in self.data["workspace_history"] if item["path"].lower() == normalized.lower()),
            None,
        )
        others = [item for item in self.data["workspace_history"] if item["path"].lower() != normalized.lower()]
        resolved_name = custom_name.strip() or (existing["name"] if existing else (Path(normalized).name or normalized))
        entry = {
            "path": normalized,
            "name": resolved_name,
            "last_manuscript": active_manuscript or (existing["last_manuscript"] if existing else ""),
            "manuscript_count": manuscript_count,
            "pinned": bool(existing["pinned"]) if existing else False,
            "updated_at": _utc_now_iso(),
        }
        self.data["last_workspace"] = normalized
        self.data["workspace_history"] = _sort_history([entry, *others])
        self.save()

    def rename_workspace(self, workspace_path: str, new_name: str) -> list[dict]:
        clean = new_name.strip()
        if not clean:
            return self.workspace_history
        normalized = str(Path(workspace_path).resolve()).lower()
        for item in self.data["workspace_history"]:
            if item["path"].lower() == normalized:
                item["name"] = clean
                item["updated_at"] = _utc_now_iso()
                self.save()
                break
        return self.workspace_history

    def set_workspace(self, workspace_path: str) -> None:
        self.record_workspace(workspace_path)

    def set_last_manuscript(self, workspace_path: str, manuscript_name: str) -> None:
        normalized = str(Path(workspace_path).resolve()).lower()
        for item in self.data["workspace_history"]:
            if item["path"].lower() == normalized:
                item["last_manuscript"] = manuscript_name
                item["updated_at"] = _utc_now_iso()
                self.save()
                return

    def set_workspace_pinned(self, workspace_path: str, pinned: bool) -> list[dict]:
        normalized = str(Path(workspace_path).resolve()).lower()
        for item in self.data["workspace_history"]:
            if item["path"].lower() == normalized:
                item["pinned"] = bool(pinned)
                break
        self.data["workspace_history"] = _sort_history(self.data["workspace_history"])
        self.save()
        return self.workspace_history

    def remove_workspace(self, workspace_path: str) -> list[dict]:
        normalized = str(Path(workspace_path).resolve()).lower()
        self.data["workspace_history"] = [
            item for item in self.data["workspace_history"] if item["path"].lower() != normalized
        ]
        if self.data["last_workspace"].lower() == normalized:
            self.data["last_workspace"] = self.data["workspace_history"][0]["path"] if self.data["workspace_history"] else ""
        self.save()
        return self.workspace_history

    def update_compiler(self, patch: dict) -> dict:
        combined = dict(self.data["compiler"])
        combined.update(patch)
        self.data["compiler"] = _normalize_compiler(combined)
        self.save()
        return self.compiler
