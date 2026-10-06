"""Offline test data only; none of these rows represent historical market observations."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from treasury_flow_radar.analytics import (
    EvidenceType,
    Observation,
    curve_metrics,
    large_yield_moves,
    yield_metrics,
)
from treasury_flow_radar.analytics.report import generate_report, load_observations_read_only
from treasury_flow_radar.analytics.research import build_research_report
from treasury_flow_radar.database.db import (
    initialize_database,
    insert_observation,
    register_series,
    register_source,
)


def _obs(series: str, day: str, value: float | None, unit: str = "Percent", **kw):
    return Observation(series, day, value, unit, logical_key=kw.pop("logical_key", day), **kw)


def test_yields_include_five_and_ten_observation_changes_without_calendar_fill():
    rows = [_obs("DGS10", d, value) for d, value in [
        ("2025-01-02", 4.00), ("2025-01-03", 4.01), ("2025-01-06", 4.02),
        ("2025-01-07", 4.03), ("2025-01-08", 4.04), ("2025-01-09", 4.05),
        ("2025-01-10", 4.06), ("2025-01-13", 4.07), ("2025-01-14", 4.08),
        ("2025-01-15", 4.09), ("2025-01-16", 4.10),
    ]]
    result = yield_metrics(rows, "DGS10")
    assert result[5].change_5_observations_bps == pytest.approx(5)
    assert result[10].change_10_observations_bps == pytest.approx(10)
    assert len(result) == len(rows)
    assert result[-1].observation_time == date(2025, 1, 16)


def test_spread_exposes_percentage_points_and_basis_points_only_on_shared_dates():
    rows = [_obs("DGS2", "2025-01-02", 4.0), _obs("DGS10", "2025-01-02", 4.2),
            _obs("DGS2", "2025-01-03", 4.1)]
    result = curve_metrics(rows)
    assert len(result) == 1
    assert result[0].spread_10y_minus_2y_percentage_points == pytest.approx(0.2)
    assert result[0].spread_10y_minus_2y_bps == pytest.approx(20)


def test_large_move_detector_threshold_direction_and_missing_values():
    rows = [_obs("DGS10", "2025-01-02", 4.0), _obs("DGS10", "2025-01-03", 4.05),
            _obs("DGS10", "2025-01-06", None), _obs("DGS10", "2025-01-07", 4.10)]
    events = large_yield_moves(rows)
    assert len(events) == 2
    assert events[0]["direction"] == "UP"
    assert events[0]["change_bps"] == pytest.approx(5)
    assert events[0]["skipped_no_value_dates"] == []
    assert events[0]["evidence_type"] == EvidenceType.OBSERVATION
    # The session after a no-value date is compared with the last valued session.
    after_gap = events[1]
    assert after_gap["event_date"] == "2025-01-07"
    assert after_gap["prior_observation_date"] == "2025-01-03"
    assert after_gap["change_bps"] == pytest.approx(5)
    assert after_gap["skipped_no_value_dates"] == ["2025-01-06"]
    assert after_gap["calendar_days_since_prior"] == 4
    with pytest.raises(ValueError, match="finite and nonnegative"):
        large_yield_moves(rows, threshold_bps=float("nan"))


def _row(source, series, day, value, *, logical="", metadata=None, unit="Percent", freq=None):
    return {
        "source_identifier": source, "source_url": f"https://example.invalid/{source}",
        "series_identifier": series, "series_name": series, "series_frequency": freq,
        "logical_key": logical or day, "revision": 1, "observation_time": day,
        "publication_time": None, "retrieval_time": "2025-02-01T12:00:00Z",
        "value_numeric": value, "unit": unit, "metadata": metadata or {},
        "observation_id": 1, "raw_record_id": 1,
    }


def _context_rows():
    rows = [
        _row("FRED", "DGS10", "2025-01-01", 4.00),
        _row("FRED", "DGS10", "2025-01-02", 4.06),
        _row("FRED", "DGS10", "2025-01-03", 4.05),
        _row("FRED", "DGS2", "2025-01-01", 4.0),
        _row("FRED", "DGS2", "2025-01-02", 4.01),
        _row("FRED", "DGS2", "2025-01-03", 4.02),
    ]
    for day, value in [("2024-12-20", 900), ("2024-12-27", 1000), ("2025-01-03", 1100)]:
        rows.append(_row("NYFED", "dealer_net_position_nominal_treasury_ex_tips_window_key",
                         day, value, unit="million_us_dollars", freq="weekly"))
    cftc_values = {
        "long": ("dealer_intermediary", 120),
        "short": ("dealer_intermediary", 70),
        "spreading": ("dealer_intermediary", 25),
    }
    for metric, (participant, value) in cftc_values.items():
        rows.append(_row("CFTC", "tff_futures_only_ust_10_year_note_043602", "2024-12-31",
                         value, logical=f"2024-12-31|{participant}|{metric}", unit="contracts",
                         freq="weekly", metadata={"metric": metric,
                         "participant_category": participant, "reporting_frequency": "weekly"}))
    auction_base = {"dates": {"auction_date": "2025-01-02"}, "security_type": "Note",
                    "security_term": "10-Year", "cusip": "TEST12345",
                    "source_identifier": "TEST12345|2025-01-02|2025-01-15"}
    for field, value in [("offering_amt", 42000), ("total_accepted", 41000),
                         ("bid_to_cover_ratio", 2.5), ("high_yield", 4.3)]:
        rows.append(_row("U.S. Treasury Fiscal Data", "treasury_auction_note_10_year",
                         "2025-01-02", value, unit=("ratio" if field == "bid_to_cover_ratio"
                         else "percent" if field == "high_yield" else "us_dollars"),
                         logical=f"auction|{field}", metadata={**auction_base, "source_field": field}))
    return rows


def test_event_context_respects_weekly_dates_cftc_lag_and_auction_window():
    report = build_research_report(_context_rows(), start_date=date(2025, 1, 1),
                                   end_date=date(2025, 1, 3))
    assert len(report["events"]) == 1
    event = report["events"][0]
    assert event["event"]["event_date"] == "2025-01-02"
    assert event["event"]["dgs2_change_bps"] == pytest.approx(1)
    dealer = next(iter(event["dealer_context"].values()))
    assert dealer["observation_date"] == "2024-12-27"
    assert dealer["prior_observation_date"] == "2024-12-20"
    assert dealer["change_from_prior_observation"] == pytest.approx(100)
    cftc = event["cftc_context"]["contracts"][0]
    assert cftc["positioning_date"] == "2024-12-31"
    assert cftc["days_before_event"] == 2
    participant = cftc["participant_categories"][0]
    assert participant["net_position"] == 50
    assert participant["spreading"] == 25
    auction = event["auction_context"]["auctions"][0]
    assert auction["auction_date"] == "2025-01-02"
    assert auction["offering_amount"] == 42000
    assert auction["accepted_amount"] == 41000
    assert auction["bid_to_cover"] == 2.5
    assert auction["yield_or_rate"] == 4.3
    assert auction["offering_amount_unit"] == "us_dollars"
    assert auction["yield_or_rate_field"] == "high_yield"
    assert event["event"]["evidence_type"] == EvidenceType.OBSERVATION
    assert report["method"]["weekly_series"].startswith("aligned to latest")


def test_empty_or_missing_context_does_not_create_weekly_or_auction_values():
    rows = [_row("FRED", "DGS10", "2025-01-01", 4.0),
            _row("FRED", "DGS10", "2025-01-02", 4.06)]
    report = build_research_report(rows, start_date=date(2025, 1, 1), end_date=date(2025, 1, 2))
    event = report["events"][0]
    assert event["dealer_context"] == {}
    assert event["cftc_context"]["contracts"] == []
    assert event["auction_context"]["auctions"] == []
    assert report["source_provenance"]


def test_read_only_sqlite_report_does_not_change_source_observations(tmp_path):
    path = tmp_path / "research.sqlite3"
    initialize_database(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fred = register_source(conn, identifier="FRED", name="FRED", source_type="test", url="https://fred")
        series = register_series(conn, source_id=fred, identifier="DGS10", name="DGS10",
                                 frequency="daily", default_unit="Percent")
        for day, value in [("2025-01-01", 4), ("2025-01-02", 4.06)]:
            insert_observation(conn, source_id=fred, series_id=series, logical_key=day,
                               observation_time=f"{day}T00:00:00Z", retrieval_time="2025-01-03T00:00:00Z",
                               value_numeric=value, unit="Percent", metadata={"observation_precision": "calendar_date"})
    before = path.read_bytes()
    loaded = load_observations_read_only(path)
    assert len(loaded) == 2
    report = generate_report(path, start_date=date(2025, 1, 1), end_date=date(2025, 1, 2))
    assert len(report["events"]) == 1
    assert path.read_bytes() == before


def test_json_encoding_accepts_report_values_and_evidence_labels():
    report = build_research_report(_context_rows(), start_date=date(2025, 1, 1),
                                   end_date=date(2025, 1, 3))
    encoded = json.dumps(report, allow_nan=False)
    assert '"CALCULATION"' in encoded
    assert '"FACT"' in encoded
    assert '"OBSERVATION"' in encoded



def test_event_study_offsets_skip_no_value_dates():
    from treasury_flow_radar.analytics.event_study import event_study
    days = ["2025-06-30", "2025-07-01", "2025-07-02", "2025-07-03", "2025-07-04",
            "2025-07-07", "2025-07-08"]
    rows = [_obs("DGS10", d, None if d == "2025-07-04" else 4.0 + i / 100)
            for i, d in enumerate(days)]
    window = {r["offset"]: r for r in event_study("2025-07-07", rows)["windows"]["T-5_T+5"]}
    assert window[-1]["source_observation_date"] == "2025-07-03"
    assert window[-4]["source_observation_date"] == "2025-06-30"
    assert window[1]["source_observation_date"] == "2025-07-08"
    assert window[0]["dgs10_change_bps"] == pytest.approx(2)
