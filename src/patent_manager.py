"""Patent workspace initialization, figures & reference numerals, fuzzy feedback CRUD, and demo generator."""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import tempfile
import threading
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from docx_engine import (
    create_sample_redline_docx,
    diff_document_blocks,
    export_markdown_to_docx,
    parse_docx,
    parse_markdown_blocks,
)
from patent_timeline import compute_patent_schedule
from patent_vcs import ensure_patent_repo, list_snapshots, read_snapshot_file

META_FILENAME = ".patent_meta.json"
FEEDBACK_FILENAME = ".patent_feedback.json"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".bmp"}

VALID_FEEDBACK_STATUSES = ("pending", "need_confirm", "revised", "resolved")
VALID_FEEDBACK_SOURCES = ("agent", "examiner", "internal")
VALID_FEEDBACK_CATEGORIES = (
    "claim_scope",
    "tech_detail",
    "figure_mark",
    "novelty_inventive",
    "clarify_needed",
)

DEFAULT_DISCLOSURE_TEMPLATE = """# 技术交底书

## 一、发明创造名称
（在此填写简明、准确的专利名称，例如：一种基于……的方法、装置及电子设备）

## 二、所属技术领域
本发明涉及……技术领域，具体而言，涉及一种……。

## 三、背景技术与现有技术缺陷
1. 现有技术通常采用……方式解决上述问题。
2. 然而，现有技术至少存在以下不足：……（技术缺陷一、技术缺陷二）。

## 四、发明内容
### 4.1 拟解决的技术问题
针对上述现有技术存在的不足，本发明提供一种……，旨在解决……问题。

### 4.2 技术方案
为实现上述目的，本发明采用如下技术方案：
一种……方法，包括以下步骤：
- 步骤S1：通过数据采集模块（101）获取……；
- 步骤S2：……

### 4.3 有益效果
与现有技术相比，本发明具有以下有益效果：
1. ……

## 五、附图说明
- 图1为本发明实施例提供的系统架构示意图；

## 六、具体实施方式
下面结合附图和具体实施例对本发明作进一步详细说明。
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_meta(title: str = "", patent_type: str = "invention") -> dict:
    return {
        "title": title,
        "patent_type": patent_type if patent_type in ("invention", "utility_model", "design") else "invention",
        "application_no": "",
        "inventors": "",
        "agency": "",
        "stage": "drafting",
        "filing_date": "",
        "grant_date": "",
        "fee_reduction": 0,
        "figures": [],
        "reference_numerals": [],
        "fee_payments": [],
        "custom_deadlines": [],
    }


def _default_feedback(title: str = "") -> dict:
    return {
        "patent": title,
        "raw_batches": [],
        "items": [],
    }


class PatentManager:
    """Manages patent folder documents, metadata, fuzzy feedback, figures, and numerals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._self_writes: dict[Path, float] = {}

    def was_written_recently(self, path: Path, window_sec: float = 0.6) -> bool:
        resolved = path.resolve()
        with self._lock:
            ts = self._self_writes.get(resolved)
        return ts is not None and (time.monotonic() - ts) < window_sec

    def _write_json_atomic(self, target: Path, payload: dict) -> None:
        resolved = target.resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_name(f"{resolved.name}.tmp")
        with self._lock:
            self._self_writes[resolved] = time.monotonic()
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, resolved)
        with self._lock:
            self._self_writes[resolved] = time.monotonic()

    def load_meta(self, patent_dir: Path) -> dict:
        meta_path = patent_dir / META_FILENAME
        base = _default_meta(title=patent_dir.name)
        if not meta_path.is_file():
            return base
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return base
        if not isinstance(raw, dict):
            return base
        for k in base:
            if k in raw and raw[k] is not None:
                base[k] = raw[k]
        return base

    def save_meta(self, patent_dir: Path, patch: dict) -> dict:
        cur = self.load_meta(patent_dir)
        for key, val in patch.items():
            if key in cur:
                cur[key] = val
        self._write_json_atomic(patent_dir / META_FILENAME, cur)
        return cur

    def load_feedback(self, patent_dir: Path, title: str = "") -> dict:
        fb_path = patent_dir / FEEDBACK_FILENAME
        base = _default_feedback(title or patent_dir.name)
        if not fb_path.is_file():
            return base
        try:
            raw = json.loads(fb_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return base
        if not isinstance(raw, dict):
            return base
        base["patent"] = str(raw.get("patent") or base["patent"])
        base["raw_batches"] = raw.get("raw_batches") if isinstance(raw.get("raw_batches"), list) else []
        base["items"] = raw.get("items") if isinstance(raw.get("items"), list) else []
        return base

    def save_feedback(self, patent_dir: Path, doc: dict) -> dict:
        normalized = {
            "patent": str(doc.get("patent") or patent_dir.name),
            "raw_batches": doc.get("raw_batches") if isinstance(doc.get("raw_batches"), list) else [],
            "items": doc.get("items") if isinstance(doc.get("items"), list) else [],
        }
        self._write_json_atomic(patent_dir / FEEDBACK_FILENAME, normalized)
        return normalized

    def scan_documents(self, patent_dir: Path) -> list[dict]:
        """List .md and .docx files in root and submissions/ while skipping Word ~$ lock files."""
        docs: list[dict] = []
        search_dirs = [patent_dir, patent_dir / "submissions"]
        seen: set[str] = set()
        for folder in search_dirs:
            if not folder.is_dir():
                continue
            for item in sorted(folder.iterdir(), key=lambda p: (p.suffix.lower() != ".md", p.name.lower())):
                if not item.is_file() or item.name.startswith((".", "~$")):
                    continue
                suf = item.suffix.lower()
                if suf not in (".md", ".docx"):
                    continue
                rel = item.relative_to(patent_dir).as_posix()
                if rel in seen:
                    continue
                seen.add(rel)
                docs.append(
                    {
                        "rel_path": rel,
                        "name": item.name,
                        "ext": suf.lstrip("."),
                        "size": item.stat().st_size,
                        "mtime": item.stat().st_mtime,
                    }
                )
        docs.sort(key=lambda d: (d["rel_path"] != "disclosure.md", d["ext"] != "md", d["rel_path"].lower()))
        return docs

    def scan_figures(self, patent_dir: Path, meta: dict) -> list[dict]:
        """Scan figures/ directory and merge user-defined ordering and captions."""
        figs_dir = patent_dir / "figures"
        if not figs_dir.is_dir():
            return []

        meta_map: dict[str, dict] = {}
        for idx, entry in enumerate(meta.get("figures", []) or []):
            if isinstance(entry, dict) and entry.get("filename"):
                meta_map[entry["filename"]] = {"order": idx, **entry}

        discovered: list[dict] = []
        for item in sorted(figs_dir.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file() or item.name.startswith(".") or item.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            saved = meta_map.get(item.name, {})
            discovered.append(
                {
                    "filename": item.name,
                    "rel_path": f"figures/{item.name}",
                    "caption": str(saved.get("caption") or item.stem),
                    "order": saved.get("order", 9999),
                    "size": item.stat().st_size,
                }
            )
        discovered.sort(key=lambda x: (x["order"], x["filename"].lower()))
        for idx, item in enumerate(discovered, start=1):
            item["fig_no"] = idx
        return discovered

    def init_or_scan_patent(
        self,
        path_str: str,
        title: str = "",
        patent_type: str = "invention",
        advance_days: int = 30,
    ) -> dict:
        """Initialize missing patent scaffolding/repo and return complete workspace payload."""
        patent_dir = Path(path_str).expanduser().resolve()
        patent_dir.mkdir(parents=True, exist_ok=True)
        (patent_dir / "figures").mkdir(exist_ok=True)
        (patent_dir / "submissions").mkdir(exist_ok=True)

        meta_file = patent_dir / META_FILENAME
        is_brand_new = not meta_file.is_file() and not (patent_dir / "disclosure.md").is_file()
        meta = self.load_meta(patent_dir)
        if title.strip():
            meta["title"] = title.strip()
        if patent_type in ("invention", "utility_model", "design") and is_brand_new:
            meta["patent_type"] = patent_type
        if not meta_file.is_file():
            self._write_json_atomic(meta_file, meta)

        if is_brand_new:
            (patent_dir / "disclosure.md").write_text(DEFAULT_DISCLOSURE_TEMPLATE, encoding="utf-8")

        fb_file = patent_dir / FEEDBACK_FILENAME
        feedback = self.load_feedback(patent_dir, meta["title"])
        if not fb_file.is_file():
            self._write_json_atomic(fb_file, feedback)

        ensure_patent_repo(patent_dir)
        return self.get_patent_payload(patent_dir, advance_days=advance_days)

    def get_patent_payload(self, patent_dir: Path, advance_days: int = 30) -> dict:
        meta = self.load_meta(patent_dir)
        feedback = self.load_feedback(patent_dir, meta["title"])
        documents = self.scan_documents(patent_dir)
        figures = self.scan_figures(patent_dir, meta)
        schedule = compute_patent_schedule(meta, advance_days=advance_days)
        snapshots = list_snapshots(patent_dir)
        numeral_check = self.check_reference_numerals(patent_dir, meta.get("reference_numerals"))

        return {
            "patent_dir": str(patent_dir),
            "meta": meta,
            "feedback": feedback,
            "documents": documents,
            "figures": figures,
            "schedule": schedule,
            "snapshots": snapshots,
            "numeral_check": numeral_check,
        }

    def preview_document(self, patent_dir: Path, rel_path: str, rev: str = "") -> dict:
        """Read and normalize a .md or .docx file (working tree or historical snapshot)."""
        clean = rel_path.replace("\\", "/").lstrip("/")
        if rev.strip():
            raw = read_snapshot_file(patent_dir, rev.strip(), clean)
        else:
            target = (patent_dir / clean).resolve()
            if patent_dir.resolve() not in target.parents or not target.is_file():
                raise FileNotFoundError(f"文档不存在: {clean}")
            raw = target.read_bytes()

        ext = Path(clean).suffix.lower()
        if ext == ".md":
            md_text = raw.decode("utf-8", errors="replace")
            return {
                "rel_path": clean,
                "rev": rev.strip(),
                "ext": "md",
                "raw_markdown": md_text,
                "blocks": parse_markdown_blocks(md_text),
                "comments": [],
                "track_changes": [],
                "has_redlines": False,
            }
        if ext == ".docx":
            parsed = parse_docx(raw)
            return {"rel_path": clean, "rev": rev.strip(), "ext": "docx", **parsed}
        raise ValueError(f"不支持预览的文件类型: {clean}")

    def diff_documents(
        self,
        patent_dir: Path,
        left_rel: str,
        left_rev: str,
        right_rel: str,
        right_rev: str = "",
    ) -> dict:
        left_doc = self.preview_document(patent_dir, left_rel, left_rev)
        right_doc = self.preview_document(patent_dir, right_rel, right_rev)
        diff = diff_document_blocks(left_doc["blocks"], right_doc["blocks"])
        return {
            "left": {"rel_path": left_rel, "rev": left_rev or "工作区"},
            "right": {"rel_path": right_rel, "rev": right_rev or "工作区"},
            **diff,
        }

    def check_reference_numerals(self, patent_dir: Path, numerals: list[dict] | None = None) -> dict:
        """Check whether declared reference numerals appear consistently in disclosure/submissions."""
        if numerals is None:
            numerals = self.load_meta(patent_dir).get("reference_numerals", [])

        corpus_parts: list[str] = []
        disc = patent_dir / "disclosure.md"
        if disc.is_file():
            corpus_parts.append(disc.read_text(encoding="utf-8", errors="replace"))
        sub_dir = patent_dir / "submissions"
        if sub_dir.is_dir():
            docx_files = sorted(
                (p for p in sub_dir.glob("*.docx") if not p.name.startswith("~$")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if docx_files:
                try:
                    corpus_parts.append(parse_docx(docx_files[0])["plain_text"])
                except ValueError:
                    pass
        corpus = "\n".join(corpus_parts)

        declared_codes: set[str] = set()
        results: list[dict] = []
        for entry in numerals or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "")).strip()
            name = str(entry.get("name", "")).strip()
            if not code:
                continue
            declared_codes.add(code)
            code_hits = len(re.findall(rf"(?<!\d){re.escape(code)}(?!\d)", corpus))
            name_hits = corpus.count(name) if name else 0
            paired = bool(
                name
                and re.search(rf"{re.escape(name)}\s*[（(]?\s*{re.escape(code)}\s*[）)]?", corpus)
            )
            if code_hits == 0:
                status = "missing"
            elif name and name_hits == 0:
                status = "name_mismatch"
            else:
                status = "ok"
            results.append(
                {
                    "code": code,
                    "name": name,
                    "code_hits": code_hits,
                    "name_hits": name_hits,
                    "paired": paired,
                    "status": status,
                }
            )

        # Detect undeclared numeric marks like 数据采集模块（103） or (201)
        undeclared_set: set[str] = set()
        for m in re.finditer(r"[\u4e00-\u9fff]{2,12}\s*[（(](\d{2,4})[）)]", corpus):
            c = m.group(1)
            if c not in declared_codes and not (1990 <= int(c) <= 2099):
                undeclared_set.add(c)

        return {
            "items": results,
            "undeclared": sorted(undeclared_set),
            "missing_count": sum(1 for r in results if r["status"] != "ok"),
        }

    def add_raw_feedback_batch(self, patent_dir: Path, source_label: str, raw_text: str) -> dict:
        clean = raw_text.strip()
        if not clean:
            raise ValueError("原始意见内容不能为空")
        doc = self.load_feedback(patent_dir)
        batch_id = f"B-{len(doc['raw_batches']) + 1}"
        doc["raw_batches"].insert(
            0,
            {
                "id": batch_id,
                "source_label": source_label.strip() or "代理师沟通记录",
                "raw_text": clean,
                "created_at": _utc_now_iso(),
            },
        )
        return self.save_feedback(patent_dir, doc)

    def _next_feedback_id(self, doc: dict, source: str) -> str:
        prefix = {"agent": "AG", "examiner": "OA", "internal": "IN"}.get(source, "AG")
        used = {item.get("id") for item in doc.get("items", [])}
        num = 1
        while f"{prefix}-{num}" in used:
            num += 1
        return f"{prefix}-{num}"

    def add_feedback_item(self, patent_dir: Path, payload: dict) -> dict:
        doc = self.load_feedback(patent_dir)
        source = payload.get("source", "agent")
        if source not in VALID_FEEDBACK_SOURCES:
            source = "agent"
        cat = payload.get("category", "clarify_needed")
        if cat not in VALID_FEEDBACK_CATEGORIES:
            cat = "clarify_needed"
        status = payload.get("status", "pending")
        if status not in VALID_FEEDBACK_STATUSES:
            status = "pending"

        fuzzy_note = str(payload.get("original_fuzzy_note", "")).strip()
        action_plan = str(payload.get("action_plan", "")).strip()
        if not (fuzzy_note or action_plan):
            raise ValueError("模糊原文或可执行修改点至少填写一项")

        item = {
            "id": str(payload.get("id") or self._next_feedback_id(doc, source)),
            "source": source,
            "category": cat,
            "original_fuzzy_note": fuzzy_note,
            "action_plan": action_plan or "待进一步拆解细化",
            "clarification_question": str(payload.get("clarification_question", "")).strip(),
            "target_section": str(payload.get("target_section", "")).strip(),
            "reply_draft": str(payload.get("reply_draft", "")).strip(),
            "status": status,
            "updated_at": _utc_now_iso(),
        }
        doc["items"].append(item)
        return self.save_feedback(patent_dir, doc)

    def update_feedback_item(self, patent_dir: Path, item_id: str, patch: dict) -> dict:
        doc = self.load_feedback(patent_dir)
        for item in doc["items"]:
            if item.get("id") == item_id:
                for field in (
                    "source",
                    "category",
                    "original_fuzzy_note",
                    "action_plan",
                    "clarification_question",
                    "target_section",
                    "reply_draft",
                    "status",
                ):
                    if field in patch and patch[field] is not None:
                        item[field] = str(patch[field]).strip()
                item["updated_at"] = _utc_now_iso()
                return self.save_feedback(patent_dir, doc)
        raise KeyError(f"Feedback item not found: {item_id}")

    def delete_feedback_item(self, patent_dir: Path, item_id: str) -> dict:
        doc = self.load_feedback(patent_dir)
        before = len(doc["items"])
        doc["items"] = [i for i in doc["items"] if i.get("id") != item_id]
        if len(doc["items"]) == before:
            raise KeyError(f"Feedback item not found: {item_id}")
        return self.save_feedback(patent_dir, doc)

    def import_docx_comments_to_feedback(self, patent_dir: Path, rel_docx: str) -> dict:
        """Extract comments from a Word .docx file and append them as feedback items."""
        parsed = self.preview_document(patent_dir, rel_docx)
        comments = parsed.get("comments", [])
        doc = self.load_feedback(patent_dir)
        existing_notes = {i.get("original_fuzzy_note", "") for i in doc["items"]}
        added = 0

        for c in comments:
            note = c.get("text", "").strip()
            if not note or note in existing_notes:
                continue
            anchor = c.get("anchor_text", "").strip()
            author = c.get("author", "代理师")
            src = "examiner" if any(k in author for k in ("审", "局", "OA")) else "agent"
            item = {
                "id": self._next_feedback_id(doc, src),
                "source": src,
                "category": "clarify_needed",
                "original_fuzzy_note": note,
                "action_plan": f"核对并补充该处技术细节（来源：{Path(rel_docx).name} · {author}）",
                "clarification_question": "若代理意见范围宽泛，请列出需确认的具体参数/步骤边界。",
                "target_section": f"锚定原文: {anchor[:60]}" if anchor else Path(rel_docx).name,
                "reply_draft": "",
                "status": "need_confirm" if ("?" in note or "？" in note or "建议" in note or "补充" in note) else "pending",
                "updated_at": _utc_now_iso(),
            }
            doc["items"].append(item)
            existing_notes.add(note)
            added += 1

        saved = self.save_feedback(patent_dir, doc)
        return {"added": added, "total_comments": len(comments), "feedback": saved}

    def export_reply_sheet_md(self, patent_dir: Path) -> str:
        """Render structured proxy clarification / OA response sheet in Markdown."""
        meta = self.load_meta(patent_dir)
        fb = self.load_feedback(patent_dir, meta["title"])
        title = meta.get("title") or patent_dir.name
        lines = [
            f"# {title} — 专利代理沟通确认单 / 修改回复草稿",
            "",
            f"- 申请号：{meta.get('application_no') or '待受理'}",
            f"- 代理机构：{meta.get('agency') or '待填写'}",
            "",
        ]
        confirm_items = [i for i in fb["items"] if i.get("clarification_question")]
        if confirm_items:
            lines.append("## 一、需与专利代理师重点确认的模糊事项")
            for item in confirm_items:
                lines.append(f"### [{item['id']}] {item.get('target_section') or '全文'}")
                lines.append(f"- **代理/审查原话**：{item.get('original_fuzzy_note', '')}")
                lines.append(f"- **待确认问题**：{item.get('clarification_question', '')}")
                lines.append("")

        lines.append("## 二、逐条修改落实与答复说明")
        for item in fb["items"]:
            lines.append(f"### [{item['id']}] ({item.get('status', 'pending')}) {item.get('target_section') or ''}")
            if item.get("original_fuzzy_note"):
                lines.append(f"- **原始意见**：{item['original_fuzzy_note']}")
            lines.append(f"- **落实措施**：{item.get('action_plan', '')}")
            if item.get("reply_draft"):
                lines.append(f"- **意见陈述/沟通回复**：{item['reply_draft']}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"


def _sample_diagram_png() -> bytes:
    """Create a crisp 360x180 PNG diagram without PIL for demo_patent."""
    w, h = 360, 180
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            in_box1 = 22 <= x <= 112 and 50 <= y <= 130
            in_box2 = 135 <= x <= 225 and 50 <= y <= 130
            in_box3 = 248 <= x <= 338 and 50 <= y <= 130
            in_arrow = (113 <= x <= 134 or 226 <= x <= 247) and 88 <= y <= 92
            if in_box1:
                row.extend((35, 31, 66))
            elif in_box2:
                row.extend((20, 184, 166))
            elif in_box3:
                row.extend((51, 65, 85))
            elif in_arrow:
                row.extend((249, 115, 22))
            else:
                row.extend((244, 247, 251))
        rows.append(bytes(row))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def ensure_demo_patent(mgr: PatentManager | None = None) -> Path:
    """Create a complete sample patent project demonstrating MD<->DOCX, redlines, figures, snapshots & fees."""
    pm = mgr or PatentManager()
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "RebuttalSidecar" / "demo_patent"
    if (base / META_FILENAME).is_file() and (base / "disclosure.md").is_file():
        return base

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    (base / "figures").mkdir(parents=True, exist_ok=True)
    (base / "submissions").mkdir(parents=True, exist_ok=True)
    (base / "figures" / "fig1_architecture.png").write_bytes(_sample_diagram_png())

    today = date.today()
    # Set filing_date ~3 years ago + 18 days so Year 4 annual fee is upcoming in 18 days.
    try:
        filing = date(today.year - 3, today.month, today.day) + timedelta(days=18)
    except ValueError:
        filing = date(today.year - 3, today.month, min(28, today.day)) + timedelta(days=18)
    grant = filing + timedelta(days=520)

    meta = {
        "title": "一种基于多跳知识图谱子图裁剪的智能问答方法及系统",
        "patent_type": "invention",
        "application_no": "CN202311048921.4",
        "inventors": "张明、李华",
        "agency": "北京科创知识产权代理事务所（王代理）",
        "stage": "granted",
        "filing_date": filing.isoformat(),
        "grant_date": grant.isoformat(),
        "fee_reduction": 70,
        "figures": [{"filename": "fig1_architecture.png", "caption": "图1 多跳子图裁剪推理系统架构图"}],
        "reference_numerals": [
            {"code": "101", "name": "实体链接模块"},
            {"code": "102", "name": "子图裁剪模块"},
            {"code": "103", "name": "证据排序模块"},
        ],
        "fee_payments": [{"year": 2, "paid_at": date(filing.year + 1, filing.month, filing.day).isoformat(), "amount": 270}],
        "custom_deadlines": [
            {
                "id": "DL-1",
                "title": "核对代理商二次补正权利要求范围确认",
                "due_date": (today + timedelta(days=9)).isoformat(),
                "done": False,
            }
        ],
    }

    md_v1 = (
        "# 一种基于多跳知识图谱子图裁剪的智能问答方法及系统\n\n"
        "## 一、技术领域\n"
        "本发明属于人工智能与自然语言处理技术领域，尤其涉及一种知识图谱多跳问答方法及系统。\n\n"
        "## 二、技术方案\n"
        "系统包括实体链接模块（101）、子图裁剪模块（102）和证据排序模块（103）。\n"
        "子图裁剪模块（102）对候选路径进行快速剪枝以减少提示词长度。\n\n"
        "## 三、附图说明\n"
        "![图1 多跳子图裁剪推理系统架构图](figures/fig1_architecture.png)\n"
    )
    (base / "disclosure.md").write_text(md_v1, encoding="utf-8")
    pm.save_meta(base, meta)
    export_markdown_to_docx(md_v1, base, base / "submissions" / "交底书_v1.docx", meta=meta)

    from patent_vcs import create_snapshot

    create_snapshot(base, "init: 技术交底书初稿与附图归档", tag="v1-交底初稿")

    md_v2 = (
        "# 一种基于多跳知识图谱子图裁剪的智能问答方法及系统\n\n"
        "## 一、技术领域\n"
        "本发明属于人工智能与自然语言处理技术领域，尤其涉及一种知识图谱多跳问答方法及系统。\n\n"
        "## 二、技术方案\n"
        "系统包括实体链接模块（101）、子图裁剪模块（102）和证据排序模块（103）。\n"
        "子图裁剪模块（102）采用自适应门控阈值（默认 0.55）对二跳候选路径实施静态背包约束裁剪，从而将提示词开销压缩至目标预算之内。\n\n"
        "## 三、附图说明\n"
        "![图1 多跳子图裁剪推理系统架构图](figures/fig1_architecture.png)\n"
    )
    (base / "disclosure.md").write_text(md_v2, encoding="utf-8")
    create_sample_redline_docx(
        base / "submissions" / "代理修改稿_v2.docx",
        paragraphs=[
            {"level": 1, "text": "一种基于多跳知识图谱子图裁剪的智能问答方法及系统"},
            {
                "comment_id": "1",
                "segments": [
                    {"kind": "text", "text": "子图裁剪模块（102）采用"},
                    {"kind": "del", "author": "王代理", "text": "快速剪枝"},
                    {"kind": "ins", "author": "王代理", "text": "自适应门控阈值（默认 0.55）对二跳候选路径实施静态背包约束裁剪"},
                    {"kind": "text", "text": "以控制推理开销。"},
                ],
            },
            {
                "comment_id": "2",
                "segments": [
                    {"kind": "text", "text": "证据排序模块（103）对候选子图按照一致性和覆盖度打分并输出排序结果。"}
                ],
            },
        ],
        comments=[
            {"id": "1", "author": "王代理", "text": "“快速剪枝”属于效果描述，建议补充具体的评分公式或门控阈值，方便写进从属权利要求。"},
            {"id": "2", "author": "王代理", "text": "这个“一致性”具体怎么算？能否给一组可选区间或实施例参数，否则审查员可能认为公开不充分。"},
        ],
    )
    fb = {
        "patent": meta["title"],
        "raw_batches": [
            {
                "id": "B-1",
                "source_label": "微信-王代理反馈 (11-26)",
                "raw_text": "老师好，我看了一下交底书，第二部分‘快速剪枝’写得太虚了，独权里不能光写快，得落到具体阈值或者约束规则上；另外附图里模块标号最好和文字再对一遍，还有一致性到底怎么算要给个说法。",
                "created_at": _utc_now_iso(),
            }
        ],
        "items": [
            {
                "id": "AG-1",
                "source": "agent",
                "category": "claim_scope",
                "original_fuzzy_note": "“快速剪枝”写得太虚了，独权里不能光写快，得落到具体阈值或约束规则上。",
                "action_plan": "在技术方案中明确写出二跳候选路径的静态背包预算约束及门控阈值（如 0.55）作为从属权利要求备选特征。",
                "clarification_question": "阈值 0.55 是否适合作为实施例写入说明书，独权仅保留“预设评分阈值”以防保护范围过窄？",
                "target_section": "二、技术方案 (子图裁剪模块 102)",
                "reply_draft": "已在 v2 版明确写出静态背包容量约束及门控评分公式；建议独权仅限定“基于预设门控阈值”，将 0.55 放入从属权及实施例。",
                "status": "need_confirm",
                "updated_at": _utc_now_iso(),
            },
            {
                "id": "AG-2",
                "source": "agent",
                "category": "tech_detail",
                "original_fuzzy_note": "一致性到底怎么算要给个说法，不然审查员可能认为公开不充分。",
                "action_plan": "补充一致性因子与门控得分的计算公式与可选区间（0.15~1.0）及至少一组具体数值实施例。",
                "clarification_question": "",
                "target_section": "二、技术方案 / 实施例",
                "reply_draft": "",
                "status": "pending",
                "updated_at": _utc_now_iso(),
            },
        ],
    }
    pm.save_feedback(base, fb)
    create_snapshot(base, "rev: 吸纳王代理初轮模糊意见并生成代理修改稿_v2.docx", tag="v2-代理沟通")
    return base
