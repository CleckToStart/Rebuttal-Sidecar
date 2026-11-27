"""Pure-Python OOXML (.docx) parser, redline/comment extractor, cross-version diff, and Markdown exporter."""

from __future__ import annotations

import difflib
import io
import os
import re
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_.\-]+|\s+|.")
MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MD_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.+)$")
MD_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")
HEADING_STYLE_RE = re.compile(r"(?:heading|标题)\s*([1-4])", re.IGNORECASE)


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _a(tag: str) -> str:
    return f"{{{A_NS}}}{tag}"


def _r(tag: str) -> str:
    return f"{{{R_NS}}}{tag}"


def _read_zip_xml(zf: zipfile.ZipFile, member: str) -> ET.Element | None:
    try:
        raw = zf.read(member)
    except KeyError:
        return None
    return ET.fromstring(raw)


def _parse_rels(zf: zipfile.ZipFile) -> dict[str, str]:
    root = _read_zip_xml(zf, "word/_rels/document.xml.rels")
    if root is None:
        return {}
    mapping: dict[str, str] = {}
    for rel in root.findall(f"{{{REL_NS}}}Relationship"):
        rid = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rid and target:
            mapping[rid] = target
    return mapping


def _parse_comments_xml(zf: zipfile.ZipFile) -> dict[str, dict]:
    root = _read_zip_xml(zf, "word/comments.xml")
    if root is None:
        return {}
    comments: dict[str, dict] = {}
    for elem in root.findall(_w("comment")):
        cid = elem.attrib.get(_w("id"), "")
        if not cid:
            continue
        texts = [t.text for t in elem.iter(_w("t")) if t.text]
        comments[cid] = {
            "id": cid,
            "author": elem.attrib.get(_w("author"), "Unknown"),
            "date": elem.attrib.get(_w("date"), ""),
            "text": "".join(texts).strip(),
            "anchor_text": "",
        }
    return comments


def _append_segment(segments: list[dict], kind: str, text: str, author: str = "", date: str = "") -> None:
    if not text:
        return
    if segments and segments[-1]["kind"] == kind and segments[-1].get("author", "") == author:
        segments[-1]["text"] += text
        return
    seg: dict = {"kind": kind, "text": text}
    if author:
        seg["author"] = author
    if date:
        seg["date"] = date
    segments.append(seg)


def _walk_paragraph_element(
    node: ET.Element,
    segments: list[dict],
    plain_parts: list[str],
    track_changes: list[dict],
    active_comments: set[str],
    comment_anchors: dict[str, list[str]],
    block_index: int,
    rels: dict[str, str],
    images: list[str],
    revision_mode: str = "text",
    rev_author: str = "",
    rev_date: str = "",
) -> None:
    tag = node.tag
    if tag == _w("commentRangeStart"):
        cid = node.attrib.get(_w("id"), "")
        if cid:
            active_comments.add(cid)
        return
    if tag == _w("commentRangeEnd"):
        cid = node.attrib.get(_w("id"), "")
        active_comments.discard(cid)
        return
    if tag == _w("ins"):
        author = node.attrib.get(_w("author"), "")
        date = node.attrib.get(_w("date"), "")
        buf: list[str] = []
        for child in node:
            _walk_paragraph_element(
                child,
                segments,
                buf,
                track_changes,
                active_comments,
                comment_anchors,
                block_index,
                rels,
                images,
                "ins",
                author,
                date,
            )
        joined = "".join(buf)
        if joined:
            plain_parts.append(joined)
            track_changes.append(
                {"kind": "ins", "author": author, "date": date, "text": joined, "block_index": block_index}
            )
        return
    if tag == _w("del"):
        author = node.attrib.get(_w("author"), "")
        date = node.attrib.get(_w("date"), "")
        buf = []
        for child in node:
            _walk_paragraph_element(
                child,
                segments,
                buf,
                track_changes,
                active_comments,
                comment_anchors,
                block_index,
                rels,
                images,
                "del",
                author,
                date,
            )
        joined = "".join(buf)
        if joined:
            track_changes.append(
                {"kind": "del", "author": author, "date": date, "text": joined, "block_index": block_index}
            )
        return
    if tag in (_w("t"), _w("delText")):
        txt = node.text or ""
        if txt:
            _append_segment(segments, revision_mode, txt, rev_author, rev_date)
            plain_parts.append(txt)
            if revision_mode != "del":
                for cid in active_comments:
                    comment_anchors.setdefault(cid, []).append(txt)
        return
    if tag == _w("tab"):
        _append_segment(segments, revision_mode, "\t", rev_author, rev_date)
        plain_parts.append("\t")
        return
    if tag == _w("br"):
        _append_segment(segments, revision_mode, "\n", rev_author, rev_date)
        plain_parts.append("\n")
        return
    if tag == _a("blip"):
        embed = node.attrib.get(_r("embed"), "")
        target = rels.get(embed, "")
        if target:
            images.append(target)
        return

    for child in node:
        _walk_paragraph_element(
            child,
            segments,
            plain_parts,
            track_changes,
            active_comments,
            comment_anchors,
            block_index,
            rels,
            images,
            revision_mode,
            rev_author,
            rev_date,
        )


def _heading_level_from_p(p_elem: ET.Element) -> int:
    ppr = p_elem.find(_w("pPr"))
    if ppr is None:
        return 0
    pstyle = ppr.find(_w("pStyle"))
    if pstyle is None:
        return 0
    val = pstyle.attrib.get(_w("val"), "")
    m = HEADING_STYLE_RE.search(val)
    if m:
        return int(m.group(1))
    if val in ("1", "2", "3", "4"):
        return int(val)
    return 0


def parse_docx(source: Path | bytes) -> dict:
    """Parse an OOXML .docx file or bytes into blocks, Word comments, and track changes."""
    if isinstance(source, Path):
        if not source.is_file():
            raise FileNotFoundError(f"DOCX file not found: {source}")
        raw_bytes = source.read_bytes()
    else:
        raw_bytes = source

    try:
        zf_ctx = zipfile.ZipFile(io.BytesIO(raw_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"无效的 .docx 文件格式: {exc}") from exc

    with zf_ctx as zf:
        doc_root = _read_zip_xml(zf, "word/document.xml")
        if doc_root is None:
            raise ValueError(".docx 缺少 word/document.xml")
        rels = _parse_rels(zf)
        comments_map = _parse_comments_xml(zf)
        media_files = [n for n in zf.namelist() if n.startswith("word/media/")]

    body = doc_root.find(_w("body"))
    if body is None:
        raise ValueError(".docx 缺少正文 w:body")

    blocks: list[dict] = []
    track_changes: list[dict] = []
    active_comments: set[str] = set()
    comment_anchors: dict[str, list[str]] = {}

    for child in body:
        if child.tag == _w("p"):
            level = _heading_level_from_p(child)
            segments: list[dict] = []
            plain_parts: list[str] = []
            images: list[str] = []
            _walk_paragraph_element(
                child,
                segments,
                plain_parts,
                track_changes,
                active_comments,
                comment_anchors,
                len(blocks),
                rels,
                images,
            )
            plain = "".join(plain_parts).strip()
            has_rev = any(seg["kind"] in ("ins", "del") for seg in segments)
            if not plain and not has_rev and not images:
                continue
            btype = "heading" if level > 0 else ("image" if images and not plain else "paragraph")
            block: dict = {
                "type": btype,
                "level": level,
                "text": plain,
                "segments": segments if segments else [{"kind": "text", "text": plain}],
            }
            if images:
                block["images"] = images
            blocks.append(block)
        elif child.tag == _w("tbl"):
            rows: list[list[str]] = []
            for tr in child.findall(_w("tr")):
                row_cells: list[str] = []
                for tc in tr.findall(_w("tc")):
                    cell_texts = ["".join(t.text or "" for t in p.iter(_w("t"))).strip() for p in tc.findall(_w("p"))]
                    row_cells.append(" ".join(x for x in cell_texts if x))
                if any(row_cells):
                    rows.append(row_cells)
            if rows:
                flat_rows = " | ".join(" ; ".join(r) for r in rows)
                blocks.append(
                    {
                        "type": "table",
                        "level": 0,
                        "rows": rows,
                        "text": flat_rows,
                        "segments": [{"kind": "text", "text": flat_rows}],
                    }
                )

    comments: list[dict] = []
    for cid, cobj in comments_map.items():
        anchor = "".join(comment_anchors.get(cid, [])).strip()
        comments.append({**cobj, "anchor_text": anchor})

    plain_text = "\n\n".join(b["text"] for b in blocks if b.get("text"))
    return {
        "blocks": blocks,
        "comments": comments,
        "track_changes": track_changes,
        "media_files": media_files,
        "plain_text": plain_text,
        "has_redlines": bool(track_changes or comments),
    }


def parse_markdown_blocks(md_text: str) -> list[dict]:
    """Parse Markdown disclosure text into normalized document blocks."""
    lines = md_text.splitlines()
    blocks: list[dict] = []
    i = 0
    in_code = False

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code or not stripped or stripped.startswith("<!--"):
            i += 1
            continue

        hm = MD_HEADING_RE.match(stripped)
        if hm:
            text = hm.group(2).strip()
            blocks.append(
                {"type": "heading", "level": len(hm.group(1)), "text": text, "segments": [{"kind": "text", "text": text}]}
            )
            i += 1
            continue

        im = MD_IMAGE_RE.fullmatch(stripped)
        if im:
            alt, rel_path = im.group(1).strip(), im.group(2).strip()
            caption = alt or Path(rel_path).name
            blocks.append(
                {
                    "type": "image",
                    "level": 0,
                    "alt": caption,
                    "path": rel_path,
                    "text": f"[附图: {caption}]",
                    "segments": [{"kind": "text", "text": f"[附图: {caption}]"}],
                }
            )
            i += 1
            continue

        if "|" in stripped and i + 1 < len(lines) and MD_TABLE_SEP_RE.match(lines[i + 1].strip()):
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i]:
                cur = lines[i].strip()
                if MD_TABLE_SEP_RE.match(cur):
                    i += 1
                    continue
                cells = [c.strip() for c in cur.strip("|").split("|")]
                rows.append(cells)
                i += 1
            flat = " | ".join(" ; ".join(r) for r in rows)
            blocks.append(
                {"type": "table", "level": 0, "rows": rows, "text": flat, "segments": [{"kind": "text", "text": flat}]}
            )
            continue

        lm = MD_LIST_RE.match(line)
        if lm:
            text = lm.group(1).strip()
            blocks.append({"type": "list", "level": 0, "text": text, "segments": [{"kind": "text", "text": text}]})
            i += 1
            continue

        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("```")
                or MD_HEADING_RE.match(nxt)
                or MD_IMAGE_RE.fullmatch(nxt)
                or MD_LIST_RE.match(lines[i])
                or ("|" in nxt and i + 1 < len(lines) and MD_TABLE_SEP_RE.match(lines[i + 1].strip()))
            ):
                break
            para_lines.append(nxt)
            i += 1
        para = " ".join(para_lines)
        blocks.append({"type": "paragraph", "level": 0, "text": para, "segments": [{"kind": "text", "text": para}]})

    return blocks


def _inline_token_diff(old_text: str, new_text: str) -> list[dict]:
    old_toks = [m.group(0) for m in CJK_TOKEN_RE.finditer(old_text)]
    new_toks = [m.group(0) for m in CJK_TOKEN_RE.finditer(new_text)]
    matcher = difflib.SequenceMatcher(None, old_toks, new_toks, autojunk=False)
    tokens: list[dict] = []

    def push(kind: str, txt: str) -> None:
        if not txt:
            return
        if tokens and tokens[-1]["kind"] == kind:
            tokens[-1]["text"] += txt
        else:
            tokens.append({"kind": kind, "text": txt})

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            push("equal", "".join(new_toks[j1:j2]))
        elif tag == "delete":
            push("delete", "".join(old_toks[i1:i2]))
        elif tag == "insert":
            push("insert", "".join(new_toks[j1:j2]))
        elif tag == "replace":
            push("delete", "".join(old_toks[i1:i2]))
            push("insert", "".join(new_toks[j1:j2]))
    return tokens


def diff_document_blocks(old_blocks: list[dict], new_blocks: list[dict]) -> dict:
    """Compute block-aligned and inline token diffs between two parsed documents."""
    old_strs = [b.get("text", "") for b in old_blocks]
    new_strs = [b.get("text", "") for b in new_blocks]
    matcher = difflib.SequenceMatcher(None, old_strs, new_strs, autojunk=False)

    out_blocks: list[dict] = []
    summary = {"added": 0, "deleted": 0, "changed": 0, "equal": 0}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for blk in new_blocks[j1:j2]:
                summary["equal"] += 1
                out_blocks.append(
                    {
                        "op": "equal",
                        "type": blk.get("type", "paragraph"),
                        "level": blk.get("level", 0),
                        "old_text": blk.get("text", ""),
                        "new_text": blk.get("text", ""),
                        "tokens": [{"kind": "equal", "text": blk.get("text", "")}],
                    }
                )
        elif tag == "delete":
            for blk in old_blocks[i1:i2]:
                summary["deleted"] += 1
                txt = blk.get("text", "")
                out_blocks.append(
                    {
                        "op": "delete",
                        "type": blk.get("type", "paragraph"),
                        "level": blk.get("level", 0),
                        "old_text": txt,
                        "new_text": "",
                        "tokens": [{"kind": "delete", "text": txt}],
                    }
                )
        elif tag == "insert":
            for blk in new_blocks[j1:j2]:
                summary["added"] += 1
                txt = blk.get("text", "")
                out_blocks.append(
                    {
                        "op": "insert",
                        "type": blk.get("type", "paragraph"),
                        "level": blk.get("level", 0),
                        "old_text": "",
                        "new_text": txt,
                        "tokens": [{"kind": "insert", "text": txt}],
                    }
                )
        elif tag == "replace":
            pair_count = min(i2 - i1, j2 - j1)
            for k in range(pair_count):
                ob = old_blocks[i1 + k]
                nb = new_blocks[j1 + k]
                summary["changed"] += 1
                out_blocks.append(
                    {
                        "op": "replace",
                        "type": nb.get("type", "paragraph"),
                        "level": nb.get("level", 0),
                        "old_text": ob.get("text", ""),
                        "new_text": nb.get("text", ""),
                        "tokens": _inline_token_diff(ob.get("text", ""), nb.get("text", "")),
                    }
                )
            for ob in old_blocks[i1 + pair_count : i2]:
                summary["deleted"] += 1
                txt = ob.get("text", "")
                out_blocks.append(
                    {
                        "op": "delete",
                        "type": ob.get("type", "paragraph"),
                        "level": ob.get("level", 0),
                        "old_text": txt,
                        "new_text": "",
                        "tokens": [{"kind": "delete", "text": txt}],
                    }
                )
            for nb in new_blocks[j1 + pair_count : j2]:
                summary["added"] += 1
                txt = nb.get("text", "")
                out_blocks.append(
                    {
                        "op": "insert",
                        "type": nb.get("type", "paragraph"),
                        "level": nb.get("level", 0),
                        "old_text": "",
                        "new_text": txt,
                        "tokens": [{"kind": "insert", "text": txt}],
                    }
                )

    return {"summary": summary, "blocks": out_blocks}


def _image_pixel_size(img_bytes: bytes, suffix: str) -> tuple[int, int]:
    """Read pixel dimensions from PNG/JPEG headers; fall back to 4:3 defaults."""
    if suffix == ".png" and len(img_bytes) >= 24 and img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", img_bytes[16:24])
        if w > 0 and h > 0:
            return w, h
    if suffix in (".jpg", ".jpeg") and len(img_bytes) > 4 and img_bytes[:2] == b"\xff\xd8":
        idx = 2
        while idx + 9 < len(img_bytes):
            if img_bytes[idx] != 0xFF:
                idx += 1
                continue
            marker = img_bytes[idx + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", img_bytes[idx + 5 : idx + 9])
                if w > 0 and h > 0:
                    return w, h
                break
            if marker == 0xD9 or marker == 0xDA:
                break
            if idx + 4 > len(img_bytes):
                break
            seg_len = struct.unpack(">H", img_bytes[idx + 2 : idx + 4])[0]
            if seg_len < 2:
                break
            idx += 2 + seg_len
    return 1200, 800


def _inline_runs_xml(text: str) -> str:
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        clean = part[2:-2] if bold else part
        rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(clean)}</w:t></w:r>')
    return "".join(out) or '<w:r><w:t xml:space="preserve"></w:t></w:r>'


def _table_xml(rows: list[list[str]]) -> str:
    borders = (
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        '</w:tblBorders>'
    )
    rows_xml: list[str] = []
    for r_idx, row in enumerate(rows):
        cells_xml: list[str] = []
        for cell in row:
            shading = '<w:shd w:val="clear" w:color="auto" w:fill="F1F5F9"/>' if r_idx == 0 else ""
            text_xml = _inline_runs_xml(f"**{cell}**" if r_idx == 0 and "**" not in cell else cell)
            cells_xml.append(
                f"<w:tc><w:tcPr>{shading}<w:tcMar><w:top w:w=\"90\" w:type=\"dxa\"/><w:bottom w:w=\"90\" w:type=\"dxa\"/><w:left w:w=\"120\" w:type=\"dxa\"/><w:right w:w=\"120\" w:type=\"dxa\"/></w:tcMar></w:tcPr><w:p>{text_xml}</w:p></w:tc>"
            )
        rows_xml.append(f"<w:tr>{''.join(cells_xml)}</w:tr>")
    return f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{borders}</w:tblPr>{"".join(rows_xml)}</w:tbl>'


def _image_paragraph_xml(rid: str, docpr_id: int, img_name: str, cx_emu: int, cy_emu: int, caption: str) -> str:
    safe_name = escape(img_name)
    drawing = (
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx_emu}" cy="{cy_emu}"/>'
        f'<wp:docPr id="{docpr_id}" name="{safe_name}"/>'
        f'<a:graphic xmlns:a="{A_NS}">'
        f'<a:graphicData uri="{PIC_NS}">'
        f'<pic:pic xmlns:pic="{PIC_NS}">'
        f'<pic:nvPicPr><pic:cNvPr id="{docpr_id}" name="{safe_name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx_emu}" cy="{cy_emu}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    )
    cap_p = f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>{_inline_runs_xml(caption)}</w:p>'
    return drawing + cap_p


_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:eastAsia="宋体" w:hAnsi="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/>'
    '</w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>'
    '<w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:spacing w:before="200" w:after="100"/></w:pPr>'
    '<w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:spacing w:before="140" w:after="80"/></w:pPr>'
    '<w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="24"/></w:rPr></w:style>'
    '</w:styles>'
)

_ROOT_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>'
)


def _write_docx_package(
    output_path: Path,
    body_xml: str,
    media_entries: list[tuple[str, bytes]],
    rel_xml_extra: str = "",
    comments_xml: str | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]
    if comments_xml is not None:
        rels.append(
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
        )
    if rel_xml_extra:
        rels.append(rel_xml_extra)

    doc_rels_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">{"".join(rels)}</Relationships>'
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}" xmlns:wp="{WP_NS}">'
        f'<w:body>{body_xml}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        '</w:body></w:document>'
    )
    overrides = [
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>',
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>',
    ]
    if comments_xml is not None:
        overrides.append(
            '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
        )
    ct_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="jpg" ContentType="image/jpeg"/>'
        '<Default Extension="jpeg" ContentType="image/jpeg"/>'
        f'{"".join(overrides)}</Types>'
    )

    tmp_path = output_path.with_suffix(".docx.tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct_xml.encode("utf-8"))
        zf.writestr("_rels/.rels", _ROOT_RELS_XML.encode("utf-8"))
        zf.writestr("word/document.xml", doc_xml.encode("utf-8"))
        zf.writestr("word/styles.xml", _STYLES_XML.encode("utf-8"))
        zf.writestr("word/_rels/document.xml.rels", doc_rels_xml.encode("utf-8"))
        if comments_xml is not None:
            zf.writestr("word/comments.xml", comments_xml.encode("utf-8"))
        for media_name, data in media_entries:
            zf.writestr(f"word/media/{media_name}", data)
    os.replace(tmp_path, output_path)
    return output_path


def export_markdown_to_docx(
    md_text: str,
    patent_dir: Path,
    output_path: Path,
    meta: dict | None = None,
) -> Path:
    """Convert disclosure Markdown + local figures into a formatted Chinese patent .docx file."""
    blocks = parse_markdown_blocks(md_text)
    body_parts: list[str] = []

    if meta and meta.get("title"):
        type_labels = {"invention": "发明专利", "utility_model": "实用新型", "design": "外观设计"}
        meta_rows = [
            ["专利名称", str(meta.get("title", "")), "专利类型", type_labels.get(meta.get("patent_type", ""), "发明专利")],
            ["发明人", str(meta.get("inventors") or "待补充"), "代理机构", str(meta.get("agency") or "待对接")],
            ["申请号", str(meta.get("application_no") or "未递交"), "交底/申请日", str(meta.get("filing_date") or "未设定")],
        ]
        body_parts.append(f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>{_inline_runs_xml("技术交底书")}</w:p>')
        body_parts.append(_table_xml(meta_rows))

    media_entries: list[tuple[str, bytes]] = []
    rel_extra: list[str] = []
    rid_counter = 3
    docpr_id = 1
    max_width_emu = 5_300_000  # ~14.7 cm inside A4 margins

    for blk in blocks:
        btype = blk["type"]
        if btype == "heading":
            lvl = min(3, max(1, int(blk.get("level", 1))))
            body_parts.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{lvl}"/></w:pPr>{_inline_runs_xml(blk["text"])}</w:p>')
        elif btype == "table":
            body_parts.append(_table_xml(blk["rows"]))
        elif btype == "list":
            body_parts.append(f'<w:p><w:pPr><w:ind w:left="420"/></w:pPr>{_inline_runs_xml("• " + blk["text"])}</w:p>')
        elif btype == "image":
            rel_img = Path(blk.get("path", ""))
            img_path = (patent_dir / rel_img).resolve()
            if not img_path.is_file():
                img_path = (patent_dir / "figures" / rel_img.name).resolve()
            suffix = img_path.suffix.lower() if img_path.is_file() else ""
            if img_path.is_file() and suffix in (".png", ".jpg", ".jpeg"):
                raw_img = img_path.read_bytes()
                px_w, px_h = _image_pixel_size(raw_img, suffix)
                cx = min(max_width_emu, px_w * 9525)
                cy = max(400_000, int(cx * (px_h / max(1, px_w))))
                media_name = f"image{len(media_entries) + 1}{suffix}"
                rid = f"rId{rid_counter}"
                rid_counter += 1
                media_entries.append((media_name, raw_img))
                rel_extra.append(
                    f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{media_name}"/>'
                )
                body_parts.append(_image_paragraph_xml(rid, docpr_id, media_name, cx, cy, blk.get("alt", img_path.name)))
                docpr_id += 1
            else:
                body_parts.append(f"<w:p>{_inline_runs_xml(blk['text'])}</w:p>")
        else:
            body_parts.append(f'<w:p><w:pPr><w:spacing w:line="320" w:lineRule="auto"/></w:pPr>{_inline_runs_xml(blk["text"])}</w:p>')

    return _write_docx_package(output_path, "".join(body_parts), media_entries, rel_xml_extra="".join(rel_extra))


def create_sample_redline_docx(
    output_path: Path,
    paragraphs: list[dict],
    comments: list[dict] | None = None,
) -> Path:
    """Create a .docx with optional Word track changes (ins/del) and comments for demos and tests."""
    body_parts: list[str] = []
    for p in paragraphs:
        lvl = int(p.get("level", 0))
        ppr = f'<w:pPr><w:pStyle w:val="Heading{lvl}"/></w:pPr>' if lvl else ""
        runs: list[str] = []
        cid = p.get("comment_id")
        if cid:
            runs.append(f'<w:commentRangeStart w:id="{escape(str(cid))}"/>')
        for seg in p.get("segments", [{"kind": "text", "text": p.get("text", "")}]):
            kind = seg.get("kind", "text")
            txt = escape(seg.get("text", ""))
            author = escape(seg.get("author", "专利代理师"))
            date = escape(seg.get("date", "2026-11-27T10:00:00Z"))
            if kind == "ins":
                runs.append(f'<w:ins w:id="1" w:author="{author}" w:date="{date}"><w:r><w:t xml:space="preserve">{txt}</w:t></w:r></w:ins>')
            elif kind == "del":
                runs.append(f'<w:del w:id="2" w:author="{author}" w:date="{date}"><w:r><w:delText xml:space="preserve">{txt}</w:delText></w:r></w:del>')
            else:
                runs.append(f'<w:r><w:t xml:space="preserve">{txt}</w:t></w:r>')
        if cid:
            runs.append(f'<w:commentRangeEnd w:id="{escape(str(cid))}"/><w:r><w:commentReference w:id="{escape(str(cid))}"/></w:r>')
        body_parts.append(f"<w:p>{ppr}{''.join(runs)}</w:p>")

    comments_xml = None
    if comments:
        c_parts = []
        for c in comments:
            cid = escape(str(c["id"]))
            author = escape(c.get("author", "专利代理师"))
            date = escape(c.get("date", "2026-11-27T10:00:00Z"))
            ctext = escape(c.get("text", ""))
            c_parts.append(f'<w:comment w:id="{cid}" w:author="{author}" w:date="{date}"><w:p><w:r><w:t>{ctext}</w:t></w:r></w:p></w:comment>')
        comments_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:comments xmlns:w="{W_NS}">{"".join(c_parts)}</w:comments>'

    return _write_docx_package(output_path, "".join(body_parts), [], comments_xml=comments_xml)
