from datetime import datetime, timezone
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError

import pytest

from treasury_flow_radar.database import database, get_observations
from treasury_flow_radar.sources.fred import (
    FredClient,
    FredConfigurationError,
    FredRequestError,
    FredResponseError,
    FredSeriesNotFoundError,
    ingest_fred,
    parse_observations,
)

UTC = timezone.utc
RETRIEVED = datetime(2026, 10, 8, 12, tzinfo=UTC)


def response(series_id="DGS10", observations=None):
    if observations is None:
        observations = [
            {"date": "2026-10-05", "value": "4.125",
             "realtime_start": "2026-10-08", "realtime_end": "9999-12-31"},
            {"date": "2026-10-06", "value": ".", 
             "realtime_start": "2026-10-08", "realtime_end": "9999-12-31"},
        ]
    return json.dumps({
        "realtime_start": "2026-10-08",
        "realtime_end": "2026-10-08",
        "observation_start": "2026-10-05",
        "observation_end": "2026-10-06",
        "units": "Percent",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(observations),
        "offset": 0,
        "limit": 100000,
        "observations": observations,
    })


class FakeHTTPResponse:
    status = 200

    def __init__(self, body):
        self.body = body.encode("utf-8")
        self.headers = {"Content-Type": "application/json", "ETag": '"fred-test"'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class FixtureOpener:
    def __init__(self, payloads):
        self.payloads = payloads
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        series_id = parse_qs(urlsplit(request.full_url).query)["series_id"][0]
        payload = self.payloads[series_id]
        if isinstance(payload, list):
            payload = payload.pop(0)
        return FakeHTTPResponse(payload)


def fixed_client(payloads, *, clock=None):
    return FredClient(
        api_key="a" * 32,
        opener=FixtureOpener(payloads),
        clock=clock or (lambda: RETRIEVED),
    )


@pytest.mark.parametrize(
    ("series_id", "value"),
    [("DGS10", 4.125), ("DGS2", 3.875)],
)
def test_fred_response_parsing_and_yield_normalization(series_id, value):
    parsed = parse_observations(response(series_id, [
        {"date": "2026-10-05", "value": str(value)}
    ]))
    assert len(parsed) == 1
    assert parsed[0].observation_date.isoformat() == "2026-10-05"
    assert parsed[0].value == value
    assert parsed[0].raw_value == str(value)


def test_missing_marker_is_missing_not_zero():
    parsed = parse_observations(response(observations=[
        {"date": "2026-10-05", "value": "."}
    ]))
    assert parsed[0].value is None
    assert parsed[0].raw_value == "."


def test_complete_mocked_ingestion_preserves_raw_payload_and_provenance(tmp_path):
    body = response("DGS10")
    client = fixed_client({"DGS10": body})
    db_path = tmp_path / "data" / "fred.sqlite3"

    result = ingest_fred(["DGS10"], database_path=db_path, client=client)

    assert result[0].inserted == 2
    assert result[0].missing == 1
    with database(db_path) as conn:
        rows = get_observations(conn)
        assert len(rows) == 2
        assert all(row["publication_time"] is None for row in rows)
        assert rows[0]["observation_time"] == "2026-10-05T00:00:00.000000Z"
        assert rows[0]["retrieval_time"] == "2026-10-08T12:00:00.000000Z"
        assert rows[0]["value_numeric"] == 4.125
        assert rows[0]["unit"] == "percent"
        assert rows[1]["value_numeric"] is None
        assert rows[1]["raw_value"] == "."
        linked = conn.execute(
            """SELECT o.raw_record_id, r.payload, r.source_id, s.identifier AS series_code,
                      src.identifier AS source_code
               FROM observations o
               JOIN raw_records r ON r.id = o.raw_record_id
               JOIN series s ON s.id = o.series_id
               JOIN sources src ON src.id = o.source_id
               WHERE o.logical_key = '2026-10-05'"""
        ).fetchone()
        assert linked["raw_record_id"] == rows[0]["raw_record_id"]
        assert linked["payload"] == body
        assert linked["source_code"] == "FRED"
        assert linked["series_code"] == "DGS10"


def test_both_supported_series_register_without_duplicates(tmp_path):
    payloads = {
        "DGS10": response("DGS10", [{"date": "2026-10-05", "value": "4.125"}]),
        "DGS2": response("DGS2", [{"date": "2026-10-05", "value": "3.875"}]),
    }
    client = fixed_client(payloads)
    db_path = tmp_path / "fred.sqlite3"
    ingest_fred(["DGS10", "DGS2"], database_path=db_path, client=client)
    ingest_fred(["DGS10", "DGS2"], database_path=db_path, client=client)
    with database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 2
        names = {row["identifier"]: row["name"] for row in conn.execute(
            "SELECT identifier, name FROM series"
        )}
        assert names["DGS10"] == "10-Year Treasury Constant Maturity Rate"
        assert names["DGS2"] == "2-Year Treasury Constant Maturity Rate"


def test_identical_ingestion_is_idempotent(tmp_path):
    body = response(observations=[
        {"date": "2026-10-05", "value": "4.125"}
    ])
    client = fixed_client({"DGS10": body})
    db_path = tmp_path / "fred.sqlite3"
    first = ingest_fred(["DGS10"], database_path=db_path, client=client)
    second = ingest_fred(["DGS10"], database_path=db_path, client=client)
    assert first[0].inserted == 1
    assert second[0].inserted == 0
    assert second[0].unchanged == 1
    with database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1


def test_changed_provider_value_is_a_new_revision(tmp_path):
    bodies = [
        response(observations=[{"date": "2026-10-05", "value": "4.125"}]),
        response(observations=[{"date": "2026-10-05", "value": "4.126"}]),
    ]
    client = fixed_client({"DGS10": bodies}, clock=lambda: RETRIEVED)
    retrieval_times = iter([RETRIEVED, datetime(2026, 10, 9, 12, tzinfo=UTC)])
    client._clock = lambda: next(retrieval_times)
    db_path = tmp_path / "fred.sqlite3"

    ingest_fred(["DGS10"], database_path=db_path, client=client)
    ingest_fred(["DGS10"], database_path=db_path, client=client)

    with database(db_path) as conn:
        rows = get_observations(conn, logical_key="2026-10-05")
        assert [row["revision"] for row in rows] == [1, 2]
        assert [row["value_numeric"] for row in rows] == [4.125, 4.126]
        assert rows[1]["revision_of_id"] == rows[0]["id"]
        assert rows[0]["raw_record_id"] != rows[1]["raw_record_id"]

def test_malformed_provider_responses_are_explicit():
    with pytest.raises(FredResponseError, match="invalid JSON"):
        parse_observations("{broken")
    with pytest.raises(FredResponseError, match="malformed value"):
        parse_observations(response(observations=[
            {"date": "2026-10-05", "value": "not-a-yield"}
        ]))
    with pytest.raises(FredSeriesNotFoundError, match="no observations"):
        parse_observations(json.dumps({"count": 0, "observations": []}))


def test_missing_credentials_fail_before_network(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(FredConfigurationError, match="FRED_API_KEY"):
        FredClient()


def test_network_failure_is_explicit():
    def fail(request, timeout):
        raise URLError("offline")

    client = FredClient(api_key="a" * 32, opener=fail)
    with pytest.raises(FredRequestError, match="network request failed"):
        client.fetch_series("DGS10")


def test_ingestion_failure_does_not_create_partial_database_changes(tmp_path):
    payloads = {
        "DGS10": response("DGS10", [{"date": "2026-10-05", "value": "4.125"}]),
        "DGS2": "{bad-json",
    }
    client = fixed_client(payloads)
    db_path = tmp_path / "fred.sqlite3"
    with pytest.raises(FredResponseError):
        ingest_fred(["DGS10", "DGS2"], database_path=db_path, client=client)
    assert not db_path.exists()



def test_database_failures_are_not_silently_swallowed(tmp_path):
    payload = response("DGS10", [{"date": "2026-10-05", "value": "4.125"}])
    client = fixed_client({"DGS10": payload})
    db_path = tmp_path / "this-is-a-directory"
    db_path.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        ingest_fred(["DGS10"], database_path=db_path, client=client)
