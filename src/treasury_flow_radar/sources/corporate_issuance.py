"""Provider-neutral corporate bond issuance normalization and persistence.

No public event-level feed is wired here. Providers supply paginated JSON records
using the documented adapter contract; raw provider fields and page payloads stay
available for audit and reconstruction.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from treasury_flow_radar.database import (
    DEFAULT_DB_PATH,
    database,
    initialize_database,
    insert_observation,
    insert_raw_record,
    register_series,
    register_source,
)

SOURCE_ID = "corporate_issuance_provider_neutral"
SERIES_ID = "corporate_issuance_tranche"
DATE_FIELDS = {"announcement_date", "maturity_date", "pricing_date", "settlement_date"}
TEXT_FIELDS = {
    "issuer_name",
    "issuer_identifier",
    "security_identifier",
    "deal_identifier",
    "tranche_identifier",
    "currency",
    "benchmark_maturity",
    "rating",
    "credit_classification",
    "sector",
    "callable_flag",
    "issuance_type",
    "source_native_security_type",
    "source_native_term",
}
NUMERIC_FIELDS = {"principal_amount", "coupon", "yield", "spread", "benchmark_yield", "credit_spread"}
NORMALIZED_FIELDS = TEXT_FIELDS | NUMERIC_FIELDS | DATE_FIELDS
MISSING = {"", "NA", "N/A", "NULL", "NONE"}


class CorporateIssuanceError(ValueError):
    """Base error for malformed or conflicting corporate issuance data."""


@dataclass(frozen=True)
class IssuanceTranche:
    """One normalized security tranche; sibling tranches retain their own identity."""

    source_record_id: str
    fields: dict[str, Any]
    source_native_fields: dict[str, Any]
    event_date: date
    publication_time: datetime | None
    duration_years: float | None
    duration_kind: str | None
    duration_method: str | None


def _optional_text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise CorporateIssuanceError(f"missing required field {field}")
        return None
    if isinstance(value, bool) and field == "callable_flag":
        value = "true" if value else "false"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CorporateIssuanceError(f"invalid text field {field}")
    text = str(value).strip()
    if text.upper() in MISSING:
        if required:
            raise CorporateIssuanceError(f"missing required field {field}")
        return None
    return text


def _date(value: Any, field: str, *, required: bool = False) -> date | None:
    if value is None or (isinstance(value, str) and value.strip().upper() in MISSING):
        if required:
            raise CorporateIssuanceError(f"missing required date {field}")
        return None
    if not isinstance(value, str):
        raise CorporateIssuanceError(f"date field {field} must be a string or null")
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError as exc:
        raise CorporateIssuanceError(f"invalid date in {field}: {value!r}") from exc


def _number(value: Any, field: str) -> tuple[str | None, float | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CorporateIssuanceError(f"invalid numeric field {field}")
    raw = str(value).strip()
    if raw.upper() in MISSING:
        return raw, None
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise CorporateIssuanceError(f"invalid numeric field {field}: {raw!r}") from exc
    if not number.is_finite() or not math.isfinite(float(number)):
        raise CorporateIssuanceError(f"non-finite or out-of-range numeric field {field}")
    if field == "principal_amount" and number < 0:
        raise CorporateIssuanceError("principal_amount cannot be negative")
    return raw, float(number)


def _publication(value: Any) -> datetime | None:
    if value is None or (isinstance(value, str) and value.strip().upper() in MISSING):
        return None
    if not isinstance(value, str):
        raise CorporateIssuanceError("publication_time must be an ISO-8601 timestamp or null")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorporateIssuanceError("invalid ISO-8601 publication_time") from exc
    if result.utcoffset() is None:
        raise CorporateIssuanceError("publication_time must include a timezone")
    return result.astimezone(UTC)


def normalize_record(record: Mapping[str, Any]) -> IssuanceTranche:
    """Normalize one provider-independent tranche record without discarding its terms."""
    if not isinstance(record, Mapping):
        raise CorporateIssuanceError("each issuance record must be a JSON object")
    record_id = _optional_text(record.get("source_record_id"), "source_record_id", required=True)
    issuer = _optional_text(record.get("issuer_name"), "issuer_name", required=True)
    fields: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        val = _optional_text(record.get(field), field, required=(field == "issuer_name"))
        if field == "currency" and val is not None:
            val = val.upper()
            if not re.fullmatch(r"[A-Z]{3}", val):
                raise CorporateIssuanceError("currency must be a three-letter source currency code")
        if field == "callable_flag" and val is not None:
            folded = val.casefold()
            if folded in {"true", "yes", "y", "1"}:
                val = "true"
            elif folded in {"false", "no", "n", "0"}:
                val = "false"
            else:
                raise CorporateIssuanceError("callable_flag must be boolean-like or null")
        if field == "credit_classification" and val is not None:
            folded = val.casefold().replace("-", "_").replace(" ", "_")
            if folded in {"ig", "investment_grade"}:
                val = "investment_grade"
            elif folded in {"hy", "high_yield"}:
                val = "high_yield"
            else:
                raise CorporateIssuanceError(
                    "credit_classification must be investment_grade, high_yield, or null"
                )
        fields[field] = val
    for field in DATE_FIELDS:
        fields[field] = _date(record.get(field), field)
    for field in NUMERIC_FIELDS:
        _, fields[field] = _number(record.get(field), field)
    fields["issuer_name"] = issuer
    # A source record must be independently identifiable and anchored to an event date.
    # Prefer pricing date, then settlement/issue date; never synthesize a deal or tranche ID.
    event_date = fields["pricing_date"] or fields["settlement_date"]
    if event_date is None:
        raise CorporateIssuanceError(
            "pricing_date or settlement_date is required as the event date"
        )
    maturity = fields["maturity_date"]
    duration = None
    duration_kind = duration_method = None
    if maturity is not None and maturity >= event_date:
        duration = (maturity - event_date).days / 365.25
        duration_kind, duration_method = "estimate", "maturity_years_proxy_act_365_25"
    native = record.get("source_native_fields")
    if native is None:
        native = {
            key: val
            for key, val in record.items()
            if key
            not in NORMALIZED_FIELDS
            | {"source_record_id", "publication_time", "source_native_fields"}
        }
    if not isinstance(native, Mapping):
        raise CorporateIssuanceError("source_native_fields must be a JSON object")
    # JSON round trip rejects non-serializable values before any database writes.
    native_copy = json.loads(json.dumps(dict(native), sort_keys=True, allow_nan=False))
    return IssuanceTranche(
        source_record_id=record_id,
        fields=fields,
        source_native_fields=native_copy,
        event_date=event_date,
        publication_time=_publication(record.get("publication_time")),
        duration_years=duration,
        duration_kind=duration_kind,
        duration_method=duration_method,
    )


def _canonical(record: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise CorporateIssuanceError("source page contains a non-JSON value") from exc


def _parse_page(
    page: str | Mapping[str, Any], number: int
) -> tuple[str, list[Mapping[str, Any]], int, int, datetime]:
    payload = page if isinstance(page, str) else _canonical(page)
    try:
        body = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CorporateIssuanceError(f"page {number} is not valid JSON") from exc
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("data"), list)
        or not isinstance(body.get("meta"), dict)
    ):
        raise CorporateIssuanceError(f"page {number} must contain data[] and meta")
    rows = body["data"]
    meta = body["meta"]
    try:
        total_count, total_pages = int(meta["total-count"]), int(meta["total-pages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorporateIssuanceError(
            f"page {number} has invalid total-count/total-pages metadata"
        ) from exc
    if total_count < 0 or total_pages < 1:
        raise CorporateIssuanceError("pagination totals must be nonnegative with at least one page")
    retrieved_raw = meta.get("retrieved_at")
    if retrieved_raw is None:
        retrieved = datetime.now(UTC)
    else:
        try:
            retrieved = datetime.fromisoformat(str(retrieved_raw))
        except ValueError as exc:
            raise CorporateIssuanceError(
                "meta.retrieved_at must be timezone-aware ISO-8601"
            ) from exc
        if retrieved.utcoffset() is None:
            raise CorporateIssuanceError("meta.retrieved_at must include a timezone")
        retrieved = retrieved.astimezone(UTC)
    if any(not isinstance(row, Mapping) for row in rows):
        raise CorporateIssuanceError(f"page {number} data rows must be objects")
    return payload, rows, total_count, total_pages, retrieved


def ingest_corporate_issuance_pages(
    pages: Iterable[str | Mapping[str, Any]],
    *,
    database_path: str = str(DEFAULT_DB_PATH),
    source_identifier: str = SOURCE_ID,
    source_name: str = "Corporate issuance structured provider (adapter boundary)",
    source_url: str | None = None,
    production_source_configured: bool = False,
    retrieved_at: datetime | None = None,
) -> dict[str, int]:
    """Validate complete structured pages, then persist normalized tranche facts.

    Expected page contract: ``{"data": [tranche objects], "meta":
    {"total-count": N, "total-pages": M}}``. Any selected vendor may map its
    response to this contract in its own source adapter. No vendor is implied.
    """
    parsed = [_parse_page(page, i) for i, page in enumerate(pages, 1)]
    if not parsed:
        raise CorporateIssuanceError("at least one page is required")
    counts = {(p[2], p[3]) for p in parsed}
    if len(counts) != 1:
        raise CorporateIssuanceError("pagination totals changed across pages")
    total, total_pages = next(iter(counts))
    if len(parsed) != total_pages:
        raise CorporateIssuanceError(f"expected {total_pages} pages; received {len(parsed)}")
    identities: dict[str, str] = {}
    normalized: dict[str, IssuanceTranche] = {}
    row_count = 0
    for _, rows, _, _, _ in parsed:
        for row in rows:
            row_count += 1
            identity = _optional_text(
                row.get("source_record_id"), "source_record_id", required=True
            )
            fingerprint = _canonical(row)
            if identity in identities:
                if identities[identity] != fingerprint:
                    raise CorporateIssuanceError(
                        f"conflicting duplicate source_record_id {identity!r}"
                    )
                continue
            identities[identity] = fingerprint
            normalized[identity] = normalize_record(row)
    if len(identities) != total:
        raise CorporateIssuanceError(
            f"pagination row count mismatch: expected {total}, received {len(identities)} unique records"
        )
    now = retrieved_at or datetime.now(UTC)
    if production_source_configured and (
        source_identifier == SOURCE_ID or not source_url
    ):
        raise CorporateIssuanceError(
            "a configured production source requires a provider-specific identifier and URL"
        )
    if now.utcoffset() is None:
        raise CorporateIssuanceError("retrieved_at must be timezone-aware")
    now = now.astimezone(UTC)
    # Parsing and duplicate validation above finish before a database file is created.
    initialize_database(database_path)
    inserted = unchanged = revisions = 0
    with database(database_path) as conn:
        source_id = register_source(
            conn,
            identifier=source_identifier,
            name=source_name,
            source_type="corporate_bond_issuance",
            url=source_url,
            metadata={
                "event_level_source_selected": production_source_configured,
                "adapter_contract": "provider_neutral_v1",
            },
        )
        series_id = register_series(
            conn,
            source_id=source_id,
            identifier=SERIES_ID,
            name="Corporate issuance tranche fields",
            description="Long-form provider-neutral facts for one corporate debt tranche per source record.",
            instrument_type="corporate_bond_tranche",
            frequency="event",
            metadata={"currency_behavior": "preserve_source"},
        )
        for page_num, (payload, rows, _, _, page_retrieved) in enumerate(parsed, 1):
            raw_id = insert_raw_record(
                conn,
                source_id=source_id,
                external_record_id=f"page:{page_num}",
                payload=payload,
                content_type="application/json",
                retrieval_time=now or page_retrieved,
                metadata={"page": page_num, "synthetic_or_provider_payload": True},
            )
            # Duplicate rows across pages retain the first page's canonical raw link.
            for row in rows:
                record_id = str(row["source_record_id"])
                if identities[record_id] != _canonical(row) or row is not next(
                    (
                        r
                        for _, p_rows, _, _, _ in parsed
                        for r in p_rows
                        if str(r.get("source_record_id")) == record_id
                    ),
                    row,
                ):
                    continue
                item = normalized[record_id]
                values: dict[str, tuple[str | None, float | None, str | None]] = {}
                for field in item.fields:
                    original = row.get(field)
                    if field in NUMERIC_FIELDS:
                        raw, numeric = _number(original, field)
                        unit = (
                            item.fields.get("currency")
                            if field == "principal_amount"
                            else "percent"
                            if field in {"coupon", "yield"}
                            else "percent"
                            if field == "benchmark_yield"
                            else "source_native"
                            if field in {"spread", "credit_spread"}
                            else None
                        )
                        values[field] = (raw, numeric, unit)
                    elif field in DATE_FIELDS:
                        values[field] = (
                            None if original is None else str(original),
                            None,
                            "calendar_date",
                        )
                    else:
                        values[field] = (None if original is None else str(original), None, None)
                values["duration_years_estimate"] = (
                    None if item.duration_years is None else f"{item.duration_years:.12g}",
                    item.duration_years,
                    "years",
                )
                metadata_base = {
                    "source_record_id": record_id,
                    "issuance_event": {
                        key: value.isoformat() if isinstance(value, date) else value
                        for key, value in item.fields.items()
                    },
                    "deal_identifier": item.fields.get("deal_identifier"),
                    "tranche_identifier": item.fields.get("tranche_identifier"),
                    "source_native_fields": item.source_native_fields,
                    "duration_classification": item.duration_kind,
                    "duration_method": item.duration_method,
                    "observation_precision": "calendar_date",
                }
                for field, (raw, numeric, unit) in values.items():
                    logical_key = f"{record_id}|{field}"
                    prior = conn.execute(
                        """SELECT * FROM observations WHERE source_id=? AND series_id=? AND logical_key=?
                        ORDER BY revision DESC LIMIT 1""",
                        (source_id, series_id, logical_key),
                    ).fetchone()
                    normalized_field = (
                        item.fields.get(field)
                        if field != "duration_years_estimate"
                        else item.duration_years
                    )
                    value_text = (
                        None
                        if numeric is not None
                        else (
                            normalized_field.isoformat()
                            if isinstance(normalized_field, date)
                            else None
                            if normalized_field is None
                            else str(normalized_field)
                        )
                    )
                    candidate_metadata = {
                        **metadata_base,
                        "source_field": field,
                        "normalized_value": numeric if numeric is not None else value_text,
                    }
                    candidate_publication = (
                        item.publication_time.isoformat(timespec="microseconds").replace(
                            "+00:00", "Z"
                        )
                        if item.publication_time
                        else None
                    )
                    if (
                        prior is not None
                        and prior["raw_value"] == raw
                        and prior["value_numeric"] == numeric
                        and prior["value_text"] == value_text
                        and json.loads(prior["metadata_json"]) == candidate_metadata
                        and prior["publication_time"] == candidate_publication
                    ):
                        unchanged += 1
                        continue
                    revision = 1 if prior is None else int(prior["revision"]) + 1
                    if prior is not None:
                        revisions += 1
                    observation_time = datetime.combine(item.event_date, time.min, tzinfo=UTC)
                    metadata = candidate_metadata
                    insert_observation(
                        conn,
                        source_id=source_id,
                        series_id=series_id,
                        logical_key=logical_key,
                        revision=revision,
                        revision_of_id=None if prior is None else int(prior["id"]),
                        observation_time=observation_time,
                        publication_time=item.publication_time,
                        retrieval_time=now,
                        value_text=value_text,
                        value_numeric=numeric,
                        unit=unit,
                        raw_value=raw,
                        raw_record_id=raw_id,
                        metadata=metadata,
                    )
                    inserted += 1
    return {
        "records": len(normalized),
        "inserted": inserted,
        "unchanged": unchanged,
        "revisions": revisions,
        "pages": len(parsed),
    }

