from __future__ import annotations

import re
from datetime import date

import pytest

from dashboard.renderer import render_report
from dashboard.reporting import load_dashboard_report
from treasury_flow_radar.analytics.descriptive import EvidenceType
from treasury_flow_radar.analytics.event_study import event_study
from treasury_flow_radar.analytics.evidence import EvidenceRecord, evidence_from_rows
from treasury_flow_radar.analytics.issuance import analyze_issuance_event
from treasury_flow_radar.analytics.research import build_research_report
from treasury_flow_radar.analytics.temporal import align_observation
from treasury_flow_radar.sources.corporate_issuance import normalize_record


def row(series: str, day: str, value: float, **kwargs):
    return {
        "series_identifier": series,
        "observation_time": day,
        "value_numeric": value,
        "unit": "percent" if series.startswith("DGS") else "index",
        "source_identifier": kwargs.pop("source_identifier", "FRED"),
        "retrieval_time": "2025-10-01T14:00:00Z",
        "publication_time": None,
        **kwargs,
    }


def test_temporal_join_records_source_event_lag_and_relationship():
    weekly = [row("DEALER", "2025-09-26", 12), row("DEALER", "2025-10-03", 14)]
    match = align_observation(weekly, "2025-10-01", direction="prior")
    assert match is not None
    assert match.to_dict()["source_observation_date"] == "2025-09-26"
    assert match.to_dict()["event_date"] == "2025-10-01"
    assert match.lag_days == 5 and match.relationship == "BEFORE"
    assert align_observation(weekly, "2025-10-01", direction="following").relationship == "AFTER"
    assert align_observation(weekly, "2025-10-01", direction="prior", max_lag_days=2) is None


def test_event_study_uses_observed_yield_dates_and_shows_missing_market_data():
    dates = [f"2025-09-{day:02d}" for day in (22, 23, 24, 25, 26, 29, 30)]
    rows = [row("DGS10", d, 4.0 + i / 100) for i, d in enumerate(dates)]
    rows += [row("DGS2", d, 3.5 + i / 100) for i, d in enumerate(dates)]
    result = event_study("2025-09-26", rows)
    assert [x["source_observation_date"] for x in result["windows"]["T-1_T+1"]] == [
        "2025-09-25", "2025-09-26", "2025-09-29"
    ]
    assert result["windows"]["T-1_T+1"][0]["temporal_relationship"] == "BEFORE"
    assert result["market_confirmation"]["status"] == "Market confirmation unavailable"
    assert result["summary"]["DGS10"]["event_day_move_bps"] == pytest.approx(1.0)


def test_issuance_duration_and_post_settlement_alignment_are_labeled():
    yields = [row("DGS2", d, v) for d, v in (
        ("2025-06-09", 3.8), ("2025-06-10", 3.9), ("2025-06-17", 4.0), ("2025-06-20", 4.1)
    )]
    result = analyze_issuance_event({
        "source_record_id": "fixture-1",
        "fields": {"issuer_name": "Fixture Issuer", "principal_amount": 1000,
                   "currency": "USD", "benchmark_maturity": "2Y",
                   "announcement_date": "2025-06-09", "pricing_date": "2025-06-10",
                   "settlement_date": "2025-06-17", "maturity_date": "2030-06-10"},
    }, yields)
    assert result["days_pricing_to_settlement"] == 7
    assert result["duration_estimate_kind"] == "estimate"
    assert result["duration_pressure_unit"] == "USD-years"
    assert result["yield_changes"]["announcement_to_pricing"]["change_bps"] == pytest.approx(10)
    assert result["post_settlement_changes"]["immediately_after_settlement"]["match"]["source_observation_date"] == "2025-06-20"
    assert result["post_settlement_changes"]["1_calendar_days"]["match"]["source_observation_date"] == "2025-06-20"
    assert "do not establish" in result["interpretation"]


def test_corporate_model_normalizes_announcement_and_credit_classification():
    normalized = normalize_record({
        "source_record_id": "fixture-deal", "issuer_name": "Fixture Issuer",
        "announcement_date": "2025-06-01", "pricing_date": "2025-06-02",
        "settlement_date": "2025-06-09", "maturity_date": "2030-06-02",
        "principal_amount": "1000000", "currency": "usd", "credit_classification": "IG",
        "benchmark_yield": "4.2", "credit_spread": "90", "publication_time": "2025-06-01T12:00:00+00:00",
        "source_native_fields": {"provider_ref": "fixture"},
    })
    assert normalized.fields["announcement_date"] == date(2025, 6, 1)
    assert normalized.fields["credit_classification"] == "investment_grade"
    assert normalized.fields["benchmark_yield"] == pytest.approx(4.2)
    assert normalized.fields["credit_spread"] == pytest.approx(90)


def test_market_returns_and_event_reversal_calculations_when_observed():
    rows = []
    for index, day in enumerate(("2025-09-24", "2025-09-25", "2025-09-26", "2025-09-29",
                                 "2025-09-30", "2025-10-01", "2025-10-02", "2025-10-03")):
        rows.extend([row("DGS10", day, (4.00, 4.01, 4.06, 4.05, 4.04, 4.03, 4.02, 4.01)[index]),
                     row("HYG", day, 100 - index, source_identifier="fixture-market")])
    result = event_study("2025-09-26", rows)
    assert result["market_confirmation"]["status"] == "AVAILABLE"
    assert result["market_confirmation"]["causality_established"] is False
    assert result["summary"]["DGS10"]["post_event_reversal_magnitude_bps"] == pytest.approx(5)
    assert result["summary"]["DGS10"]["post_event_reversal_percent"] == pytest.approx(100)


def test_evidence_taxonomy_requires_a_basis_for_confidence():
    facts = evidence_from_rows(EvidenceType.FACT, "Yield was reported.", [row("DGS10", "2025-09-30", 4.0)])
    assert facts.source == "FRED"
    assert facts.observation_dates == ("2025-09-30",)
    assert facts.retrieval_dates == ("2025-10-01T14:00:00Z",)
    with pytest.raises(ValueError, match="statistical basis"):
        EvidenceRecord(EvidenceType.INFERENCE, "An inference.", confidence=0.8)


def test_report_explicitly_marks_unavailable_sources_and_retains_alignment():
    rows = [row("DGS10", "2025-09-30", 4.0), row("DGS10", "2025-10-01", 4.1),
            row("NYFED_DEALER", "2025-09-26", 10, source_identifier="NYFED")]
    report = build_research_report(rows, start_date=date(2025, 9, 1), end_date=date(2025, 10, 2))
    assert report["corporate_issuance"]["status"] == "Corporate issuance event feed not configured"
    assert report["market_confirmation"]["status"] == "Market confirmation unavailable"
    context = report["events"][0]["dealer_context"]["NYFED_DEALER"]
    assert context["lag_days"] == 5
    assert context["temporal_relationship"] == "BEFORE"
    assert report["events"][0]["event_study"]["event_date"] == "2025-10-01"


def test_empty_database_report_is_read_only_and_export_is_offline(tmp_path):
    from treasury_flow_radar.database import initialize_database

    db = tmp_path / "snapshot.sqlite3"
    initialize_database(db)
    before = db.read_bytes()
    report = load_dashboard_report(db)
    html = render_report(report)
    assert db.read_bytes() == before
    assert "Generated at:" in html
    assert "Corporate issuance event feed not configured" in html
    assert "Market confirmation unavailable" in html
    assert "<style>" in html and "<script>" in html
    assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=['\"]https?://", html, re.IGNORECASE)
    assert "<form" not in html
    assert "Static snapshot" in html
    assert "FRED_API_KEY" not in html and "TREASURY_FLOW_RADAR_DB" not in html
    assert len(html.encode("utf-8")) < 1_000_000


def test_compact_dashboard_overview_and_audit_details_are_rendered():
    rows = [
        row("DGS10", "2025-09-25", 4.00), row("DGS2", "2025-09-25", 3.50),
        row("DGS10", "2025-09-26", 4.10), row("DGS2", "2025-09-26", 3.55),
        row("DGS10", "2025-09-29", 4.12), row("DGS2", "2025-09-29", 3.56),
        row("DGS10", "2025-09-30", 4.14), row("DGS2", "2025-09-30", 3.58),
        row("DGS10", "2025-10-01", 4.16), row("DGS2", "2025-10-01", 3.60),
        row("DGS10", "2025-10-02", 4.18), row("DGS2", "2025-10-02", 3.62),
    ]
    report = build_research_report(rows, start_date=date(2025, 9, 1), end_date=date(2025, 10, 2), threshold_bps=1)
    html = render_report(report)
    for heading in ("What is happening?", "Dealer positioning", "Futures positioning", "Treasury supply",
                    "What evidence is missing?", "Large 10-year moves", "Sources and freshness"):
        assert f">{heading}</h" in html
    # The deterministic curve sentence: both maturities rose over 5 sessions (2y +12, 10y +18).
    assert "yields rose at every maturity shown" in html
    assert "The largest move was the <span class=\"nw\">10-year</span> (+18 bp)" in html
    assert "Production corporate issuance feed unavailable." in html
    assert "HYG: <strong>Unavailable</strong>" in html
    assert html.count('<details class="event">') == len(report["events"]) > 1
    assert "id=research-data" not in html


def test_compact_cftc_contract_summary_keeps_participant_details_collapsed():
    rows = [
        row("DGS10", "2025-10-01", 4.0), row("DGS10", "2025-10-02", 4.1),
        row("UST_10", "2025-09-30", 120, source_identifier="CFTC", source_type="cftc", series_name="10Y Treasury", logical_key="dealer|long", unit="contracts", metadata={"participant_category": "dealer", "metric": "long"}),
        row("UST_10", "2025-09-30", 80, source_identifier="CFTC", source_type="cftc", series_name="10Y Treasury", logical_key="dealer|short", unit="contracts", metadata={"participant_category": "dealer", "metric": "short"}),
    ]
    report = build_research_report(rows, start_date=date(2025, 9, 1), end_date=date(2025, 10, 2), threshold_bps=1)
    html = render_report(report)
    futures = _section(html, "Futures positioning")
    assert '<span class="cell-main">+40</span>' in futures  # dealer net = 120 - 80
    assert "Long, short, and spreading for every trader group" in futures
    assert "<th scope=\"col\" class=\"r\">Spreading</th>" in futures


def _section(html: str, heading: str) -> str:
    """Return the HTML of one <section>, located by its heading text."""
    start = html.index(f">{heading}</h")
    return html[start:html.index("</section>", start)]


def _list_after(html: str, h3: str) -> list[str]:
    start = html.index(f"<h3>{h3}</h3>")
    block = html[start:html.index("</ul>", start)]
    return re.findall(r"<li>(.*?)</li>", block)


def test_empty_report_lists_no_treasury_evidence_as_available():
    html = render_report(build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1)))
    missing = _section(html, "What evidence is missing?")
    stored = _list_after(missing, "Treasury-market data not stored")
    for label in ("10Y yield movement", "10Y–2Y curve movement", "Primary dealer positioning",
                  "CFTC positioning", "Treasury auction data"):
        assert f"{label}: <strong>UNAVAILABLE — NO OBSERVATIONS IN DATABASE</strong>" in stored
    assert _list_after(missing, "Have") == ["None"]
    assert "currently holds the Treasury-market side" not in missing
    assert "even the Treasury side of the hypothesis is incomplete" in missing
    assert "Not enough yield observations to describe the curve." in html


def test_partial_report_lists_only_the_evidence_that_exists():
    rows = [row("DGS10", "2025-10-01", 4.0), row("DGS2", "2025-10-01", 3.5),
            row("DGS10", "2025-10-02", 4.1), row("DGS2", "2025-10-02", 3.6)]
    html = render_report(build_research_report(rows, start_date=date(2025, 10, 1), end_date=date(2025, 10, 2)))
    stored = " ".join(_list_after(_section(html, "What evidence is missing?"), "Treasury-market data not stored"))
    assert "Primary dealer positioning: <strong>UNAVAILABLE — NO OBSERVATIONS IN DATABASE" in stored
    assert "Treasury auction data" in stored
    assert "10Y yield movement" not in stored and "10Y–2Y curve movement" not in stored
    assert "HYG: <strong>Unavailable</strong>" in html


def test_status_text_is_rendered_as_markup_not_escaped_text():
    html = render_report(build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1)))
    assert "&lt;span" not in html and "&lt;strong" not in html
    assert '<span class="unknown">' in html


def test_live_and_static_modes_describe_themselves_accurately():
    report = build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1))
    live = render_report(report, allow_event_input=True)
    static = render_report(report)
    assert "Live view of the local database" in live and "Static snapshot" not in live
    assert "self-contained snapshot" not in live
    assert "Static snapshot" in static and "self-contained snapshot" in static


def test_event_study_and_issuance_cover_5y_7y_30y_curve_points():
    days = ["2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06"]
    rows = [row(s, d, base + i / 100) for i, d in enumerate(days)
            for s, base in (("DGS10", 4.0), ("DGS5", 3.8), ("DGS7", 3.9), ("DGS30", 4.6))]
    study = event_study("2025-10-03", rows)
    t0 = next(r for r in study["windows"]["T-5_T+5"] if r["offset"] == 0)
    assert t0["dgs30_percent"] == pytest.approx(4.62)
    assert t0["dgs30_change_bps"] == pytest.approx(1)
    assert study["summary"]["DGS5"]["event_day_move_bps"] == pytest.approx(1)
    result = analyze_issuance_event(
        {"fields": {"pricing_date": "2025-10-02", "settlement_date": "2025-10-03",
                    "benchmark_maturity": "30Y"}}, rows)
    assert result["treasury_yield_series"] == "DGS30"
    unknown = analyze_issuance_event(
        {"fields": {"pricing_date": "2025-10-02", "benchmark_maturity": "3Y"}}, rows)
    assert unknown["treasury_yield_series"] is None
