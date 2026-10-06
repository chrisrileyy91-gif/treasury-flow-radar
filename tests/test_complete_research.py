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


