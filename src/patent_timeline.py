"""Patent lifecycle stages, CNIPA annual fee scheduler, Windows reminder task, and optional SMTP notifier."""

from __future__ import annotations

import calendar
import os
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from config import AppConfig
from proc_utils import run_cmd

PATENT_STAGES = (
    ("drafting", "交底撰写"),
    ("agency_review", "代理修改"),
    ("filed", "递交受理"),
    ("prelim_exam", "初审"),
    ("substantive_exam", "实审 / OA 答复"),
    ("granted", "授权维持"),
    ("expired", "届满 / 终止"),
)

PATENT_DURATIONS = {
    "invention": 20,
    "utility_model": 10,
    "design": 15,
}

# CNIPA statutory annual fee reference (RMB before fee reduction).
CNIPA_FEE_BRACKETS = {
    "invention": ((1, 3, 900), (4, 6, 1200), (7, 9, 2000), (10, 12, 4000), (13, 15, 6000), (16, 20, 8000)),
    "utility_model": ((1, 3, 600), (4, 5, 900), (6, 8, 1200), (9, 10, 2000)),
    "design": ((1, 3, 600), (4, 5, 900), (6, 8, 1200), (9, 10, 2000), (11, 15, 3000)),
}

WINDOWS_TASK_NAME = "RebuttalSidecar_PatentReminder"


def _parse_iso_date(raw: str) -> date | None:
    clean = str(raw or "").strip()[:10]
    if not clean:
        return None
    try:
        return date.fromisoformat(clean)
    except ValueError:
        return None


def _add_years(d: date, years: int) -> date:
    target_year = d.year + years
    last_day = calendar.monthrange(target_year, d.month)[1]
    return date(target_year, d.month, min(d.day, last_day))


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def reference_annual_fee(patent_type: str, year_num: int, fee_reduction: int = 0) -> int:
    """Return CNIPA reference annual fee (RMB) after applying fee reduction (0, 70, 85%)."""
    brackets = CNIPA_FEE_BRACKETS.get(patent_type, CNIPA_FEE_BRACKETS["invention"])
    base = brackets[-1][2]
    for start, end, amount in brackets:
        if start <= year_num <= end:
            base = amount
            break
    pct = max(0, min(85, int(fee_reduction)))
    return round(base * (100 - pct) / 100.0)


def _severity_for_days(days_left: int, grace_end: date, ref_today: date, advance_days: int) -> str:
    if days_left < 0:
        return "grace_period" if ref_today <= grace_end else "overdue"
    if days_left <= 7:
        return "urgent"
    if days_left <= advance_days:
        return "upcoming"
    return "normal"


def compute_patent_schedule(meta: dict, today: date | None = None, advance_days: int = 30) -> dict:
    """Calculate current patent year, protection end date, upcoming CNIPA fees, and active alerts."""
    ref_today = today or date.today()
    ptype = str(meta.get("patent_type") or "invention")
    max_years = PATENT_DURATIONS.get(ptype, 20)
    reduction = int(meta.get("fee_reduction") or 0)
    filing = _parse_iso_date(str(meta.get("filing_date", "")))
    grant = _parse_iso_date(str(meta.get("grant_date", "")))
    stage = str(meta.get("stage") or "drafting")

    paid_map: dict[int, dict] = {}
    for p in meta.get("fee_payments", []) or []:
        if isinstance(p, dict) and p.get("year") is not None:
            paid_map[int(p["year"])] = p

    current_year_index = 0
    protection_end = ""
    fee_rows: list[dict] = []
    alerts: list[dict] = []

    if filing is not None:
        protection_end = _add_years(filing, max_years).isoformat()
        for y in range(1, max_years + 1):
            yr_start = _add_years(filing, y - 1)
            yr_end = _add_years(filing, y)
            if yr_start <= ref_today < yr_end:
                current_year_index = y
                break
        if current_year_index == 0 and ref_today >= _add_years(filing, max_years):
            current_year_index = max_years

        if grant is not None or stage == "granted":
            start_yr = 1
            if grant is not None:
                for y in range(1, max_years + 1):
                    if _add_years(filing, y - 1) <= grant < _add_years(filing, y):
                        start_yr = y
                        break

            for y in range(start_yr, max_years + 1):
                due = grant if (grant is not None and y == start_yr) else _add_years(filing, y - 1)
                grace_end = _add_months(due, 6)
                days_left = (due - ref_today).days
                paid_info = paid_map.get(y)
                is_paid = paid_info is not None
                severity = "paid" if is_paid else _severity_for_days(days_left, grace_end, ref_today, advance_days)
                row = {
                    "year": y,
                    "due_date": due.isoformat(),
                    "grace_end_date": grace_end.isoformat(),
                    "days_left": days_left,
                    "estimated_fee": reference_annual_fee(ptype, y, reduction),
                    "paid": is_paid,
                    "paid_at": paid_info.get("paid_at", "") if paid_info else "",
                    "paid_amount": paid_info.get("amount", "") if paid_info else "",
                    "severity": severity,
                }
                # Include all paid years, any unpaid overdue/due year, and the next 3 upcoming unpaid years.
                unpaid_future = [r for r in fee_rows if not r["paid"] and r["days_left"] >= 0]
                if is_paid or days_left <= 0 or len(unpaid_future) < 3:
                    fee_rows.append(row)
                if not is_paid and severity in ("upcoming", "urgent", "grace_period", "overdue"):
                    alerts.append(
                        {
                            "key": f"fee_y{y}_{due.isoformat()}",
                            "kind": "annual_fee",
                            "title": f"第 {y} 年度专利年费（预估 ¥{row['estimated_fee']}）",
                            "due_date": row["due_date"],
                            "days_left": days_left,
                            "severity": severity,
                        }
                    )

    for dl in meta.get("custom_deadlines", []) or []:
        if not isinstance(dl, dict) or dl.get("done"):
            continue
        due = _parse_iso_date(str(dl.get("due_date", "")))
        if due is None:
            continue
        days_left = (due - ref_today).days
        if days_left <= advance_days:
            severity = "overdue" if days_left < 0 else ("urgent" if days_left <= 7 else "upcoming")
            alerts.append(
                {
                    "key": f"dl_{dl.get('id', due.isoformat())}_{due.isoformat()}",
                    "kind": "deadline",
                    "title": str(dl.get("title") or "专利截止事项"),
                    "due_date": due.isoformat(),
                    "days_left": days_left,
                    "severity": severity,
                }
            )

    alerts.sort(key=lambda a: a["days_left"])
    unpaid = [r for r in fee_rows if not r["paid"]]
    next_fee = unpaid[0] if unpaid else None
    return {
        "current_year_index": current_year_index,
        "max_years": max_years,
        "protection_end_date": protection_end,
        "next_due_year": next_fee["year"] if next_fee else None,
        "next_due_date": next_fee["due_date"] if next_fee else "",
        "next_days_left": next_fee["days_left"] if next_fee else None,
        "fee_schedule": fee_rows,
        "active_alerts": alerts,
    }


def collect_all_patent_alerts(config: AppConfig, today: date | None = None) -> list[dict]:
    """Aggregate due/overdue annual fees and custom deadlines across all registered patents."""
    import json

    advance = int(config.reminders.get("advance_days", 30))
    all_alerts: list[dict] = []
    for entry in config.patent_history:
        pdir = Path(entry["path"])
        meta_path = pdir / ".patent_meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sched = compute_patent_schedule(meta, today=today, advance_days=advance)
        for alert in sched["active_alerts"]:
            all_alerts.append(
                {
                    **alert,
                    "patent_path": str(pdir),
                    "patent_name": meta.get("title") or entry.get("name") or pdir.name,
                    "key": f"{pdir.name}:{alert['key']}",
                }
            )
    all_alerts.sort(key=lambda a: a["days_left"])
    return all_alerts


def _reminder_command_line() -> str:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return f'"{sys.executable}" --check-reminders'
    py_exe = Path(sys.executable)
    pyw_exe = py_exe.with_name("pythonw.exe")
    runner = pyw_exe if pyw_exe.is_file() else py_exe
    main_py = Path(__file__).resolve().parent / "main.py"
    return f'"{runner}" "{main_py}" --check-reminders'


def get_windows_reminder_task_status() -> bool:
    if os.name != "nt":
        return False
    res = run_cmd(["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME], timeout=10)
    return res.returncode == 0


def set_windows_reminder_task(enabled: bool, daily_time: str = "09:30") -> dict:
    """Create or delete a non-admin daily Windows Scheduled Task for patent deadline reminders."""
    if os.name != "nt":
        return {"enabled": False, "supported": False, "message": "仅支持 Windows 计划任务"}

    clean_time = daily_time.strip() or "09:30"
    if len(clean_time) != 5 or clean_time[2] != ":":
        clean_time = "09:30"

    if not enabled:
        run_cmd(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK_NAME], timeout=10)
        return {"enabled": False, "supported": True, "message": "已关闭 Windows 每日定时提醒"}

    tr = _reminder_command_line()
    res = run_cmd(
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", clean_time, "/TN", WINDOWS_TASK_NAME, "/TR", tr],
        timeout=15,
    )
    if res.returncode != 0:
        raise RuntimeError(f"创建 Windows 计划任务失败: {res.stderr.strip() or res.stdout.strip()}")
    return {"enabled": True, "supported": True, "daily_time": clean_time, "message": f"已注册每日 {clean_time} 专利缴费提醒任务"}


def send_windows_balloon(title: str, body: str) -> bool:
    """Display a lightweight native Windows system tray balloon notification without external deps."""
    if os.name != "nt":
        return False
    try:
        try:
            import clr
        except Exception:
            os.environ["PYTHONNET_RUNTIME"] = "coreclr"
            import clr

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        import time
        import System.Windows.Forms as WinForms
        from System.Drawing import SystemIcons

        notify = WinForms.NotifyIcon()
        notify.Icon = SystemIcons.Information
        notify.BalloonTipTitle = title[:60]
        notify.BalloonTipText = body[:240]
        notify.Visible = True
        notify.ShowBalloonTip(6000)
        WinForms.Application.DoEvents()
        time.sleep(0.3)
        notify.Visible = False
        notify.Dispose()
        return True
    except Exception:
        return False


def send_smtp_reminder(smtp_cfg: dict, subject: str, body: str) -> None:
    """Send optional SMTP reminder email when configured by user."""
    host = str(smtp_cfg.get("host", "")).strip()
    port = int(smtp_cfg.get("port") or 465)
    username = str(smtp_cfg.get("username", "")).strip()
    password = str(smtp_cfg.get("password", "")).strip()
    to_email = str(smtp_cfg.get("to_email", "")).strip()
    if not (host and username and password and to_email):
        raise ValueError("SMTP 服务器、账号、授权码或收件人不能为空")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_email
    msg.set_content(body)

    if smtp_cfg.get("ssl", True):
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)


def run_reminder_check(config: AppConfig, force: bool = False, today: date | None = None) -> dict:
    """Scan all registered patents and trigger daily-deduplicated desktop/email notifications."""
    ref_today = today or date.today()
    today_iso = ref_today.isoformat()
    alerts = collect_all_patent_alerts(config, today=ref_today)
    last_notified = dict(config.reminders.get("last_notified") or {})

    pending = alerts if force else [a for a in alerts if last_notified.get(a["key"]) != today_iso]
    if not pending and not force:
        return {"checked": len(alerts), "notified": 0, "balloon_sent": False, "email_sent": False}

    if pending:
        lines = [
            f"- {a['patent_name']}：{a['title']}（{'已逾期 ' + str(-a['days_left']) + ' 天' if a['days_left'] < 0 else '剩余 ' + str(a['days_left']) + ' 天'}）"
            for a in pending[:5]
        ]
        summary_body = "\n".join(lines)
        title = f"Rebuttal Sidecar · {len(pending)} 项专利提醒"
    else:
        summary_body = "当前所有已登记专利均无即将到期的年费或截止节点（测试通知）。"
        title = "Rebuttal Sidecar · 专利提醒服务测试"

    balloon_ok = send_windows_balloon(title, summary_body)
    email_ok = False
    smtp_cfg = config.reminders.get("smtp") or {}
    if smtp_cfg.get("enabled") and (pending or force):
        try:
            send_smtp_reminder(smtp_cfg, title, summary_body)
            email_ok = True
        except Exception:
            email_ok = False

    if pending:
        for a in pending:
            last_notified[a["key"]] = today_iso
        # Keep only today's / recent entries to avoid unbounded config growth.
        cutoff = (ref_today - timedelta(days=30)).isoformat()
        cleaned = {k: v for k, v in last_notified.items() if v >= cutoff}
        config.update_reminders({"last_notified": cleaned})

    return {
        "checked": len(alerts),
        "notified": len(pending),
        "balloon_sent": balloon_ok,
        "email_sent": email_ok,
        "alerts": pending,
    }
