from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from treasury_flow_radar.database import database, get_observations
from treasury_flow_radar.sources.fred import FredClient, FredResponseError
from treasury_flow_radar.sources.fred_releases import ingest_release_dates, parse_release_dates


class _Response:
    status = 200

    def __init__(self, body: str) -> None:
        self.body = body.encode()

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _opener(payloads):
    def open_(request, timeout):
        from urllib.parse import parse_qs, urlsplit
        rid = int(parse_qs(urlsplit(request.full_url).query)["release_id"][0])
        return _Response(payloads[rid])
    return open_


def _payload(rid: int, days: list[str]) -> str:
    return json.dumps({"release_dates": [{"release_id": rid, "date": d} for d in days]})


def test_parse_rejects_other_release_and_malformed():
    assert parse_release_dates(_payload(10, ["2026-09-11", "2026-08-12"]), 10) == (date(2026, 8, 12), date(2026, 9, 11))
    with pytest.raises(FredResponseError):
        parse_release_dates(_payload(50, ["2026-09-11"]), 10)
    with pytest.raises(FredResponseError):
        parse_release_dates("{}", 10)


def test_ingest_stores_dated_text_facts_idempotently(tmp_path):
    db = tmp_path / "r.sqlite3"
    payloads = {10: _payload(10, ["2026-09-11"]), 50: _payload(50, ["2026-10-02"]),
                54: _payload(54, ["2026-09-26"]), 101: _payload(101, ["2026-09-16"])}
    client = FredClient(api_key="a" * 32, opener=_opener(payloads), clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    first = ingest_release_dates(database_path=db, client=client)
    again = ingest_release_dates(database_path=db, client=client)
    assert first["inserted"] == 4 and again["inserted"] == 0 and again["unchanged"] == 4
    with database(db) as conn:
        rows = get_observations(conn)
        jobs = next(r for r in rows if r["logical_key"] == "2026-10-02")
        assert jobs["value_text"] == "Employment Situation" and jobs["value_numeric"] is None
        assert jobs["publication_time"] is None
        payload = conn.execute("SELECT payload FROM raw_records").fetchall()
        assert all("a" * 32 not in p[0] for p in payload)   # the API key is never stored
