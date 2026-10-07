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
                54: _payload(54, ["2026-09-26"])}
    client = FredClient(api_key="a" * 32, opener=_opener(payloads), clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    first = ingest_release_dates(database_path=db, client=client)
    again = ingest_release_dates(database_path=db, client=client)
    assert first["inserted"] == 3 and again["inserted"] == 0 and again["unchanged"] == 3
    with database(db) as conn:
        rows = get_observations(conn)
        jobs = next(r for r in rows if r["logical_key"] == "2026-10-02")
        assert jobs["value_text"] == "Employment Situation" and jobs["value_numeric"] is None
        assert jobs["publication_time"] is None
        payload = conn.execute("SELECT payload FROM raw_records").fetchall()
        assert all("a" * 32 not in p[0] for p in payload)   # the API key is never stored


def test_fomc_comes_from_the_fed_calendar_not_fred_release_101():
    from treasury_flow_radar.events import load_fomc_decisions
    from treasury_flow_radar.sources.fred_releases import EXCLUDED_RELEASE_IDS, RELEASES
    assert 101 not in RELEASES and 101 in EXCLUDED_RELEASE_IDS
    decisions = {d["date"] for d in load_fomc_decisions()}
    assert "2026-09-16" in decisions and "2026-10-28" in decisions
    assert "2026-10-01" not in decisions and "2025-08-22" not in decisions   # no meeting; notation vote


def test_report_ignores_daily_fred_101_rows_when_ranking():
    from treasury_flow_radar.analytics.research import build_research_report
    days = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
    rows = []
    for i, d in enumerate(days):
        for sid, base in (("DGS10", 5.0), ("DGS2", 4.8), ("DGS30", 5.4)):
            rows.append({"series_identifier": sid, "observation_time": d, "value_numeric": base + i / 100,
                         "unit": "percent", "source_identifier": "FRED", "revision": 1})
        rows.append({"series_identifier": "FRED_RELEASE_101", "observation_time": d, "value_numeric": None,
                     "value_text": "FOMC Press Release", "unit": "release_event", "source_identifier": "FRED",
                     "revision": 1, "metadata": {"release_id": 101, "short_name": "FOMC decision", "kind": "policy"}})
    report = build_research_report(rows, start_date=date(2026, 9, 1), end_date=date(2026, 10, 2))
    assert not [c for c in report["candidates"]["candidates"] if c["name"].startswith("FOMC")]
