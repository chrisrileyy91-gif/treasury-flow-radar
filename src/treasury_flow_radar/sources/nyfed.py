"""NY Fed Primary Dealer Statistics Treasury net-position adapter.

This module measures an aggregate dealer-reported position. It does not infer
intent, causation, manipulation, or a directional market signal.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

SOURCE_IDENTIFIER = "NYFED"
SOURCE_NAME = "Federal Reserve Bank of New York Primary Dealer Statistics"
SOURCE_URL = "https://www.newyorkfed.org/markets/counterparties/primary-dealers-statistics"
API_ROOT = "https://markets.newyorkfed.org/api/pd"
DEFAULT_SERIES_BREAK = "SBN2024"
DEFAULT_KEY_ID = "PDPOSGST-TOT"
UNIT = "million_us_dollars"
TIME_SERIES_URL = API_ROOT + "/get/{series_break}/timeseries/{key_id}.json"
TIMEOUT_SECONDS = 30


class NyfedError(RuntimeError):
    """Base NY Fed adapter error."""


class NyfedRequestError(NyfedError):
    """The official endpoint could not be reached or returned an HTTP error."""


class NyfedResponseError(NyfedError):
    """The official endpoint response was not valid for the selected metric."""


@dataclass(frozen=True)
class NyfedObservation:
    observation_date: date
    value: float | None
    raw_value: str
    key_id: str
    series_break: str
    source_metadata: dict[str, Any]


@dataclass(frozen=True)
class NyfedPayload:
    series_break: str
    key_id: str
    observations: tuple[NyfedObservation, ...]
    raw_payload: str
    response_metadata: dict[str, Any]
    retrieval_time: datetime


@dataclass(frozen=True)
class IngestionResult:
    series_break: str
    key_id: str
    inserted: int
    unchanged: int
    missing: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_date(value: Any) -> date:
    if not isinstance(value, str):
        raise NyfedResponseError("observation date must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise NyfedResponseError(f"invalid observation date: {value!r}") from exc


def _parse_value(value: Any) -> tuple[float | None, str]:
    if value is None:
        return None, ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise NyfedResponseError(f"malformed numeric value: {value!r}")
    raw_value = str(value).strip()
    if raw_value in {"", ".", "*", "NA", "N/A"}:
        return None, raw_value
    try:
        parsed = float(raw_value.replace(",", ""))
    except ValueError as exc:
        raise NyfedResponseError(f"malformed numeric value: {raw_value!r}") from exc
    if not math.isfinite(parsed):
        raise NyfedResponseError(f"non-finite numeric value: {raw_value!r}")
    return parsed, raw_value


def parse_timeseries(
    raw_payload: str,
    *,
    series_break: str = DEFAULT_SERIES_BREAK,
    key_id: str = DEFAULT_KEY_ID,
) -> tuple[NyfedObservation, ...]:
    """Parse one official NY Fed timeseries JSON response; preserve source strings."""
    try:
        document = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NyfedResponseError("invalid JSON response") from exc
    pd_data = document.get("pd") if isinstance(document, dict) else None
    records = pd_data.get("timeseries") if isinstance(pd_data, dict) else None
    if not isinstance(records, list):
        raise NyfedResponseError("response is missing pd.timeseries array")

    selected = [
        item for item in records
        if isinstance(item, dict) and item.get("keyid") == key_id
    ]
    if not selected:
        raise NyfedResponseError(f"selected keyid {key_id!r} is absent from response")

    parsed: list[NyfedObservation] = []
    for item in selected:
        record_break = item.get("seriesbreak")
        if record_break is not None and record_break != series_break:
            raise NyfedResponseError(
                f"response seriesbreak {record_break!r} does not match {series_break!r}"
            )
        date_value = item.get("asofdate", item.get("asOfDate"))
        if date_value is None:
            date_value = item.get("observationDate", item.get("observationdate"))
        if date_value is None:
            raise NyfedResponseError("selected record is missing asofdate")
        observation_date = _parse_date(date_value)
        value, raw_value = _parse_value(item.get("value"))
        metadata = {
            key: item[key]
            for key in ("seriesbreak", "description", "units", "releaseDate")
            if key in item
        }
        parsed.append(NyfedObservation(
            observation_date=observation_date,
            value=value,
            raw_value=raw_value,
            key_id=key_id,
            series_break=series_break,
            source_metadata=metadata,
        ))
    parsed.sort(key=lambda row: row.observation_date)
    if len({row.observation_date for row in parsed}) != len(parsed):
        raise NyfedResponseError("duplicate observation date in response")
    return tuple(parsed)


class NyfedClient:
    """Small injectable HTTP client for the official NY Fed Markets Data API."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] = _utc_now,
        user_agent: str | None = None,
    ) -> None:
        self._opener = opener
        self._clock = clock
        self._user_agent = user_agent or os.getenv(
            "TFR_USER_AGENT", "treasury-flow-radar/0.1 (research)"
        )

    def fetch_series(
        self,
        *,
        series_break: str = DEFAULT_SERIES_BREAK,
        key_id: str = DEFAULT_KEY_ID,
    ) -> NyfedPayload:
        if not series_break or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in series_break):
            raise ValueError("series_break must contain only letters, digits, '-' or '_'")
        if not key_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in key_id):
            raise ValueError("key_id must contain only letters, digits, '-' or '_'")
        url = TIME_SERIES_URL.format(series_break=series_break, key_id=key_id)
        request = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        })
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read()
                headers = dict(response.headers.items())
                status = getattr(response, "status", 200)
        except HTTPError as exc:
            raise NyfedRequestError(f"official NY Fed API returned HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise NyfedRequestError(f"official NY Fed API request failed: {exc}") from exc
        if status < 200 or status >= 300:
            raise NyfedRequestError(f"official NY Fed API returned HTTP {status}")
        try:
            raw_payload = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NyfedResponseError("response body is not UTF-8 JSON") from exc
        observations = parse_timeseries(
            raw_payload, series_break=series_break, key_id=key_id
        )
        retrieval_time = self._clock()
        if retrieval_time.utcoffset() is None:
            raise ValueError("client clock must return a timezone-aware datetime")
        return NyfedPayload(
            series_break=series_break,
            key_id=key_id,
            observations=observations,
            raw_payload=raw_payload,
            response_metadata={
                "request_url": url,
                "http_status": status,
                "response_headers": headers,
                "provider": "NY Fed",
                "series_break": series_break,
                "key_id": key_id,
            },
            retrieval_time=retrieval_time,
        )


def _series_identifier(series_break: str, key_id: str) -> str:
    # Series breaks can change definitions; keeping them separate avoids silently
    # merging potentially non-comparable historical measurements.
    safe_break = series_break.lower().replace("-", "_")
    safe_key = key_id.lower().replace("-", "_")
    return f"dealer_net_position_nominal_treasury_ex_tips_{safe_break}_{safe_key}"


def ingest_nyfed(
    *,
    database_path: str | Path = DEFAULT_DB_PATH,
    client: NyfedClient | None = None,
    series_break: str = DEFAULT_SERIES_BREAK,
    key_id: str = DEFAULT_KEY_ID,
    observation_start: date | None = None,
    observation_end: date | None = None,
) -> IngestionResult:
    """Fetch and persist selected weekly historical observations idempotently."""
    if observation_start and observation_end and observation_start > observation_end:
        raise ValueError("observation_start must not be after observation_end")
    payload = (client or NyfedClient()).fetch_series(
        series_break=series_break, key_id=key_id
    )
    selected_observations = tuple(
        row for row in payload.observations
        if (observation_start is None or row.observation_date >= observation_start)
        and (observation_end is None or row.observation_date <= observation_end)
    )
    initialize_database(database_path)
    with database(database_path) as conn:
        source_id = register_source(
            conn,
            identifier=SOURCE_IDENTIFIER,
            name=SOURCE_NAME,
            source_type="financial_market_data",
            url=SOURCE_URL,
            metadata={
                "provider": "Federal Reserve Bank of New York",
                "api_base": API_ROOT,
                "series_break": series_break,
            },
        )
        series_id = register_series(
            conn,
            source_id=source_id,
            identifier=_series_identifier(series_break, key_id),
            name="Primary dealer net position: nominal U.S. Treasury securities ex-TIPS",
            description=(
                "Aggregate primary dealer long positions minus short positions in "
                "U.S. Treasury securities excluding TIPS."
            ),
            instrument_type="treasury_position",
            frequency="weekly",
            default_unit=UNIT,
            metadata={
                "provider": "NY Fed",
                "source_keyid": key_id,
                "series_break": series_break,
                "observation_units": "millions of U.S. dollars",
                "source_series_url": TIME_SERIES_URL.format(
                    series_break=series_break, key_id=key_id
                ),
                "observation_precision": "calendar_date",
            },
        )
        raw_record_id = insert_raw_record(
            conn,
            source_id=source_id,
            external_record_id=f"{series_break}:{key_id}",
            payload=payload.raw_payload,
            content_type="application/json",
            retrieval_time=payload.retrieval_time,
            metadata=payload.response_metadata,
        )

        inserted = unchanged = missing = 0
        for observation in selected_observations:
            logical_key = observation.observation_date.isoformat()
            current = get_observations(
                conn, source_id=source_id, series_id=series_id, logical_key=logical_key
            )
            if current:
                latest = max(current, key=lambda row: row["revision"])
                if (latest["raw_value"] == observation.raw_value
                        and latest["value_numeric"] == observation.value):
                    unchanged += 1
                    if observation.value is None:
                        missing += 1
                    continue
                revision = latest["revision"] + 1
                revision_of_id = latest["id"]
            else:
                revision = 1
                revision_of_id = None
            observation_time = datetime.combine(
                observation.observation_date, time.min, tzinfo=UTC
            )
            insert_observation(
                conn,
                source_id=source_id,
                series_id=series_id,
                logical_key=logical_key,
                revision=revision,
                revision_of_id=revision_of_id,
                observation_time=observation_time,
                # NY Fed gives an approximate weekly update schedule, not exact
                # publication timestamps for each historical record.
                publication_time=None,
                retrieval_time=payload.retrieval_time,
                value_numeric=observation.value,
                unit=UNIT,
                raw_value=observation.raw_value,
                raw_record_id=raw_record_id,
                metadata={
                    "provider": "NY Fed",
                    "key_id": key_id,
                    "series_break": series_break,
                    "observation_date": logical_key,
                    "observation_precision": "calendar_date",
                    "source_record_metadata": observation.source_metadata,
                    "normalization_range": {
                        "start": observation_start.isoformat() if observation_start else None,
                        "end": observation_end.isoformat() if observation_end else None,
                    },
                },
            )
            inserted += 1
            if observation.value is None:
                missing += 1
    return IngestionResult(series_break, key_id, inserted, unchanged, missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest NY Fed aggregate nominal Treasury dealer net positions."
    )
    parser.add_argument(
        "--series-break", default=DEFAULT_SERIES_BREAK,
        help="NY Fed structural series window to ingest (default: SBN2024)",
    )
    parser.add_argument(
        "--key-id", default=DEFAULT_KEY_ID,
        help="NY Fed provider keyid (default: PDPOSGST-TOT)",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("TREASURY_FLOW_RADAR_DB", str(DEFAULT_DB_PATH)),
        help="SQLite database path (default: TREASURY_FLOW_RADAR_DB or project data path)",
    )
    args = parser.parse_args(argv)
    try:
        result = ingest_nyfed(
            database_path=args.database,
            series_break=args.series_break,
            key_id=args.key_id,
        )
    except NyfedError as exc:
        parser.exit(2, f"NY Fed ingestion failed: {exc}\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Ingestion failed: {exc}\n")
    print(
        f"{result.series_break}/{result.key_id}: inserted={result.inserted}, "
        f"unchanged={result.unchanged}, missing={result.missing}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

