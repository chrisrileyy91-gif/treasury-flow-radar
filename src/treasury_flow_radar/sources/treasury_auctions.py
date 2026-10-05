"""Treasury Fiscal Data auction facts with raw-page provenance and revisions."""

from __future__ import annotations

import argparse
import json
import math
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

DATASET_ID = "auctions_query"
API_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/od/auctions_query"
)
SOURCE_ID = "U.S. Treasury Fiscal Data"
SOURCE_NAME = "U.S. Department of the Treasury Fiscal Data"
SOURCE_URL = "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/"
DATASET_URL = "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/"
PAGE_SIZE = 1000
SECURITY_TERMS = ("2-Year", "5-Year", "7-Year", "10-Year", "20-Year", "30-Year")
SECURITY_TYPES = {"Note", "Bond"}
FIELDS = (
    "record_date", "cusip", "security_type", "security_term", "auction_date",
    "announcemt_date", "issue_date", "maturity_date", "original_issue_date",
    "original_security_term", "reopening", "inflation_index_security", "floating_rate",
    "offering_amt", "total_accepted", "comp_accepted", "noncomp_accepted",
    "bid_to_cover_ratio", "high_yield", "high_investment_rate", "high_discnt_rate",
    "int_rate",
)
AMOUNT_FIELDS = {
    "offering_amt", "total_accepted", "comp_accepted", "noncomp_accepted"
}
RATE_FIELDS = {
    "bid_to_cover_ratio", "high_yield", "high_investment_rate", "high_discnt_rate", "int_rate"
}
NUMERIC_FIELDS = (
    "offering_amt", "total_accepted", "comp_accepted", "noncomp_accepted",
    "bid_to_cover_ratio", "high_yield", "high_investment_rate", "high_discnt_rate", "int_rate",
)
DATE_FIELDS = (
    "record_date", "announcemt_date", "auction_date", "issue_date", "maturity_date",
    "original_issue_date",
)
MISSING = {"", ".", "*", "-", "NA", "N/A", "NULL", "NONE"}


class TreasuryAuctionError(RuntimeError):
    """Base error for Treasury auction retrieval and normalization."""


class TreasuryAuctionRequestError(TreasuryAuctionError):
    """The official Treasury API request failed."""


class TreasuryAuctionResponseError(TreasuryAuctionError):
    """The Treasury API response was invalid or internally inconsistent."""


@dataclass(frozen=True)
class Auction:
    """One source-native Fiscal Data auction row."""

    source_id: str
    cusip: str
    security_type: str
    security_term: str
    announcement_date: date | None
    auction_date: date
    issue_date: date | None
    maturity_date: date | None
    record_date: date | None
    original_issue_date: date | None
    original_security_term: str | None
    reopening: str | None
    values: tuple[tuple[str, str | None, float | None], ...]
    source_fields: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class Page:
    number: int
    payload: str
    auctions: tuple[Auction, ...]
    retrieved: datetime
    metadata: dict[str, Any]


def _calendar_date(value: Any, field: str, *, required: bool = False) -> date | None:
    if value is None or (isinstance(value, str) and value.strip().upper() in MISSING):
        if required:
            raise TreasuryAuctionResponseError(f"missing required date {field}")
        return None
    if not isinstance(value, str):
        raise TreasuryAuctionResponseError(f"date field {field} must be a string or null")
    # These API fields are DATE values. Retain their written calendar date and never
    # convert an offset-aware timestamp through UTC (which could shift that date).
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:$|T| )", value.strip())
    if not match:
        raise TreasuryAuctionResponseError(f"invalid calendar date in {field}: {value!r}")
    try:
        return date.fromisoformat(match.group(1))
    except ValueError as exc:
        raise TreasuryAuctionResponseError(f"invalid calendar date in {field}: {value!r}") from exc


def _text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise TreasuryAuctionResponseError(f"missing required field {field}")
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TreasuryAuctionResponseError(f"malformed text field {field}")
    result = str(value).strip()
    if not result or result.upper() in MISSING:
        if required:
            raise TreasuryAuctionResponseError(f"missing required field {field}")
        return None
    return result


def _number(value: Any, field: str) -> tuple[str | None, float | None]:
    if value is None:
        return None, None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TreasuryAuctionResponseError(f"malformed numeric field {field}")
    raw = str(value).strip()
    if raw.upper() in MISSING:
        return raw, None
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise TreasuryAuctionResponseError(f"invalid numeric field {field}: {raw!r}") from exc
    if not number.is_finite():
        raise TreasuryAuctionResponseError(f"non-finite numeric field {field}: {raw!r}")
    normalized = float(number)
    if not math.isfinite(normalized):
        raise TreasuryAuctionResponseError(f"numeric field outside supported range {field}: {raw!r}")
    return raw, normalized


def _auction(row: Mapping[str, Any], index: int) -> Auction | None:
    security_type = _text(row.get("security_type"), "security_type", required=True)
    term = _text(row.get("security_term"), "security_term", required=True)
    # This implementation targets nominal Notes and Bonds at the specified terms.
    # Preserve source labels and explicitly exclude TIPS and FRNs.
    if security_type not in SECURITY_TYPES or term not in SECURITY_TERMS:
        return None
    if _text(row.get("inflation_index_security"), "inflation_index_security") == "Yes":
        return None
    if _text(row.get("floating_rate"), "floating_rate") == "Yes":
        return None
    cusip = _text(row.get("cusip"), "cusip", required=True)
    auction_date = _calendar_date(row.get("auction_date"), "auction_date", required=True)
    parsed_dates = {
        key: _calendar_date(row.get(key), key) for key in DATE_FIELDS if key != "auction_date"
    }
    source_id = "|".join((cusip, auction_date.isoformat(),
                          parsed_dates["issue_date"].isoformat() if parsed_dates["issue_date"] else ""))
    values: list[tuple[str, str | None, float | None]] = []
    for field in NUMERIC_FIELDS:
        raw, number = _number(row.get(field), field)
        # Keep fields omitted by a record distinct in source_fields; raw_value=NULL
        # for an explicit JSON null is preserved the same way by the database.
        values.append((field, raw, number))
    return Auction(
        source_id=source_id,
        cusip=cusip,
        security_type=security_type,
        security_term=term,
        announcement_date=parsed_dates["announcemt_date"],
        auction_date=auction_date,
        issue_date=parsed_dates["issue_date"],
        maturity_date=parsed_dates["maturity_date"],
        record_date=parsed_dates["record_date"],
        original_issue_date=parsed_dates["original_issue_date"],
        original_security_term=_text(row.get("original_security_term"), "original_security_term"),
        reopening=_text(row.get("reopening"), "reopening"),
        values=tuple(values),
        source_fields=tuple((field, row.get(field)) for field in FIELDS if field in row),
    )


def parse_response(payload: str | Mapping[str, Any]) -> tuple[tuple[Auction, ...], int, int]:
    """Validate the Fiscal Data envelope and parse selected nominal coupon auctions."""
    try:
        body = json.loads(payload) if isinstance(payload, str) else payload
    except (json.JSONDecodeError, TypeError) as exc:
        raise TreasuryAuctionResponseError("invalid Treasury JSON response") from exc
    if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
        raise TreasuryAuctionResponseError("response must contain a data array")
    meta = body.get("meta")
    if not isinstance(meta, Mapping):
        raise TreasuryAuctionResponseError("response must contain pagination metadata")
    try:
        total_count, total_pages = int(meta["total-count"]), int(meta["total-pages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TreasuryAuctionResponseError("invalid total-count/total-pages metadata") from exc
    if total_count < 0 or total_pages < 0:
        raise TreasuryAuctionResponseError("pagination totals cannot be negative")
    result = []
    seen: dict[str, Auction] = {}
    for index, row in enumerate(body["data"]):
        if not isinstance(row, Mapping):
            raise TreasuryAuctionResponseError(f"row {index} must be an object")
        parsed = _auction(row, index)
        if parsed is not None:
            previous = seen.get(parsed.source_id)
            if previous is not None:
                if _fingerprint(previous) != _fingerprint(parsed):
                    raise TreasuryAuctionResponseError(
                        f"conflicting duplicate auction in page: {parsed.source_id}"
                    )
                continue
            seen[parsed.source_id] = parsed
            result.append(parsed)
    return tuple(result), total_count, total_pages


def _fingerprint(auction: Auction) -> tuple[Any, ...]:
    return (auction,)


class TreasuryAuctionClient:
    """Injectable read-only client for the public Treasury Fiscal Data API."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_size: int = PAGE_SIZE,
        user_agent: str | None = None,
    ) -> None:
        if not 1 <= page_size <= 10000:
            raise ValueError("page_size must be between 1 and 10000")
        self.opener, self.clock, self.page_size = opener, clock, page_size
        self.user_agent = user_agent or "TreasuryFlowRadar/0.1 (public Treasury Fiscal Data)"

    def fetch_history(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[Page, ...]:
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        pages: list[Page] = []
        seen: dict[str, Auction] = {}
        expected_count = expected_pages = None
        page_number = 1
        filters = [( 
            "security_type:in:(Note,Bond),"
            "security_term:in:(2-Year,5-Year,7-Year,10-Year,20-Year,30-Year),"
            "inflation_index_security:eq:No,floating_rate:eq:No"
        )]
        if start_date:
            filters.append(f"auction_date:gte:{start_date.isoformat()}")
        if end_date:
            filters.append(f"auction_date:lte:{end_date.isoformat()}")
        filter_expression = ",".join(filters)
        while expected_pages is None or page_number <= expected_pages:
            params = urlencode({
                "format": "json", "fields": ",".join(FIELDS), "filter": filter_expression,
                "sort": "auction_date,cusip,issue_date", "page[number]": str(page_number),
                "page[size]": str(self.page_size),
            })
            request = Request(f"{API_URL}?{params}", headers={
                "Accept": "application/json", "User-Agent": self.user_agent,
            })
            try:
                with self.opener(request, timeout=45) as response:
                    status, body = getattr(response, "status", 200), response.read()
                    headers = dict(response.headers.items())
            except HTTPError as exc:
                raise TreasuryAuctionRequestError(f"Treasury API returned HTTP {exc.code}") from None
            except (URLError, OSError, TimeoutError) as exc:
                raise TreasuryAuctionRequestError(f"Treasury API request failed: {exc}") from exc
            if status < 200 or status >= 300:
                raise TreasuryAuctionRequestError(f"Treasury API returned HTTP {status}")
            try:
                raw = body.decode("utf-8")
                envelope = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TreasuryAuctionResponseError("response is not valid UTF-8 JSON") from exc
            auctions, total_count, total_pages = parse_response(envelope)
            if expected_count is None:
                expected_count, expected_pages = total_count, total_pages
                if total_pages > 0 and total_count == 0:
                    raise TreasuryAuctionResponseError("inconsistent zero count and positive page count")
                if total_pages == 0 and total_count != 0:
                    raise TreasuryAuctionResponseError("inconsistent positive count and zero page count")
            elif (total_count, total_pages) != (expected_count, expected_pages):
                raise TreasuryAuctionResponseError("pagination totals changed during retrieval")
            if len(envelope["data"]) > self.page_size:
                raise TreasuryAuctionResponseError("Treasury API returned more rows than requested")
            page_auctions = []
            for auction in auctions:
                previous = seen.get(auction.source_id)
                if previous is not None:
                    if _fingerprint(previous) != _fingerprint(auction):
                        raise TreasuryAuctionResponseError(
                            f"conflicting duplicate auction across pages: {auction.source_id}"
                        )
                    continue
                seen[auction.source_id] = auction
                page_auctions.append(auction)
            retrieved = self.clock()
            if retrieved.tzinfo is None or retrieved.utcoffset() is None:
                raise ValueError("retrieval clock must be timezone-aware")
            pages.append(Page(page_number, raw, tuple(page_auctions), retrieved, {
                "dataset_id": DATASET_ID,
                "dataset_url": DATASET_URL,
                "endpoint": API_URL,
                "request_parameters": {
                    "fields": list(FIELDS), "filter": filter_expression, "sort": "auction_date,cusip,issue_date",
                    "page_size": self.page_size, "page_number": page_number,
                    "start_date": start_date.isoformat() if start_date else None,
                    "end_date": end_date.isoformat() if end_date else None,
                },
                "total_count": total_count, "total_pages": total_pages, "http_status": status,
                "response_headers": {k: v for k, v in headers.items()
                                      if k.lower() in {"content-type", "etag", "last-modified", "date"}},
            }))
            page_number += 1
        if expected_count and len(seen) != expected_count:
            raise TreasuryAuctionResponseError(
                f"pagination returned {len(seen)} unique auctions; expected {expected_count}"
            )
        if not any(page.auctions for page in pages):
            raise TreasuryAuctionResponseError("Treasury API returned no selected auction rows")
        return tuple(pages)


def _series(auction: Auction) -> tuple[str, str]:
    safe_type = auction.security_type.lower()
    safe_term = auction.security_term.lower().replace("-", "_")
    return f"treasury_auction_{safe_type}_{safe_term}", f"{auction.security_term} Treasury {auction.security_type} auctions"


def ingest_treasury_auctions(
    *, database_path: str | Path = DEFAULT_DB_PATH,
    client: TreasuryAuctionClient | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, int]:
    """Fetch and validate every page before transactional persistence."""
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    auction_client = client or TreasuryAuctionClient()
    pages = (
        auction_client.fetch_history()
        if start_date is None and end_date is None
        else auction_client.fetch_history(start_date=start_date, end_date=end_date)
    )
    initialize_database(database_path)
    inserted = unchanged = missing = 0
    with database(database_path) as conn:
        source_id = register_source(
            conn, identifier=SOURCE_ID, name=SOURCE_NAME, source_type="official_government_data",
            url=SOURCE_URL, metadata={
                "provider": "U.S. Department of the Treasury",
                "dataset": "Treasury Securities Auctions Data",
                "dataset_id": DATASET_ID, "endpoint": API_URL,
                "date_field_semantics": {"record_date": "source publication calendar date; not a timestamp",
                                         "auction_date": "date auction was held",
                                         "issue_date": "issue/settlement calendar date"},
                "publication_timestamp": "not supplied by dataset; stored as NULL",
                "amount_unit": "source-native thousands of U.S. dollars",
                "supported_types": sorted(SECURITY_TYPES),
                "supported_terms": list(SECURITY_TERMS),
            },
        )
        for page in pages:
            raw_id = insert_raw_record(
                conn, source_id=source_id,
                external_record_id=f"{DATASET_ID}:page={page.number}", payload=page.payload,
                content_type="application/json", retrieval_time=page.retrieved,
                metadata=page.metadata,
            )
            for auction in page.auctions:
                series_key, series_name = _series(auction)
                series_id = register_series(
                    conn, source_id=source_id, identifier=series_key, name=series_name,
                    description="Official Fiscal Data auction rows; security type/term follow source values.",
                    instrument_type=f"treasury_{auction.security_type.lower()}", frequency="auction",
                    default_unit=None, metadata={"security_type": auction.security_type,
                                                 "security_term": auction.security_term,
                                                 "dataset_id": DATASET_ID},
                )
                obs_time = datetime.combine(auction.auction_date, time.min, tzinfo=UTC)
                dates = {
                    "announcement_date": auction.announcement_date.isoformat() if auction.announcement_date else None,
                    "auction_date": auction.auction_date.isoformat(),
                    "issue_date": auction.issue_date.isoformat() if auction.issue_date else None,
                    "maturity_date": auction.maturity_date.isoformat() if auction.maturity_date else None,
                    "record_date": auction.record_date.isoformat() if auction.record_date else None,
                    "original_issue_date": auction.original_issue_date.isoformat() if auction.original_issue_date else None,
                }
                for field, raw_value, number in auction.values:
                    logical_key = f"{auction.source_id}|{field}"
                    current = get_observations(conn, source_id=source_id, series_id=series_id,
                                               logical_key=logical_key)
                    if current:
                        latest = max(current, key=lambda row: row["revision"])
                        if latest["raw_value"] == raw_value and latest["value_numeric"] == number:
                            unchanged += 1
                            missing += number is None
                            continue
                        revision, predecessor = latest["revision"] + 1, latest["id"]
                    else:
                        revision, predecessor = 1, None
                    unit = (
                        "thousand_us_dollars" if field in AMOUNT_FIELDS
                        else "ratio" if field == "bid_to_cover_ratio"
                        else "percent"
                    )
                    insert_observation(
                        conn, source_id=source_id, series_id=series_id, logical_key=logical_key,
                        revision=revision, revision_of_id=predecessor, observation_time=obs_time,
                        publication_time=None, retrieval_time=page.retrieved,
                        value_numeric=number, value_text=None if field in AMOUNT_FIELDS | RATE_FIELDS else raw_value,
                        unit=unit, raw_value=raw_value, raw_record_id=raw_id,
                        metadata={
                            "source_identifier": auction.source_id, "source_field": field,
                            "source_record_fields": dict(auction.source_fields),
                            "security_type": auction.security_type, "security_term": auction.security_term,
                            "cusip": auction.cusip, "dates": dates,
                            "reopening": auction.reopening,
                            "new_issue_or_reopening": (
                                "reopening" if auction.reopening == "Yes"
                                else "new_issue" if auction.reopening == "No" else None
                            ),
                            "original_security_term": auction.original_security_term,
                            "observation_precision": "calendar_date",
                            "source_dataset_id": DATASET_ID,
                        },
                    )
                    inserted += 1
                    missing += number is None
    return {"auctions": sum(len(p.auctions) for p in pages), "inserted": inserted,
            "unchanged": unchanged, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Treasury Fiscal Data auction results.")
    parser.add_argument("--database", default=os.getenv("TREASURY_FLOW_RADAR_DB", str(DEFAULT_DB_PATH)))
    args = parser.parse_args(argv)
    try:
        result = ingest_treasury_auctions(database_path=args.database)
    except TreasuryAuctionError as exc:
        parser.exit(2, f"Treasury auction ingestion failed: {exc}\n")
    print("Treasury auctions: " + ", ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

