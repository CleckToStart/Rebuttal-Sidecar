"""Atomic .rebuttal_todo.json manager and response letter exporter."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from config import VALID_ENGINES

TODO_FILENAME = ".rebuttal_todo.json"
VALID_STATUSES = ("pending", "agent_done", "verified")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_item(item: dict) -> dict:
    status = item.get("status", "pending")
    if status not in VALID_STATUSES:
        status = "pending"
    return {
        "id": str(item.get("id", "")).strip(),
        "reviewer": str(item.get("reviewer", "Reviewer #1")).strip() or "Reviewer #1",
        "summary": str(item.get("summary", "")).strip(),
        "target_location": str(item.get("target_location", "")).strip(),
        "status": status,
        "response_draft": str(item.get("response_draft", "")),
        "updated_at": str(item.get("updated_at") or _utc_now_iso()),
    }


def _next_item_id(reviewer: str, existing_items: list[dict]) -> str:
    existing_ids = {item["id"] for item in existing_items}
    match = re.search(r"(\d+)", reviewer)
    prefix = f"R{match.group(1)}-C" if match else "C"
    idx = 1
    while f"{prefix}{idx}" in existing_ids:
        idx += 1
    return f"{prefix}{idx}"


class TodoManager:
    """Handles atomic CRUD on per-manuscript .rebuttal_todo.json files."""

    def __init__(self) -> None:
        self._recent_writes: dict[Path, float] = {}

    def was_written_recently(self, file_path: Path, window_sec: float = 0.8) -> bool:
        ts = self._recent_writes.get(file_path.resolve())
        if ts is None:
            return False
        return (time.monotonic() - ts) <= window_sec

    def _default_doc(self, manuscript_dir: Path) -> dict:
        return {
            "manuscript": manuscript_dir.name,
            "active_baseline": None,
            "engine": "auto",
            "items": [],
        }

    def load(self, manuscript_dir: Path) -> dict:
        todo_path = manuscript_dir / TODO_FILENAME
        doc = self._default_doc(manuscript_dir)
        if not todo_path.is_file():
            return doc

        try:
            raw = json.loads(todo_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return doc

        baseline = raw.get("active_baseline")
        if isinstance(baseline, dict) and baseline.get("type") in ("git", "archive") and baseline.get("ref"):
            doc["active_baseline"] = {"type": baseline["type"], "ref": str(baseline["ref"])}

        engine = raw.get("engine", "auto")
        if engine in VALID_ENGINES:
            doc["engine"] = engine

        doc["items"] = [_normalize_item(item) for item in raw.get("items", []) if isinstance(item, dict)]
        return doc

    def save(self, manuscript_dir: Path, data: dict) -> dict:
        todo_path = (manuscript_dir / TODO_FILENAME).resolve()
        tmp_path = todo_path.with_name(f"{TODO_FILENAME}.tmp")

        baseline = data.get("active_baseline")
        if not (isinstance(baseline, dict) and baseline.get("type") in ("git", "archive") and baseline.get("ref")):
            baseline = None

        engine = data.get("engine", "auto")
        if engine not in VALID_ENGINES:
            engine = "auto"

        clean_doc = {
            "manuscript": manuscript_dir.name,
            "active_baseline": baseline,
            "engine": engine,
            "items": [_normalize_item(item) for item in data.get("items", [])],
        }

        self._recent_writes[todo_path] = time.monotonic()
        tmp_path.write_text(json.dumps(clean_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, todo_path)
        return clean_doc

    def add_item(self, manuscript_dir: Path, payload: dict) -> dict:
        doc = self.load(manuscript_dir)
        item = _normalize_item(payload)
        if not item["id"]:
            item["id"] = _next_item_id(item["reviewer"], doc["items"])
        elif any(existing["id"] == item["id"] for existing in doc["items"]):
            raise ValueError(f"Duplicate todo id: {item['id']}")
        item["updated_at"] = _utc_now_iso()
        doc["items"].append(item)
        return self.save(manuscript_dir, doc)

    def update_item(self, manuscript_dir: Path, item_id: str, patch: dict) -> dict:
        doc = self.load(manuscript_dir)
        for idx, current in enumerate(doc["items"]):
            if current["id"] == item_id:
                merged = dict(current)
                for key in ("id", "reviewer", "summary", "target_location", "status", "response_draft"):
                    if key in patch:
                        merged[key] = patch[key]
                merged["updated_at"] = _utc_now_iso()
                doc["items"][idx] = _normalize_item(merged)
                return self.save(manuscript_dir, doc)
        raise KeyError(f"Todo item not found: {item_id}")

    def delete_item(self, manuscript_dir: Path, item_id: str) -> dict:
        doc = self.load(manuscript_dir)
        remaining = [item for item in doc["items"] if item["id"] != item_id]
        if len(remaining) == len(doc["items"]):
            raise KeyError(f"Todo item not found: {item_id}")
        doc["items"] = remaining
        return self.save(manuscript_dir, doc)

    def set_preferences(
        self,
        manuscript_dir: Path,
        active_baseline: dict | None = ...,
        engine: str | None = None,
    ) -> dict:
        doc = self.load(manuscript_dir)
        if active_baseline is not ...:
            doc["active_baseline"] = active_baseline
        if engine is not None and engine in VALID_ENGINES:
            doc["engine"] = engine
        return self.save(manuscript_dir, doc)

    def export_response_letter(self, manuscript_dir: Path, fmt: str = "md") -> str:
        doc = self.load(manuscript_dir)
        grouped: dict[str, list[dict]] = {}
        for item in doc["items"]:
            grouped.setdefault(item["reviewer"], []).append(item)

        if fmt == "tex":
            lines = [f"% Response Letter Draft for {doc['manuscript']}"]
            for reviewer, items in grouped.items():
                lines.append(f"\\subsection*{{{reviewer}}}")
                for item in items:
                    loc = f" ({item['target_location']})" if item["target_location"] else ""
                    lines.append(f"\\paragraph{{[{item['id']}] {item['summary']}{loc}}}")
                    lines.append(item["response_draft"] or "% TODO: draft response")
                    lines.append("")
            return "\n".join(lines).rstrip() + "\n"

        lines = [f"# Response Letter — {doc['manuscript']}", ""]
        for reviewer, items in grouped.items():
            lines.append(f"## {reviewer}")
            lines.append("")
            for item in items:
                loc = f" | Location: `{item['target_location']}`" if item["target_location"] else ""
                lines.append(f"### [{item['id']}] {item['summary']}")
                lines.append(f"- Status: `{item['status']}`{loc}")
                lines.append("")
                lines.append(item["response_draft"] or "_Pending response draft._")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"
