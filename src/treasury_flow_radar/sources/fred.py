"""FRED observations adapter and SQLite ingestion for selected Treasury yields."""
from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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

API_URL = "https://api.stlouisfed.org/fred/series/observations"
SUPPORTED_SERIES = {
    "DGS10": {
        "name": "10-Year Treasury Constant Maturity Rate",
        "description": "Market yield on U.S. Treasury securities at 10-year constant maturity.",
    },
    "DGS2": {
        "name": "2-Year Treasury Constant Maturity Rate",
        "description": "Market yield on U.S. Treasury securities at 2-year constant maturity.",
    },
}
SOURCE_IDENTIFIER = "FRED"
SOURCE_NAME = "Federal Reserve Bank of St. Louis FRED"
SOURCE_URL = "https://fred.stlouisfed.org/"
RATE_UNIT = "percent"


class FredError(RuntimeError):
    """Base error for FRED acquisition and ingestion."""


class FredConfigurationError(FredError):
    """FRED credentials or configuration are missing or invalid."""


class FredRequestError(FredError):
    """The FRED endpoint could not be reached or rejected the request."""


class FredResponseError(FredError):
    """The FRED response was invalid or could not be normalized."""


class FredSeriesNotFoundError(FredResponseError):
    """FRED returned no series observations for the requested series."""


@dataclass(frozen=True)
class FredObservation:
    observation_date: date
    raw_value: str
    value: float | None
    realtime_start: str | None = None
    realtime_end: str | None = None


@dataclass(frozen=True)
class FredSeriesResponse:
    series_id: str
    raw_payload: str
    response_metadata: Mapping[str, Any]
    observations: tuple[FredObservation, ...]
    retrieval_time: datetime


@dataclass(frozen=True)
class IngestionResult:
    series_id: str
    inserted: int
    unchanged: int
    missing: int


def parse_observations(payload: str | Mapping[str, Any]) -> tuple[FredObservation, ...]:
    """Validate and normalize the observations array from FRED's JSON response."""
    try:
        document = json.loads(payload) if isinstance(payload, str) else payload
    except (json.JSONDecodeError, TypeError) as exc:
        raise FredResponseError("FRED returned invalid JSON") from exc
    if not isinstance(document, Mapping):
        raise FredResponseError("FRED response must be a JSON object")
    if "error_code" in document:
        message = str(document.get("error_message", "FRED API error"))
        lowered = message.lower()
        if "series_id" in lowered or "series id" in lowered:
            raise FredSeriesNotFoundError(message)
        if "api_key" in lowered:
            raise FredConfigurationError("FRED rejected the configured API key")
        raise FredResponseError(f"FRED API error: {message}")
    raw_items = document.get("observations")
    if not isinstance(raw_items, list):
        raise FredResponseError("FRED response is missing its observations array")
    count = document.get("count")
    if not isinstance(count, int) or count < 0:
        raise FredResponseError("FRED response has an invalid observation count")
    if count == 0:
        raise FredSeriesNotFoundError("FRED returned no observations for the requested series")
    if count != len(raw_items):
        raise FredResponseError(
            f"FRED response is incomplete: count={count}, received={len(raw_items)}"
        )

    result: list[FredObservation] = []
    seen_dates: set[date] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise FredResponseError(f"FRED observation {index} must be an object")
        date_value = item.get("date")
        if not isinstance(date_value, str):
            raise FredResponseError(f"FRED observation {index} has no date string")
        try:
            observation_date = date.fromisoformat(date_value)
        except ValueError as exc:
            raise FredResponseError(
                f"FRED observation {index} has invalid date {date_value!r}"
            ) from exc
        if observation_date in seen_dates:
            raise FredResponseError(f"FRED response contains duplicate date {date_value}")
        seen_dates.add(observation_date)

        source_value = item.get("value")
        if not isinstance(source_value, str):
            raise FredResponseError(f"FRED observation {index} has no value string")
        if source_value == ".":
            normalized_value = None
        else:
            try:
                decimal_value = Decimal(source_value)
            except InvalidOperation as exc:
                raise FredResponseError(
                    f"FRED observation {index} has malformed value {source_value!r}"
                ) from exc
            if not decimal_value.is_finite():
                raise FredResponseError(
                    f"FRED observation {index} has non-finite value {source_value!r}"
                )
            normalized_value = float(decimal_value)
            if not math.isfinite(normalized_value):
                raise FredResponseError(
                    f"FRED observation {index} is outside the supported numeric range"
                )
        realtime_start = item.get("realtime_start")
        realtime_end = item.get("realtime_end")
        if realtime_start is not None and not isinstance(realtime_start, str):
            raise FredResponseError(f"FRED observation {index} has invalid realtime_start")
        if realtime_end is not None and not isinstance(realtime_end, str):
            raise FredResponseError(f"FRED observation {index} has invalid realtime_end")
        result.append(FredObservation(
            observation_date=observation_date,
            raw_value=source_value,
            value=normalized_value,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
        ))
    return tuple(result)


class FredClient:
    """Small FRED API client; credentials are read from FRED_API_KEY by default."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        user_agent: str | None = None,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("FRED_API_KEY")
        if not self.api_key or not self.api_key.strip():
            raise FredConfigurationError(
                "FRED_API_KEY is required; set it in the process environment"
            )
        self.timeout = timeout
        self.user_agent = user_agent or os.getenv(
            "TFR_USER_AGENT", "TreasuryFlowRadar/0.1.0"
        )
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch_series(self, series_id: str) -> FredSeriesResponse:
        if not series_id or not series_id.strip():
            raise ValueError("series_id must not be empty")
        query = urlencode({
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "asc",
            "limit": "100000",
        })
        request = Request(
            f"{API_URL}?{query}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
                retrieved_at = self._clock()
                headers = dict(response.headers.items())
        except HTTPError as exc:
            # Never expose the request URL: it contains the API key.
            raise FredRequestError(f"FRED HTTP request failed with status {exc.code}") from None
        except URLError as exc:
            raise FredRequestError(f"FRED network request failed: {exc.reason}") from None
        except OSError as exc:
            raise FredRequestError(f"FRED network request failed: {exc}") from exc
        if status < 200 or status >= 300:
            raise FredRequestError(f"FRED HTTP request failed with status {status}")
        try:
            raw_payload = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FredResponseError("FRED response is not valid UTF-8") from exc
        observations = parse_observations(raw_payload)
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise FredConfigurationError("retrieval clock must return a timezone-aware datetime")
        metadata: dict[str, Any] = {
            "endpoint": API_URL,
            "request_parameters": {
                "series_id": series_id,
                "file_type": "json",
                "sort_order": "asc",
                "limit": 100000,
            },
            "response_headers": {
                key: value for key, value in headers.items()
                if key.lower() in {"content-type", "etag", "last-modified", "date"}
            },
        }
        return FredSeriesResponse(
            series_id=series_id,
            raw_payload=raw_payload,
            response_metadata=metadata,
            observations=observations,
            retrieval_time=retrieved_at,
        )


def _series_definition(series_id: str) -> Mapping[str, str]:
    try:
        return SUPPORTED_SERIES[series_id]
    except KeyError as exc:
        raise ValueError(
            f"unsupported ingestion series {series_id!r}; choose DGS10 and/or DGS2"
        ) from exc


def ingest_fred(
    series_ids: list[str] | tuple[str, ...],
    *,
    database_path: str | Path = DEFAULT_DB_PATH,
    client: FredClient | None = None,
) -> list[IngestionResult]:
    """Fetch requested FRED series, then atomically persist payloads and observations."""
    if not series_ids:
        raise ValueError("request at least one series: DGS10 and/or DGS2")
    if len(set(series_ids)) != len(series_ids):
        raise ValueError("series identifiers must not be repeated")
    for series_id in series_ids:
        _series_definition(series_id)
    fred = client or FredClient()
    fetched = [fred.fetch_series(series_id) for series_id in series_ids]

    initialize_database(database_path)
    results: list[IngestionResult] = []
    with database(database_path) as conn:
        source_id = register_source(
            conn,
            identifier=SOURCE_IDENTIFIER,
            name=SOURCE_NAME,
            source_type="economic_data",
            url=SOURCE_URL,
            metadata={"provider": "FRED", "api": API_URL},
        )
        for response in fetched:
            definition = _series_definition(response.series_id)
            series_id = register_series(
                conn,
                source_id=source_id,
                identifier=response.series_id,
                name=definition["name"],
                description=definition["description"],
                instrument_type="treasury_yield",
                frequency="daily",
                default_unit=RATE_UNIT,
                metadata={
                    "provider": "FRED",
                    "observation_units": "Percent",
                    "source_series_url": (
                        f"https://fred.stlouisfed.org/series/{response.series_id}"
                    ),
                    "observation_precision": "calendar_date",
                },
            )
            raw_record_id = insert_raw_record(
                conn,
                source_id=source_id,
                external_record_id=response.series_id,
                payload=response.raw_payload,
                content_type=next(
                    (
                        value
                        for key, value in response.response_metadata.get(
                            "response_headers", {}
                        ).items()
                        if key.lower() == "content-type"
                    ),
                    "application/json",
                ),
                retrieval_time=response.retrieval_time,
                metadata=dict(response.response_metadata),
            )
            inserted = unchanged = missing = 0
            for observation in response.observations:
                logical_key = observation.observation_date.isoformat()
                current = get_observations(
                    conn,
                    source_id=source_id,
                    series_id=series_id,
                    logical_key=logical_key,
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

                # FRED returns a calendar date, not a publication timestamp or time of day.
                observation_time = datetime.combine(
                    observation.observation_date, time.min, tzinfo=timezone.utc
                )
                observation_metadata = {
                    "provider": "FRED",
                    "series_id": response.series_id,
                    "observation_date": logical_key,
                    "observation_precision": "calendar_date",
                    "fred_realtime_start": observation.realtime_start,
                    "fred_realtime_end": observation.realtime_end,
                }
                insert_observation(
                    conn,
                    source_id=source_id,
                    series_id=series_id,
                    logical_key=logical_key,
                    revision=revision,
                    revision_of_id=revision_of_id,
                    observation_time=observation_time,
                    publication_time=None,
                    retrieval_time=response.retrieval_time,
                    value_numeric=observation.value,
                    unit=RATE_UNIT,
                    raw_value=observation.raw_value,
                    raw_record_id=raw_record_id,
                    metadata=observation_metadata,
                )
                inserted += 1
                if observation.value is None:
                    missing += 1
            results.append(IngestionResult(
                series_id=response.series_id,
                inserted=inserted,
                unchanged=unchanged,
                missing=missing,
            ))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest FRED Treasury constant maturity yields (DGS10 and DGS2)."
    )
    parser.add_argument(
        "series",
        nargs="*",
        choices=sorted(SUPPORTED_SERIES),
        default=None,
        help="series to ingest; defaults to both DGS10 and DGS2",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("TREASURY_FLOW_RADAR_DB", str(DEFAULT_DB_PATH)),
        help="SQLite database path (default: TREASURY_FLOW_RADAR_DB or project data path)",
    )
    args = parser.parse_args(argv)
    selected = args.series or ["DGS10", "DGS2"]
    try:
        results = ingest_fred(selected, database_path=args.database)
    except FredError as exc:
        parser.exit(2, f"FRED ingestion failed: {exc}\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Ingestion failed: {exc}\n")
    for result in results:
        print(
            f"{result.series_id}: inserted={result.inserted}, "
            f"unchanged={result.unchanged}, missing={result.missing}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

