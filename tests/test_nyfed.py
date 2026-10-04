import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from treasury_flow_radar.database import database, get_observations
from treasury_flow_radar.sources.nyfed import (
    DEFAULT_KEY_ID,
    DEFAULT_SERIES_BREAK,
    NyfedClient,
    NyfedRequestError,
    NyfedResponseError,
    ingest_nyfed,
    parse_timeseries,
)

RETRIEVED = datetime(2026, 10, 8, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "nyfed_primary_dealer_treasury.json"


class FakeResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {
        "Content-Type": "application/json",
        "ETag": '"nyfed-fixture"',
    }

    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class FixtureOpener:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)


def client_for(body_or_bodies):
    bodies = body_or_bodies if isinstance(body_or_bodies, list) else [body_or_bodies]
    opener = FixtureOpener(bodies)
    return NyfedClient(opener=opener, clock=lambda: RETRIEVED), opener


def fixture_text():
    return FIXTURE.read_text(encoding="utf-8")


def test_official_json_fixture_extracts_only_treasury_net_position():
    body = fixture_text()
    observations = parse_timeseries(body)
    assert len(observations) == 2
    assert [row.observation_date.isoformat() for row in observations] == [
        "2026-09-30", "2026-10-07"
    ]
    assert observations[0].value == 172345.6
    assert observations[0].raw_value == "172345.6"
    assert observations[1].value is None
    assert observations[1].raw_value == "*"
    assert observations[0].key_id == DEFAULT_KEY_ID
    assert observations[0].series_break == DEFAULT_SERIES_BREAK


@pytest.mark.parametrize("raw,expected", [("1,234.50", 1234.5), ("-25", -25.0)])
def test_numeric_values_preserve_source_units(raw, expected):
    payload = json.dumps({"pd": {"timeseries": [{
        "keyid": DEFAULT_KEY_ID, "seriesbreak": DEFAULT_SERIES_BREAK,
        "asofdate": "2026-10-07", "value": raw
    }]}})
    observation = parse_timeseries(payload)[0]
    assert observation.value == expected
    assert observation.raw_value == raw
    assert observation.observation_date.isoformat() == "2026-10-07"


def test_malformed_dates_values_and_json_fail_explicitly():
    with pytest.raises(NyfedResponseError, match="invalid JSON"):
        parse_timeseries("{bad")
    with pytest.raises(NyfedResponseError, match="invalid observation date"):
        parse_timeseries(json.dumps({"pd": {"timeseries": [{
            "keyid": DEFAULT_KEY_ID, "asofdate": "2026-99-77", "value": "1"
        }]}}))
    with pytest.raises(NyfedResponseError, match="malformed numeric value"):
        parse_timeseries(json.dumps({"pd": {"timeseries": [{
            "keyid": DEFAULT_KEY_ID, "asofdate": "2026-10-07", "value": "bad"
        }]}}))
    with pytest.raises(NyfedResponseError, match="absent"):
        parse_timeseries(json.dumps({"pd": {"timeseries": []}}))


def test_http_request_uses_official_historical_series_endpoint():
    client, opener = client_for(fixture_text())
    client.fetch_series()
    request, timeout = opener.requests[0]
    parsed = urlsplit(request.full_url)
    assert parsed.netloc == "markets.newyorkfed.org"
    assert parsed.path.endswith(
        f"/api/pd/get/{DEFAULT_SERIES_BREAK}/timeseries/{DEFAULT_KEY_ID}.json"
    )
    assert timeout > 0
    assert request.get_header("Accept") == "application/json"


def test_live_client_failure_is_explicit():
    from urllib.error import URLError

    client, _ = client_for([URLError("offline")])
    with pytest.raises(NyfedRequestError, match="request failed"):
        client.fetch_series()


def test_complete_mocked_ingestion_preserves_raw_payload_and_provenance(tmp_path):
    body = fixture_text()
    client, _ = client_for(body)
    db_path = tmp_path / "data" / "nyfed.sqlite3"

    result = ingest_nyfed(database_path=db_path, client=client)

    assert result.inserted == 2
    assert result.missing == 1
    with database(db_path) as conn:
        rows = get_observations(conn)
        assert len(rows) == 2
        assert all(row["publication_time"] is None for row in rows)
        assert rows[0]["observation_time"] == "2026-09-30T00:00:00.000000Z"
        assert rows[0]["retrieval_time"] == "2026-10-08T12:00:00.000000Z"
        assert rows[0]["value_numeric"] == 172345.6
        assert rows[0]["raw_value"] == "172345.6"
        assert rows[0]["unit"] == "million_us_dollars"
        assert rows[1]["value_numeric"] is None
        linked = conn.execute(
            """SELECT r.payload, src.identifier AS source_code, s.identifier AS series_code
               FROM observations o
               JOIN raw_records r ON r.id = o.raw_record_id
               JOIN sources src ON src.id = o.source_id
               JOIN series s ON s.id = o.series_id
               WHERE o.logical_key = '2026-09-30'"""
        ).fetchone()
        assert linked["payload"] == body
        assert linked["source_code"] == "NYFED"
        assert "sbn2024" in linked["series_code"]
        raw_meta = conn.execute(
            "SELECT content_type, metadata_json FROM raw_records"
        ).fetchone()
        assert raw_meta["content_type"] == "application/json"
        assert "request_url" in raw_meta["metadata_json"]


def test_repeated_ingestion_is_idempotent(tmp_path):
    body = json.dumps({"pd": {"timeseries": [{
        "keyid": DEFAULT_KEY_ID, "seriesbreak": DEFAULT_SERIES_BREAK,
        "asofdate": "2026-10-07", "value": "10.25"
    }]}})
    db_path = tmp_path / "nyfed.sqlite3"
    first = ingest_nyfed(database_path=db_path, client=client_for(body)[0])
    second = ingest_nyfed(database_path=db_path, client=client_for(body)[0])
    assert (first.inserted, second.inserted, second.unchanged) == (1, 0, 1)
    with database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1


def test_provider_correction_creates_immutable_revision(tmp_path):
    bodies = [
        json.dumps({"pd": {"timeseries": [{
            "keyid": DEFAULT_KEY_ID, "seriesbreak": DEFAULT_SERIES_BREAK,
            "asofdate": "2026-10-07", "value": "10.25"
        }]}}),
        json.dumps({"pd": {"timeseries": [{
            "keyid": DEFAULT_KEY_ID, "seriesbreak": DEFAULT_SERIES_BREAK,
            "asofdate": "2026-10-07", "value": "10.50"
        }]}}),
    ]
    times = iter([RETRIEVED, datetime(2026, 10, 9, 12, tzinfo=UTC)])
    db_path = tmp_path / "nyfed.sqlite3"
    for body in bodies:
        ingest_nyfed(
            database_path=db_path,
            client=NyfedClient(
                opener=FixtureOpener([body]), clock=lambda: next(times)
            ),
        )
    with database(db_path) as conn:
        rows = get_observations(conn, logical_key="2026-10-07")
        assert [row["revision"] for row in rows] == [1, 2]
        assert [row["value_numeric"] for row in rows] == [10.25, 10.5]
        assert rows[1]["revision_of_id"] == rows[0]["id"]
        assert rows[0]["raw_record_id"] != rows[1]["raw_record_id"]


def test_series_breaks_get_distinct_series_identity():
    base = "dealer_net_position_nominal_treasury_ex_tips"
    from treasury_flow_radar.sources.nyfed import _series_identifier

    assert _series_identifier("SBN2024", DEFAULT_KEY_ID).startswith(base)
    assert _series_identifier("SBN2022", DEFAULT_KEY_ID) != _series_identifier(
        "SBN2024", DEFAULT_KEY_ID
    )

