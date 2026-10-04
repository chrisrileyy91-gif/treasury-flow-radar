"""CFTC TFF futures-only Treasury positioning facts and provenance."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
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

DATASET_ID = "gpe5-46if"
API_URL = f"https://publicreporting.cftc.gov/resource/{DATASET_ID}.json"
SOURCE_ID = "CFTC"
SOURCE_NAME = "U.S. Commodity Futures Trading Commission Commitments of Traders"
SOURCE_URL = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"
PRE_URL = f"https://publicreporting.cftc.gov/d/{DATASET_ID}"
DATASET_TITLE = "Traders in Financial Futures (TFF) - Futures Only"
REPORT_VARIANT = "tff_futures_only"
PAGE_SIZE = 5000
UNIT = "contracts"

CONTRACTS = {
    "042601": ("ust_2_year_note_042601", "2-Year U.S. Treasury Note Futures"),
    "044601": ("ust_5_year_note_044601", "5-Year U.S. Treasury Note Futures"),
    "043602": ("ust_10_year_note_043602", "10-Year U.S. Treasury Note Futures"),
    "043607": ("ust_ultra_10_year_note_043607", "Ultra 10-Year U.S. Treasury Note Futures"),
    "020601": ("ust_30_year_bond_020601", "U.S. Treasury Bond Futures (30-Year)"),
}
CATEGORIES = {
    "dealer_intermediary": (
        "Dealer/Intermediary",
        {
            "long": "dealer_positions_long_all",
            "short": "dealer_positions_short_all",
            "spreading": "dealer_positions_spread_all",
        },
    ),
    "asset_manager_institutional": (
        "Asset Manager/Institutional",
        {
            "long": "asset_mgr_positions_long",
            "short": "asset_mgr_positions_short",
            "spreading": "asset_mgr_positions_spread",
        },
    ),
    "leveraged_funds": (
        "Leveraged Funds",
        {
            "long": "lev_money_positions_long",
            "short": "lev_money_positions_short",
            "spreading": "lev_money_positions_spread",
        },
    ),
    "other_reportables": (
        "Other Reportables",
        {
            "long": "other_rept_positions_long",
            "short": "other_rept_positions_short",
            "spreading": "other_rept_positions_spread",
        },
    ),
    "nonreportable": (
        "Nonreportable",
        {"long": "nonrept_positions_long_all", "short": "nonrept_positions_short_all"},
    ),
}
CODE = "cftc_contract_market_code"
REPORT_DATE = "report_date_as_yyyy_mm_dd"
MARKET_NAME = "market_and_exchange_names"
CONTRACT_NAME = "contract_market_name"
POSITION_FIELDS = tuple(
    dict.fromkeys(field for _, fields in CATEGORIES.values() for field in fields.values())
)
MISSING = {"", ".", "*", "-", "NA", "N/A", "NULL", "NONE"}


class CftcError(RuntimeError):
    """Base CFTC adapter error."""


class CftcRequestError(CftcError):
    """CFTC public API request failed."""


class CftcResponseError(CftcError):
    """CFTC source response is malformed."""


@dataclass(frozen=True)
class Measure:
    participant: str
    metric: str
    source_field: str
    value: int | None
    raw_value: str


@dataclass(frozen=True)
class Record:
    report_date: date
    code: str
    market_name: str
    contract_name: str | None
    as_of_date: str | None
    metrics: tuple[Measure, ...]


@dataclass(frozen=True)
class Page:
    offset: int
    payload: str
    records: tuple[Record, ...]
    retrieved: datetime
    metadata: dict[str, Any]


def _date(value: Any) -> date:
    if not isinstance(value, str):
        raise CftcResponseError("report date must be a string")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        # The CFTC field is a floating timestamp. Keep its source calendar date;
        # converting an offset-aware timestamp to UTC could shift the report day.
        parsed = datetime.fromisoformat(value)
        return parsed.date()
    except ValueError as exc:
        raise CftcResponseError(f"invalid report date {value!r}") from exc


def _count(value: Any, field: str) -> tuple[int | None, str]:
    if value is None:
        return None, ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CftcResponseError(f"malformed integer field {field}: {value!r}")
    raw = str(value).strip()
    if raw.upper() in MISSING:
        return None, raw
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise CftcResponseError(f"malformed integer field {field}: {raw!r}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise CftcResponseError(f"non-integer field {field}: {raw!r}")
    return int(number), raw


def parse_rows(payload: str | list[Mapping[str, Any]]) -> tuple[Record, ...]:
    """Parse official TFF Futures Only Socrata JSON rows."""
    try:
        rows = json.loads(payload) if isinstance(payload, str) else payload
    except (json.JSONDecodeError, TypeError) as exc:
        raise CftcResponseError("invalid JSON response") from exc
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise CftcResponseError("response must be an array of row objects")
    result, seen = [], {}
    for index, row in enumerate(rows):
        code = row.get(CODE)
        if not isinstance(code, str) or not code.strip():
            raise CftcResponseError(f"row {index} is missing contract market code")
        code = code.strip()
        if code not in CONTRACTS:
            continue
        name = row.get(MARKET_NAME)
        if not isinstance(name, str) or not name.strip():
            raise CftcResponseError(f"row {index} is missing market name")
        contract_name = row.get(CONTRACT_NAME)
        if contract_name is not None and not isinstance(contract_name, str):
            raise CftcResponseError(f"row {index} has malformed contract market name")
        if CONTRACT_NAME not in row:
            raise CftcResponseError(f"row {index} missing source field {CONTRACT_NAME}")
        report_date = _date(row.get(REPORT_DATE))
        asof = row.get("as_of_date_in_form_yy_mm_dd")
        if asof is not None and not isinstance(asof, str):
            raise CftcResponseError("invalid as-of date in form")
        fields = [("all_participants", "open_interest", "open_interest_all")]
        fields += [
            (category, metric, field)
            for category, (_, mappings) in CATEGORIES.items()
            for metric, field in mappings.items()
        ]
        measures = []
        for category, metric, field in fields:
            if field not in row:
                raise CftcResponseError(f"row {index} missing source field {field}")
            value, raw = _count(row[field], field)
            measures.append(Measure(category, metric, field, value, raw))
        record = Record(report_date, code, name.strip(), contract_name, asof, tuple(measures))
        identity = (code, report_date)
        old = seen.get(identity)
        if old is not None:
            if old != record:
                raise CftcResponseError(f"conflicting duplicate {code} on {report_date}")
            continue
        seen[identity] = record
        result.append(record)
    return tuple(result)


class CftcClient:
    """Unauthenticated injectable client for CFTC's public Socrata API."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_size: int = PAGE_SIZE,
        user_agent: str | None = None,
    ) -> None:
        if not 1 <= page_size <= PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {PAGE_SIZE}")
        self.opener, self.clock, self.page_size = opener, clock, page_size
        self.user_agent = user_agent or os.getenv(
            "TFR_USER_AGENT", "TreasuryFlowRadar/0.1 (public CFTC data)"
        )

    def fetch_history(self) -> tuple[Page, ...]:
        pages, offset = [], 0
        seen: dict[tuple[str, date], Record] = {}
        while True:
            codes = ", ".join(f"'{code}'" for code in sorted(CONTRACTS))
            fields = [
                "id",
                MARKET_NAME,
                CONTRACT_NAME,
                "as_of_date_in_form_yy_mm_dd",
                REPORT_DATE,
                CODE,
                "open_interest_all",
                *POSITION_FIELDS,
            ]
            params = urlencode(
                {
                    "$select": ",".join(dict.fromkeys(fields)),
                    "$where": f"{CODE} in ({codes})",
                    "$order": f"{REPORT_DATE} ASC, {CODE} ASC, id ASC",
                    "$limit": str(self.page_size),
                    "$offset": str(offset),
                }
            )
            url = f"{API_URL}?{params}"
            request = Request(
                url, headers={"Accept": "application/json", "User-Agent": self.user_agent}
            )
            try:
                with self.opener(request, timeout=45) as response:
                    status, body = getattr(response, "status", 200), response.read()
                    headers = dict(response.headers.items())
            except HTTPError as exc:
                raise CftcRequestError(f"CFTC API returned HTTP {exc.code}") from None
            except (URLError, OSError, TimeoutError) as exc:
                raise CftcRequestError(f"CFTC API request failed: {exc}") from exc
            if status < 200 or status >= 300:
                raise CftcRequestError(f"CFTC API returned HTTP {status}")
            try:
                raw = body.decode("utf-8")
                rows = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CftcResponseError("response is not valid UTF-8 JSON") from exc
            if not isinstance(rows, list) or len(rows) > self.page_size:
                raise CftcResponseError("response page is not a valid bounded row array")
            page_records = parse_rows(rows)
            records = []
            for record in page_records:
                identity = (record.code, record.report_date)
                previous = seen.get(identity)
                if previous is not None:
                    if previous != record:
                        raise CftcResponseError(
                            "conflicting duplicate across pages "
                            f"{record.code} on {record.report_date}"
                        )
                    continue
                seen[identity] = record
                records.append(record)
            retrieved = self.clock()
            if retrieved.tzinfo is None or retrieved.utcoffset() is None:
                raise ValueError("retrieval clock must be timezone-aware")
            pages.append(
                Page(
                    offset,
                    raw,
                    records,
                    retrieved,
                    {
                        "dataset_id": DATASET_ID,
                        "endpoint": API_URL,
                        "request_parameters": {
                            "contract_codes": sorted(CONTRACTS),
                            "limit": self.page_size,
                            "offset": offset,
                        },
                        "http_status": status,
                        "response_headers": {
                            k: v
                            for k, v in headers.items()
                            if k.lower() in {"content-type", "etag", "last-modified", "date"}
                        },
                    },
                )
            )
            if len(rows) < self.page_size:
                break
            offset += len(rows)
        if not any(page.records for page in pages):
            raise CftcResponseError("no selected Treasury contract rows returned")
        return tuple(pages)


def _series(code: str) -> str:
    return f"tff_futures_only_{CONTRACTS[code][0]}"


def _key(record: Record, measure: Measure) -> str:
    return f"{record.report_date.isoformat()}|{measure.participant}|{measure.metric}"


def ingest_cftc(
    *, database_path: str | Path = DEFAULT_DB_PATH, client: CftcClient | None = None
) -> dict[str, int]:
    """Fetch history before opening SQLite; persist revisions and raw provenance."""
    pages = (client or CftcClient()).fetch_history()
    initialize_database(database_path)
    inserted = unchanged = missing = 0
    with database(database_path) as conn:
        source_id = register_source(
            conn,
            identifier=SOURCE_ID,
            name=SOURCE_NAME,
            source_type="regulatory_market_data",
            url=SOURCE_URL,
            metadata={
                "provider": "CFTC",
                "report": "TFF futures only",
                "dataset_title": DATASET_TITLE,
                "dataset_id": DATASET_ID,
                "report_variant": REPORT_VARIANT,
                "options_scope": "futures only; excludes options positions",
                "dataset_url": PRE_URL,
                "api_url": API_URL,
                "authentication": "public; no key required",
                "unit": UNIT,
            },
        )
        for page in pages:
            raw_id = insert_raw_record(
                conn,
                source_id=source_id,
                external_record_id=f"{DATASET_ID}:offset={page.offset}",
                payload=page.payload,
                content_type="application/json",
                retrieval_time=page.retrieved,
                metadata=page.metadata,
            )
            for record in page.records:
                contract_id, contract_name = CONTRACTS[record.code]
                series_id = register_series(
                    conn,
                    source_id=source_id,
                    identifier=f"tff_futures_only_{contract_id}",
                    name=contract_name,
                    description=f"CFTC contract market code {record.code}, futures only.",
                    instrument_type="treasury_future",
                    frequency="weekly",
                    default_unit=UNIT,
                    metadata={
                        "dataset_id": DATASET_ID,
                        "dataset_title": DATASET_TITLE,
                        "report_variant": REPORT_VARIANT,
                        "cftc_contract_market_code": record.code,
                        "official_market_and_exchange_name": record.market_name,
                        "official_contract_market_name": record.contract_name,
                        "report_type": "TFF futures only",
                    },
                )
                obs_time = datetime.combine(record.report_date, time.min, tzinfo=UTC)
                for measure in record.metrics:
                    key = _key(record, measure)
                    current = get_observations(
                        conn, source_id=source_id, series_id=series_id, logical_key=key
                    )
                    if current:
                        latest = max(current, key=lambda row: row["revision"])
                        if (
                            latest["raw_value"] == measure.raw_value
                            and latest["value_numeric"] == measure.value
                        ):
                            unchanged += 1
                            missing += measure.value is None
                            continue
                        revision, predecessor = latest["revision"] + 1, latest["id"]
                    else:
                        revision, predecessor = 1, None
                    insert_observation(
                        conn,
                        source_id=source_id,
                        series_id=series_id,
                        logical_key=key,
                        revision=revision,
                        revision_of_id=predecessor,
                        observation_time=obs_time,
                        publication_time=None,
                        retrieval_time=page.retrieved,
                        value_numeric=measure.value,
                        unit=UNIT,
                        raw_value=measure.raw_value,
                        raw_record_id=raw_id,
                        metadata={
                            "contract_market_code": record.code,
                            "market_and_exchange_names": record.market_name,
                            "contract_market_name": record.contract_name,
                            "report_date": record.report_date.isoformat(),
                            "as_of_date_in_form": record.as_of_date,
                            "participant_category": measure.participant,
                            "participant_label": CATEGORIES.get(
                                measure.participant, ("All participants", {})
                            )[0],
                            "metric": measure.metric,
                            "source_field": measure.source_field,
                            "source_dataset_id": DATASET_ID,
                            "report_variant": REPORT_VARIANT,
                            "observation_precision": "calendar_date",
                        },
                    )
                    inserted += 1
                    missing += measure.value is None
    return {
        "contracts": len(CONTRACTS),
        "inserted": inserted,
        "unchanged": unchanged,
        "missing": missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest CFTC Treasury futures positioning.")
    parser.add_argument(
        "--database", default=os.getenv("TREASURY_FLOW_RADAR_DB", str(DEFAULT_DB_PATH))
    )
    args = parser.parse_args(argv)
    try:
        result = ingest_cftc(database_path=args.database)
    except CftcError as exc:
        parser.exit(2, f"CFTC ingestion failed: {exc}\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Ingestion failed: {exc}\n")
    print("TFF futures only: " + ", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

