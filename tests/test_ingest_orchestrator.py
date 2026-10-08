"""Offline tests for the controlled adapter runner."""

import json
from datetime import UTC, date, datetime
from typing import ClassVar

from dashboard.research import load_observations
from treasury_flow_radar.database import (
    database,
    initialize_database,
    insert_observation,
    insert_raw_record,
    register_series,
    register_source,
)
from treasury_flow_radar.ingest import run_ingestion
from treasury_flow_radar.sources.nyfed import NyfedClient, ingest_nyfed


def _nyfed_payload():
    return json.dumps({"pd": {"timeseries": [{
        "keyid": "PDPOSGST-TOT", "seriesbreak": "SBN2024",
        "asofdate": "2026-10-07", "value": "123.5",
    }]}})


class _Response:
    status = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self.payload = payload.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def _client():
    return NyfedClient(
        opener=lambda *_args, **_kwargs: _Response(_nyfed_payload()),
        clock=lambda: datetime(2026, 10, 8, tzinfo=UTC),
    )


def test_fred_is_skipped_without_key_and_does_not_read_or_print_secrets(tmp_path):
    summary = run_ingestion(
        database_path=tmp_path / "empty.sqlite3",
        sources=["fred-dgs2", "fred-dgs10"],
        environment={"FRED_API_KEY": ""},
        adapter_functions={
            "fred-dgs2": lambda: (_ for _ in ()).throw(AssertionError("must not call FRED")),
            "fred-dgs10": lambda: (_ for _ in ()).throw(AssertionError("must not call FRED")),
        },
    )
    assert [result.status for result in summary.results] == ["SKIPPED", "SKIPPED"]
    assert summary.as_dict()["status"] == "COMPLETE_WITH_SKIPS"
    assert all("FRED_API_KEY" in result.message for result in summary.results)
    assert not (tmp_path / "empty.sqlite3").exists()


def test_runner_reingestion_is_idempotent_and_reports_counts(tmp_path):
    path = tmp_path / "runner.sqlite3"

    def nyfed():
        return ingest_nyfed(
            database_path=path, client=_client(),
            observation_start=date(2026, 10, 1), observation_end=date(2026, 10, 8),
        )

    first = run_ingestion(
        database_path=path, sources=["nyfed"], start_date=date(2024, 10, 1),
        end_date=date(2026, 10, 8), adapter_functions={"nyfed": nyfed},
    ).results[0]
    second = run_ingestion(
        database_path=path, sources=["nyfed"], start_date=date(2024, 10, 1),
        end_date=date(2026, 10, 8), adapter_functions={"nyfed": nyfed},
    ).results[0]
    assert (first.status, first.inserted, first.raw_records_added) == ("SUCCESS", 1, 1)
    assert (second.status, second.inserted, second.unchanged, second.raw_records_added) == (
        "SUCCESS", 0, 1, 0
    )
    assert second.observations_total == 1
    assert second.raw_records_total == 1
    dashboard_rows = load_observations(path)
    assert len(dashboard_rows) == 1
    assert dashboard_rows[0]["series_identifier"].startswith("dealer_net_position")
    assert dashboard_rows[0]["raw_record_id"] is not None


def test_failed_source_does_not_rollback_another_sources_transaction(tmp_path):
    path = tmp_path / "independent.sqlite3"

    def persist_nyfed():
        initialize_database(path)
        with database(path) as conn:
            source_id = register_source(conn, identifier="NYFED", name="NY Fed", source_type="fixture")
            series_id = register_series(conn, source_id=source_id, identifier="pd", name="dealer position",
                                        frequency="weekly", default_unit="million_us_dollars")
            raw_id = insert_raw_record(conn, source_id=source_id, payload='{"x":1}',
                                       retrieval_time=datetime(2026, 10, 8, tzinfo=UTC))
            insert_observation(conn, source_id=source_id, series_id=series_id, logical_key="2026-10-07",
                               observation_time="2026-10-07T00:00:00Z",
                               retrieval_time=datetime(2026, 10, 8, tzinfo=UTC), value_numeric=10,
                               unit="million_us_dollars", raw_record_id=raw_id)
        return {"inserted": 1, "unchanged": 0, "missing": 0}

    def fail_cftc():
        raise OSError("public endpoint unavailable")

    summary = run_ingestion(
        database_path=path, sources=["nyfed", "cftc"],
        start_date=date(2024, 10, 1), end_date=date(2026, 10, 8),
        adapter_functions={"nyfed": persist_nyfed, "cftc": fail_cftc},
    )
    assert [result.status for result in summary.results] == ["SUCCESS", "FAILED"]
    assert summary.results[0].inserted == 1
    assert summary.results[0].raw_records_added == 1
    assert "OSError" in summary.results[1].message
    with database(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1


def test_runner_rejects_invalid_dates_and_duplicate_sources(tmp_path):
    try:
        run_ingestion(database_path=tmp_path / "x.db", start_date=date(2026, 2, 1), end_date=date(2026, 1, 1))
    except ValueError as exc:
        assert "after" in str(exc)
    else:
        raise AssertionError("invalid date range was accepted")
    try:
        run_ingestion(database_path=tmp_path / "x.db", sources=["nyfed", "nyfed"])
    except ValueError as exc:
        assert "repeated" in str(exc)
    else:
        raise AssertionError("duplicate source selection was accepted")



def test_orchestrator_offers_each_fred_curve_point_as_its_own_source():
    from treasury_flow_radar.ingest import FRED_SOURCES, SOURCE_NAMES
    assert FRED_SOURCES == {"fred-dgs2": "DGS2", "fred-dgs5": "DGS5", "fred-dgs7": "DGS7",
                            "fred-dgs10": "DGS10", "fred-dgs30": "DGS30", "fred-dfii10": "DFII10",
                            "fred-t10yie": "T10YIE", "fred-threefytp10": "THREEFYTP10",
                            "fred-ig-spread": "BAMLC0A0CM", "fred-hy-spread": "BAMLH0A0HYM2",
                            "fred-sp500": "SP500", "fred-dollar": "DTWEXBGS"}
    assert set(FRED_SOURCES) <= set(SOURCE_NAMES)
