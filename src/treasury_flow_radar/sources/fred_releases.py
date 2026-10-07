"""FRED release calendar: the dates major macro data were published.

These dates are candidate explanations for Treasury moves and confounders for any
issuance test: a CPI or jobs day can move yields on its own. The adapter requests the
official FRED ``/fred/release/dates`` endpoint for a fixed set of releases, keeps the
complete JSON response in ``raw_records``, and stores each release date as a text
observation (FACT: "release X was published on date D"). The date is a calendar date;
FRED does not supply a release time of day, so none is claimed.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from treasury_flow_radar.database import (
    DEFAULT_DB_PATH,
    database,
    get_observations,
    initialize_database,
    insert_observation,
    insert_raw_record,
    register_series,
    register_source,
)
from treasury_flow_radar.sources.fred import (
    SOURCE_IDENTIFIER,
    SOURCE_NAME,
    SOURCE_URL,
    FredClient,
    FredRequestError,
    FredResponseError,
)

RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
# FRED release ids, confirmed on fred.stlouisfed.org/release?rid=N.
# "weight" is the attribution prior: how strongly the release type is expected to move
# Treasuries (an explicit, adjustable assumption; see analytics/attribution.py).
RELEASES: dict[int, dict[str, Any]] = {
    10: {"name": "Consumer Price Index", "short": "CPI", "kind": "inflation", "weight": 1.0},
    50: {"name": "Employment Situation", "short": "Jobs report", "kind": "labor", "weight": 1.0},
    54: {"name": "Personal Income and Outlays", "short": "PCE / personal income", "kind": "inflation", "weight": 0.6},
    53: {"name": "Gross Domestic Product", "short": "GDP", "kind": "growth", "weight": 0.6},
    9: {"name": "Advance Monthly Sales for Retail and Food Services", "short": "Retail sales", "kind": "growth", "weight": 0.6},
    46: {"name": "Producer Price Index", "short": "PPI", "kind": "inflation", "weight": 0.5},
    192: {"name": "Job Openings and Labor Turnover Survey", "short": "JOLTS", "kind": "labor", "weight": 0.5},
    180: {"name": "Unemployment Insurance Weekly Claims Report", "short": "Jobless claims", "kind": "labor", "weight": 0.4},
}
# A real publication calendar has at most weekly dates; more than this per year means the
# FRED "release" tracks daily series updates rather than publication events.
MAX_RELEASE_DATES_PER_YEAR = 70
# Not used: FRED release 101 ("FOMC Press Release") records a release date on every day
# because its series update daily, so it is not a meeting calendar. FOMC decision dates
# come from events/fomc_meetings.json (Federal Reserve meeting calendar) instead.
EXCLUDED_RELEASE_IDS = frozenset({101})
EVENT_UNIT = "release_event"


@dataclass(frozen=True)
class ReleaseDates:
    release_id: int
    dates: tuple[date, ...]
    raw_payload: str
    retrieval_time: datetime


def series_identifier(release_id: int) -> str:
    return f"FRED_RELEASE_{release_id}"


def parse_release_dates(payload: str, release_id: int) -> tuple[date, ...]:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FredResponseError("FRED release dates response is not valid JSON") from exc
    rows = document.get("release_dates") if isinstance(document, Mapping) else None
    if not isinstance(rows, list):
        raise FredResponseError("FRED release dates response has no release_dates list")
    dates = set()
    for row in rows:
        if not isinstance(row, Mapping) or "date" not in row:
            raise FredResponseError("FRED release dates row is missing its date")
        if int(row.get("release_id", release_id)) != release_id:
            raise FredResponseError("FRED release dates row belongs to another release")
        dates.add(date.fromisoformat(str(row["date"])))
    return tuple(sorted(dates))


def fetch_release_dates(client: FredClient, release_id: int, *, start: date | None = None,
                        end: date | None = None,
                        opener: Callable[..., Any] | None = None) -> ReleaseDates:
    parameters = {"release_id": str(release_id), "api_key": client.api_key, "file_type": "json",
                  "sort_order": "asc", "include_release_dates_with_no_data": "true", "limit": "10000"}
    if start:
        parameters["realtime_start"] = start.isoformat()
    if end:
        parameters["realtime_end"] = end.isoformat()
    request = Request(f"{RELEASE_DATES_URL}?{urlencode(parameters)}",
                      headers={"Accept": "application/json", "User-Agent": client.user_agent})
    try:
        with (opener or client._opener)(request, timeout=client.timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read()
    except HTTPError as exc:
        # Never expose the request URL: it contains the API key.
        raise FredRequestError(f"FRED release dates request failed with status {exc.code}") from None
    except (URLError, OSError) as exc:
        raise FredRequestError(f"FRED release dates network request failed: {exc}") from None
    if status < 200 or status >= 300:
        raise FredRequestError(f"FRED release dates request failed with status {status}")
    retrieved = client._clock()
    payload = body.decode("utf-8")
    return ReleaseDates(release_id, parse_release_dates(payload, release_id), payload, retrieved)


def ingest_release_dates(*, database_path: str | Path = DEFAULT_DB_PATH, client: FredClient | None = None,
                         start_date: date | None = None, end_date: date | None = None,
                         release_ids: tuple[int, ...] = tuple(RELEASES)) -> dict[str, int]:
    """Fetch every selected release calendar first, then persist in one transaction."""
    fred = client or FredClient()
    fetched = [fetch_release_dates(fred, rid, start=start_date, end=end_date) for rid in release_ids]
    initialize_database(database_path)
    inserted = unchanged = 0
    with database(database_path) as conn:
        source_id = register_source(conn, identifier=SOURCE_IDENTIFIER, name=SOURCE_NAME,
                                    source_type="economic_data", url=SOURCE_URL,
                                    metadata={"provider": "FRED"})
        for item in fetched:
            info = RELEASES[item.release_id]
            series_id = register_series(
                conn, source_id=source_id, identifier=series_identifier(item.release_id),
                name=f"Release dates: {info['name']}",
                description="Calendar dates on which FRED records this release as published.",
                instrument_type="release_calendar", frequency="irregular", default_unit=EVENT_UNIT,
                metadata={"provider": "FRED", "release_id": item.release_id, "kind": info["kind"],
                          "source_release_url": f"https://fred.stlouisfed.org/release?rid={item.release_id}"})
            raw_id = insert_raw_record(
                conn, source_id=source_id, external_record_id=f"release_dates:{item.release_id}",
                payload=item.raw_payload, content_type="application/json",
                retrieval_time=item.retrieval_time,
                metadata={"endpoint": RELEASE_DATES_URL, "release_id": item.release_id})
            for day in item.dates:
                if start_date and day < start_date or end_date and day > end_date:
                    continue
                key = day.isoformat()
                if get_observations(conn, source_id=source_id, series_id=series_id, logical_key=key):
                    unchanged += 1
                    continue
                insert_observation(
                    conn, source_id=source_id, series_id=series_id, logical_key=key, revision=1,
                    revision_of_id=None, observation_time=datetime.combine(day, time.min, tzinfo=UTC),
                    publication_time=None, retrieval_time=item.retrieval_time, value_numeric=None,
                    value_text=info["name"], unit=EVENT_UNIT, raw_value=key, raw_record_id=raw_id,
                    metadata={"provider": "FRED", "release_id": item.release_id, "release_name": info["name"],
                              "short_name": info["short"], "kind": info["kind"],
                              "observation_precision": "calendar_date"})
                inserted += 1
    return {"inserted": inserted, "unchanged": unchanged, "missing": 0}
