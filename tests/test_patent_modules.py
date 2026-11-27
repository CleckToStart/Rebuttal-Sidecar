"""Tests for patent VCS, CNIPA fee timeline, reminder check, figures/numerals, and fuzzy feedback."""

from datetime import date, timedelta
from pathlib import Path

from config import AppConfig
from docx_engine import create_sample_redline_docx
from patent_manager import PatentManager, ensure_demo_patent
from patent_timeline import compute_patent_schedule, run_reminder_check
from patent_vcs import create_snapshot, list_snapshots, read_snapshot_file


def test_patent_snapshot_and_binary_history_read(tmp_path: Path) -> None:
    pm = PatentManager()
    pdir = tmp_path / "my_patent"
    payload = pm.init_or_scan_patent(str(pdir), title="测试专利", patent_type="invention")
    assert (pdir / "disclosure.md").is_file()
    assert (pdir / ".gitignore").is_file()
    assert payload["meta"]["title"] == "测试专利"

    s1 = create_snapshot(pdir, "v1 draft", tag="v1")
    assert s1["ref"]
    assert len(list_snapshots(pdir)) == 1

    # Update disclosure and save second snapshot, then read v1 via git show
    (pdir / "disclosure.md").write_text("# 测试专利\n第二版更新内容。\n", encoding="utf-8")
    create_snapshot(pdir, "v2 update")
    old_bytes = read_snapshot_file(pdir, "v1", "disclosure.md")
    assert "技术交底书" in old_bytes.decode("utf-8")


def test_cnipa_annual_fee_schedule_and_leap_year_reminders(tmp_path: Path) -> None:
    # Leap day filing date 2024-02-29 granted in year 2
    meta = {
        "title": "闰年测试专利",
        "patent_type": "invention",
        "stage": "granted",
        "filing_date": "2024-02-29",
        "grant_date": "2025-06-15",
        "fee_reduction": 85,
        "fee_payments": [{"year": 2, "paid_at": "2025-06-15", "amount": 135}],
        "custom_deadlines": [{"id": "D1", "title": "提交补正", "due_date": "2026-02-20", "done": False}],
    }
    sched = compute_patent_schedule(meta, today=date(2026, 2, 10), advance_days=30)
    assert sched["protection_end_date"] == "2044-02-29"
    assert sched["next_due_year"] == 3
    assert sched["next_due_date"] == "2026-02-28"
    assert sched["next_days_left"] == 18
    assert sched["fee_schedule"][1]["estimated_fee"] == 135  # 900 * 15% = 135
    assert len(sched["active_alerts"]) == 2  # Year 3 fee + D1 deadline

    # Register in AppConfig and run daily deduplicated reminder check
    pdir = tmp_path / "leap_patent"
    pm = PatentManager()
    pm.init_or_scan_patent(str(pdir), title="闰年测试专利")
    pm.save_meta(pdir, meta)

    cfg = AppConfig(tmp_path / "cfg.json")
    cfg.record_patent(str(pdir), name="闰年测试专利", patent_type="invention", stage="granted")
    res1 = run_reminder_check(cfg, force=False, today=date(2026, 2, 10))
    assert res1["checked"] == 2
    assert res1["notified"] == 2
    # Second check on the same day must deduplicate
    res2 = run_reminder_check(cfg, force=False, today=date(2026, 2, 10))
    assert res2["notified"] == 0


def test_numeral_consistency_and_docx_comment_import(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    pm = PatentManager()
    pdir = tmp_path / "pat"
    pm.init_or_scan_patent(str(pdir), title="标号与意见测试")
    (pdir / "disclosure.md").write_text(
        "# 方案\n系统包含数据采集模块（101）、计算核心（102）和输出接口（104）。\n",
        encoding="utf-8",
    )
    pm.save_meta(
        pdir,
        {
            "reference_numerals": [
                {"code": "101", "name": "数据采集模块"},
                {"code": "102", "name": "推理引擎"},
                {"code": "105", "name": "缓存单元"},
            ]
        },
    )
    chk = pm.check_reference_numerals(pdir)
    by_code = {i["code"]: i for i in chk["items"]}
    assert by_code["101"]["status"] == "ok"
    assert by_code["102"]["status"] == "name_mismatch"
    assert by_code["105"]["status"] == "missing"
    assert "104" in chk["undeclared"]

    create_sample_redline_docx(
        pdir / "submissions" / "agent.docx",
        paragraphs=[{"comment_id": "1", "segments": [{"kind": "text", "text": "权利要求1范围过宽"}]}],
        comments=[{"id": "1", "author": "李代理", "text": "建议缩小技术特征范围，不然新颖性过不了。"}],
    )
    imported = pm.import_docx_comments_to_feedback(pdir, "submissions/agent.docx")
    assert imported["added"] == 1
    assert imported["feedback"]["items"][0]["status"] == "need_confirm"

    demo_dir = ensure_demo_patent(pm)
    demo_payload = pm.get_patent_payload(demo_dir)
    assert len(demo_payload["snapshots"]) >= 2
    assert len(demo_payload["figures"]) == 1
    assert len(demo_payload["schedule"]["active_alerts"]) >= 1
