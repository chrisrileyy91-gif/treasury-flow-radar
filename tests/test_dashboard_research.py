"""Offline dashboard, evidence, provenance, freshness, and event-study coverage."""

from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs

import pytest

from dashboard.app import make_server, render_dashboard
from dashboard.research import (
    Event,
    build_event_window,
    classify_freshness,
    compare_events,
    load_observations,
)
from treasury_flow_radar.database.db import (
    database,
    initialize_database,
    insert_observation,
    insert_raw_record,
    register_series,
    register_source,
)


def _db(path, rows=()):
    initialize_database(path)
    with database(path) as conn:
        source_ids = {}
        series_ids = {}
        for source, series, freq, unit in rows:
            if source not in source_ids:
                source_ids[source] = register_source(conn, identifier=source, name=source.title(), source_type="test")
            key = (source, series)
            if key not in series_ids:
                series_ids[key] = register_series(conn, source_id=source_ids[source], identifier=series,
                                                   name=series, frequency=freq, default_unit=unit)
        return source_ids, series_ids


def _put(path, series_ids, source_ids, source, series, day, value, *, logical=None, revision=1,
         freq="daily", unit="Percent", metadata=None, publication=None):
    with database(path) as conn:
        raw_id = insert_raw_record(conn, source_id=source_ids[source], payload='{"fixture":true}',
                                   retrieval_time=datetime(2025, 1, 10, tzinfo=UTC), external_record_id=series+day)
        insert_observation(conn, source_id=source_ids[source], series_id=series_ids[(source, series)],
                           logical_key=logical or day, revision=revision,
                           revision_of_id=None if revision == 1 else 1,
                           observation_time=f"{day}T00:00:00Z", publication_time=publication,
                           retrieval_time=datetime(2025, 1, 10, tzinfo=UTC), value_numeric=value,
                           unit=unit, raw_record_id=raw_id, metadata=metadata)


def test_dashboard_data_loader_reads_latest_revision_and_retains_provenance(tmp_path):
    path = tmp_path / "radar.sqlite3"
    sources, series = _db(path, [("fred", "DGS10", "daily", "Percent")])
    _put(path, series, sources, "fred", "DGS10", "2025-01-02", 4.0, logical="d", revision=1)
    _put(path, series, sources, "fred", "DGS10", "2025-01-02", 4.1, logical="d", revision=2)
    rows = load_observations(path)
    assert len(rows) == 1
    assert rows[0]["value_numeric"] == 4.1
    assert rows[0]["raw_record_id"] is not None
    assert rows[0]["source_name"] == "Fred"


def test_missing_or_uninitialized_database_is_empty_and_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    assert load_observations(missing) == []
    assert not missing.exists()
    empty = tmp_path / "empty.db"
    empty.touch()
    assert load_observations(empty) == []


@pytest.mark.parametrize("freq,days,expected", [("daily", 4, "FRESH"), ("daily", 8, "STALE"),
                                                    ("weekly", 10, "FRESH"), ("weekly", 20, "STALE"),
                                                    ("monthly", 40, "FRESH"), ("quarterly", 1, "UNKNOWN")])
def test_freshness_classification(freq, days, expected):
    now = datetime(2025, 2, 1, tzinfo=UTC)
    retrieved = (now - timedelta(days=days)).isoformat()
    assert classify_freshness(retrieved, freq, now=now) == expected


@pytest.mark.parametrize("retrieved,freq", [(None, "daily"), ("bad", "daily"),
                                               ("2025-01-01", "daily"), ("2025-01-01T00:00:00Z", None)])
def test_freshness_missing_inputs_are_unknown(retrieved, freq):
    assert classify_freshness(retrieved, freq) == "UNKNOWN"


def _obs(series, day, value, source="fred", source_name="Federal Reserve", metadata=None):
    return {"series_identifier": series, "observation_time": day, "value_numeric": value,
            "source_identifier": source, "source_name": source_name, "revision": 1,
            "unit": "Percent", "default_unit": "Percent", "retrieval_time": "2025-01-10T00:00:00Z",
            "publication_time": None, "frequency": "daily", "raw_record_id": 1,
            "metadata": metadata or {}, "freshness": "FRESH", "series_name": series}


def test_evidence_labels_separate_fact_calculation_mechanism_hypothesis():
    items = evidence_summary([_obs("DGS10", "2025-01-02", 4.0), _obs("DGS10", "2025-01-03", 4.08)])
    assert [x["type"] for x in items] == ["FACT", "CALCULATION", "MECHANISM", "HYPOTHESIS"]
    assert "8" in items[1]["text"]
    assert "hypothesized" not in items[0]["text"].lower()


def test_system_read_is_deterministic_and_raw_observations_do_not_generate_causality():
    rows = [_obs("DGS10", "2025-01-02", 4.0), _obs("DGS10", "2025-01-03", 4.08)]
    first = evidence_summary(rows)
    second = evidence_summary(rows)
    assert first == second
    assert all("caused" not in item["text"].lower() for item in first)
    assert all("manipulation" not in item["text"].lower() for item in first)


def evidence_summary(rows):
    from dashboard.research import evidence_summary as summarize
    return summarize(rows)


def test_corporate_issuance_is_explicitly_unavailable_and_fixture_not_production():
    html = render_dashboard("missing-production.sqlite3")
    assert "Production corporate issuance feed unavailable." in html
    assert "Synthetic test fixtures are not loaded" in html


def test_cftc_spreading_stays_separate_in_dashboard(tmp_path):
    path = tmp_path / "cftc-dashboard-test.sqlite3"
    sources, series = _db(path, [("CFTC", "tff_10y", "weekly", "contracts")])
    for day, long, short, spread in (("2025-01-03", 100, 70, 600), ("2025-01-10", 120, 80, 700)):
        for metric, value in (("long", long), ("short", short), ("spreading", spread)):
            _put(path, series, sources, "CFTC", "tff_10y", day, value,
                 logical=f"{day}|leveraged_funds|{metric}", unit="contracts",
                 metadata={"participant_category": "leveraged_funds", "metric": metric})
    html = render_dashboard(path)
    assert "120.0 / 80.0 / 700.0 / 40.0" in html
    assert "spreading" in html
    assert "10.0" in html  # latest outright net change versus the preceding report


def test_event_window_has_fixed_offsets_and_actual_dates_only():
    rows = [_obs("DGS10", f"2025-01-{day:02d}", 4 + day / 100) for day in range(2, 10)]
    rows += [_obs("DGS2", f"2025-01-{day:02d}", 4.2) for day in range(2, 10)]
    event = Event("ev", date(2025, 1, 5), "test")
    window = build_event_window(event, rows)
    assert len(window) == 11
    assert [r["offset"] for r in window] == list(range(-5, 6))
    assert window[5]["date"] == "2025-01-05"
    assert window[5]["yield_10y"] == pytest.approx(4.05)
    assert window[4]["yield_10y_change_bps"] == 0
    assert window[6]["yield_10y_change_bps"] == pytest.approx(2)
    assert all(r["date"] is None or r["date"] in {x["observation_time"][:10] for x in rows} for r in window)


def test_missing_t0_and_short_history_remain_null_without_fabrication():
    rows = [_obs("DGS10", "2025-01-02", 4), _obs("DGS10", "2025-01-06", 4.1)]
    window = build_event_window(Event("ev", date(2025, 1, 3), "test"), rows)
    assert len(window) == 11
    assert window[5]["date"] == "2025-01-03"
    assert window[5]["yield_10y"] is None
    assert window[0]["date"] is None
    assert window[6]["date"] == "2025-01-06"


def test_weekly_cftc_only_attaches_on_exact_report_date():
    rows = [_obs("DGS10", f"2025-01-{day:02d}", 4.0) for day in (2, 3, 6)]
    rows.append(_obs("tff", "2025-01-03", 50, source="cftc_tff", source_name="CFTC",
                     metadata={"metric": "long"}))
    window = build_event_window(Event("ev", date(2025, 1, 3), "test"), rows)
    assert window[5]["cftc"]
    assert window[6]["cftc"] == []
    assert len([r for r in rows if r["source_identifier"] == "cftc_tff"]) == 1


def test_event_comparison_reports_insufficient_sample_and_descriptive_stats():
    window = [{"offset": -1, "yield_10y": 4.0}, {"offset": 1, "yield_10y": 4.1}]
    insufficient = compare_events([window], minimum_sample=2)
    assert insufficient["status"] == "INSUFFICIENT SAMPLE"
    assert insufficient["mean_bps"] is None
    windows = [[{"offset": -1, "yield_10y": 4.0}, {"offset": 1, "yield_10y": 4 + delta / 100}]
               for delta in (1, -1, 2, 0, -2)]
    result = compare_events(windows, minimum_sample=5)
    assert result["status"] == "DESCRIPTIVE SUMMARY"
    assert result["n"] == 5
    assert result["mean_bps"] == pytest.approx(0)
    assert result["median_bps"] == pytest.approx(0)
    assert result["pct_rising"] == pytest.approx(40)
    assert result["pct_post_event_decline"] == pytest.approx(40)


def test_event_interface_marks_user_supplied_event_unverified():
    html = render_dashboard("no-db", {"event_date": ["2025-01-03"], "event_type": ["Issuance"], "issuer": ["Example"]})
    assert "USER-SUPPLIED — UNVERIFIED" in html
    assert "No production event records are asserted." in html


def test_dashboard_startup_import_and_render_empty_state():
    # Loopback sockets are denied by this sandbox; exercise the production handler renderer.
    body = render_dashboard("missing-dashboard-db.sqlite3", parse_qs("event_date=2025-01-03"))
    assert "TREASURY FLOW RADAR" in body
    assert "UNKNOWN" in body
    assert "Event Study" in body
    assert callable(make_server)

