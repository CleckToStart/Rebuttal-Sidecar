"""End-to-end HTTP/SSE integration tests for the Patent Management workbench endpoints."""

import json
import urllib.request
from pathlib import Path

from config import AppConfig
from server import SidecarApp


def test_patent_workbench_full_http_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    cfg = AppConfig(tmp_path / "cfg.json")
    app = SidecarApp(config=cfg)
    port = app.start(port=0)
    base = f"http://127.0.0.1:{port}"

    try:
        # 1. Switch top-level workbench and verify persistence
        req_wb = urllib.request.Request(
            f"{base}/api/workbench",
            data=json.dumps({"workbench": "patent"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        wb_res = json.loads(urllib.request.urlopen(req_wb, timeout=3).read().decode("utf-8"))
        assert wb_res["active_workbench"] == "patent"

        # 2. Load built-in demo patent
        req_demo = urllib.request.Request(f"{base}/api/patents/demo", data=b"{}", method="POST")
        state = json.loads(urllib.request.urlopen(req_demo, timeout=10).read().decode("utf-8"))
        assert state["meta"]["patent_type"] == "invention"
        assert len(state["snapshots"]) >= 2
        assert len(state["figures"]) == 1
        assert len(state["alerts"]) >= 1

        # 3. Preview agency .docx with Word track changes & comments
        doc_res = json.loads(
            urllib.request.urlopen(f"{base}/api/patents/document?rel=submissions/%E4%BB%A3%E7%90%86%E4%BF%AE%E6%94%B9%E7%A8%BF_v2.docx", timeout=5)
            .read()
            .decode("utf-8")
        )
        assert doc_res["has_redlines"] is True
        assert len(doc_res["comments"]) >= 2

        # 4. Cross-version diff between snapshot v1 and current disclosure.md
        diff_res = json.loads(
            urllib.request.urlopen(
                f"{base}/api/patents/diff?left_rel=disclosure.md&left_rev=v1-%E4%BA%A4%E5%BA%95%E5%88%9D%E7%A8%BF&right_rel=disclosure.md",
                timeout=5,
            )
            .read()
            .decode("utf-8")
        )
        assert diff_res["summary"]["changed"] >= 1

        # 5. Export disclosure.md -> submissions/test_export.docx
        req_exp = urllib.request.Request(
            f"{base}/api/patents/export-docx",
            data=json.dumps({"filename": "test_export.docx", "snapshot": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        exp_res = json.loads(urllib.request.urlopen(req_exp, timeout=10).read().decode("utf-8"))
        assert exp_res["exported_rel"] == "submissions/test_export.docx"

        # 6. Import Word comments to fuzzy feedback + add raw batch + export reply sheet
        req_raw = urllib.request.Request(
            f"{base}/api/patents/feedback/raw",
            data=json.dumps({"source_label": "审查员电话", "raw_text": "附图102标号需再核实"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        raw_res = json.loads(urllib.request.urlopen(req_raw, timeout=3).read().decode("utf-8"))
        assert len(raw_res["feedback"]["raw_batches"]) >= 2

        reply = json.loads(
            urllib.request.urlopen(f"{base}/api/patents/feedback/export?format=md", timeout=3).read().decode("utf-8")
        )
        assert "专利代理沟通确认单" in reply["content"]

        # 7. Figure image streaming & numeral consistency check
        with urllib.request.urlopen(f"{base}/api/patents/figures/fig1_architecture.png", timeout=3) as img_resp:
            assert img_resp.status == 200
            assert img_resp.read(4) == b"\x89PNG"

        chk = json.loads(urllib.request.urlopen(f"{base}/api/patents/numerals/check", timeout=3).read().decode("utf-8"))
        assert len(chk["items"]) == 3
    finally:
        app.stop()
