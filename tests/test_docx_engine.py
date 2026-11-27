"""Tests for pure-Python OOXML .docx parser, redline/comment extraction, diff, and exporter."""

import struct
import zlib
from pathlib import Path

from docx_engine import (
    create_sample_redline_docx,
    diff_document_blocks,
    export_markdown_to_docx,
    parse_docx,
    parse_markdown_blocks,
)


def _minimal_png(width: int = 8, height: int = 6) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x40\x80\xc0" * width for _ in range(height))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_export_markdown_to_docx_and_parse_back(tmp_path: Path) -> None:
    figs = tmp_path / "figures"
    figs.mkdir()
    (figs / "arch.png").write_bytes(_minimal_png(120, 60))

    md = (
        "# 发明创造名称\n"
        "一种基于多跳知识图谱检索的智能问答系统（100）。\n\n"
        "## 附图说明\n"
        "![图1 系统架构图](figures/arch.png)\n\n"
        "| 标号 | 名称 |\n"
        "| --- | --- |\n"
        "| 101 | 数据采集模块 |\n"
    )
    out = tmp_path / "submissions" / "disclosure.docx"
    export_markdown_to_docx(md, tmp_path, out, meta={"title": "图谱问答专利", "patent_type": "invention"})
    assert out.is_file()

    parsed = parse_docx(out)
    assert "智能问答系统" in parsed["plain_text"]
    assert any(b["type"] == "heading" and b["level"] == 1 for b in parsed["blocks"])
    assert any(b["type"] == "table" for b in parsed["blocks"])
    assert len(parsed["media_files"]) == 1


def test_parse_docx_extracts_track_changes_and_anchored_comments(tmp_path: Path) -> None:
    docx_path = tmp_path / "agency_rev.docx"
    create_sample_redline_docx(
        docx_path,
        paragraphs=[
            {"level": 1, "text": "权利要求书"},
            {
                "comment_id": "1",
                "segments": [
                    {"kind": "text", "text": "一种方法，其特征在于，使用"},
                    {"kind": "del", "author": "李代理", "text": "快速"},
                    {"kind": "ins", "author": "李代理", "text": "基于阈值裁剪的"},
                    {"kind": "text", "text": "图检索算法。"},
                ],
            },
        ],
        comments=[
            {"id": "1", "author": "李代理", "text": "这里“快速”太模糊了，需写清具体约束阈值或步骤，不然审查员会驳回。"}
        ],
    )

    parsed = parse_docx(docx_path)
    assert parsed["has_redlines"] is True
    assert len(parsed["track_changes"]) == 2
    assert parsed["track_changes"][0]["kind"] == "del"
    assert parsed["track_changes"][1]["kind"] == "ins"
    assert len(parsed["comments"]) == 1
    assert "约束阈值" in parsed["comments"][0]["text"]
    assert "图检索算法" in parsed["comments"][0]["anchor_text"]


def test_cross_version_diff_between_markdown_and_docx(tmp_path: Path) -> None:
    md_v1 = "# 技术方案\n系统包括数据采集模块（101）和推理模块（102）。\n"
    docx_v2 = tmp_path / "v2.docx"
    create_sample_redline_docx(
        docx_v2,
        paragraphs=[
            {"level": 1, "text": "技术方案"},
            {"segments": [{"kind": "text", "text": "系统包括数据采集模块（101）、索引模块（103）和推理模块（102）。"}]},
            {"segments": [{"kind": "text", "text": "新增有益效果说明段落。"}]},
        ],
    )

    old_blocks = parse_markdown_blocks(md_v1)
    new_blocks = parse_docx(docx_v2)["blocks"]
    diff = diff_document_blocks(old_blocks, new_blocks)
    assert diff["summary"]["changed"] == 1
    assert diff["summary"]["added"] == 1
    assert any(b["op"] == "replace" and any(t["kind"] == "insert" for t in b["tokens"]) for b in diff["blocks"])
