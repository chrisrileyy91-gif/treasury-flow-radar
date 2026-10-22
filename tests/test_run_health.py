from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from dashboard import export
from dashboard.renderer import render_report
from treasury_flow_radar import alert
from treasury_flow_radar.analytics.research import build_research_report
from treasury_flow_radar.run_health import fred_failures, fred_needs_inspection, load_ingest_report
from treasury_flow_radar.sources.fred import MAINTENANCE_MESSAGE

MAINTENANCE = f"FredResponseError: {MAINTENANCE_MESSAGE}"


def report(*fred_statuses: str, others: str = "SUCCESS") -> dict:
    """An ingest report shaped like `treasury_flow_radar.ingest` prints it."""
    fred = [{"source": f"fred-s{i}", "status": s, "message": MAINTENANCE if s == "FAILED" else "ok"}
            for i, s in enumerate(fred_statuses)]
    rest = [{"source": key, "status": others, "message": "ok"} for key in ("sec-edgar", "nyfed", "cftc")]
    return {"end_date": "2026-10-21", "status": "FAILED", "sources": fred + rest}


def test_only_failed_fred_sources_count():
    assert fred_failures(report("SUCCESS", "SUCCESS")) == []
    assert [s["source"] for s in fred_failures(report("FAILED", "SUCCESS"))] == ["fred-s0"]
    assert fred_failures(report("SUCCESS", others="FAILED")) == []


def test_missing_report_needs_inspection_but_a_cftc_only_backfill_does_not():
    assert fred_needs_inspection(None)
    assert not fred_needs_inspection({"sources": [{"source": "cftc", "status": "SUCCESS"}]})


def test_unreadable_report_loads_as_none(tmp_path: Path):
    empty = tmp_path / "ingest-report.json"
    empty.write_text("", encoding="utf-8")   # what `tee` leaves when ingest dies before printing
    assert load_ingest_report(empty) is None
    assert load_ingest_report(tmp_path / "missing.json") is None


def _page(run_health: dict | None) -> str:
    research = build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1))
    if run_health is not None:
        research["run_health"] = run_health
    return render_report(research)


def test_page_says_inspect_fred_only_when_fred_failed():
    assert "Inspect FRED." in _page({"fred_failed": True})
    assert "Inspect FRED." not in _page({"fred_failed": False})
    assert "Inspect FRED." not in _page(None)   # local exports without a run report


def test_export_reads_the_run_report(tmp_path: Path):
    ingest = tmp_path / "ingest-report.json"
    out = tmp_path / "site" / "index.html"
    ingest.write_text(json.dumps(report("FAILED", "FAILED")), encoding="utf-8")
    export.main(["--database", str(tmp_path / "db.sqlite3"), "--output", str(out), "--ingest-report", str(ingest)])
    assert "Inspect FRED." in out.read_text(encoding="utf-8")

    ingest.write_text(json.dumps(report("SUCCESS", "SUCCESS")), encoding="utf-8")
    export.main(["--database", str(tmp_path / "db.sqlite3"), "--output", str(out), "--ingest-report", str(ingest)])
    assert "Inspect FRED." not in out.read_text(encoding="utf-8")


def test_pheme_silent_when_fred_came_through():
    assert alert.build_message(report("SUCCESS", "SUCCESS", others="FAILED")) is None


def test_pheme_names_the_count_and_the_reason_once():
    message = alert.build_message(report("FAILED", "FAILED", "SUCCESS"))
    assert message is not None
    assert message.startswith("Treasury Flow Radar (2026-10-21): FRED failed for 2 of 3 FRED sources.")
    assert "Inspect FRED." in message
    assert f"- {MAINTENANCE} (x2)" in message
    assert message.endswith(alert.PAGE_URL)


def test_pheme_posts_when_there_is_no_report():
    assert "no readable ingest report" in (alert.build_message(None) or "")


def test_pheme_refuses_non_discord_webhook():
    with pytest.raises(ValueError):
        alert.post("https://discordapp.com/api/webhooks/x", "hi")


def test_pheme_without_webhook_prints_and_exits_clean(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_PHEME_WEBHOOK", raising=False)
    ingest = tmp_path / "ingest-report.json"
    ingest.write_text(json.dumps(report("FAILED")), encoding="utf-8")
    assert alert.main([str(ingest)]) == 0
    assert "Pheme silent: no webhook" in capsys.readouterr().out
