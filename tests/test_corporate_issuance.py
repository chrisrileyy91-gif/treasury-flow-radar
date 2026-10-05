"""Offline tests for provider-neutral corporate issuance normalization."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from treasury_flow_radar.database import database, get_observations
from treasury_flow_radar.sources.corporate_issuance import (
    CorporateIssuanceError,
    ingest_corporate_issuance_pages,
    normalize_record,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2025, 10, 1, 14, 0, tzinfo=UTC)


def load(name: str = "corporate_issuance_page_1.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pages(first: dict | None = None, second: dict | None = None) -> list[dict]:
    return [first or load(), second or load("corporate_issuance_page_2.json")]


def test_single_tranche_normalization_and_source_precision():
    row = load()["data"][0]
    item = normalize_record(row)
    assert item.source_record_id == "SYN-DEAL-001-2Y"
    assert item.fields["principal_amount"] == 1_250_000_000
    assert item.fields["currency"] == "USD"
    assert item.fields["coupon"] == 4.25
    assert item.fields["pricing_date"].isoformat() == "2025-06-10"
    assert item.fields["settlement_date"].isoformat() == "2025-06-17"
    assert item.publication_time == datetime(2025, 6, 10, 18, 20, tzinfo=UTC)


def test_multi_tranche_parent_relationship_and_different_maturities():
    rows = load()["data"]
    assert len(rows) == 2
    assert {row["deal_identifier"] for row in rows} == {"SYN-DEAL-001"}
    assert {row["tranche_identifier"] for row in rows} == {"2Y", "10Y"}
    assert {row["security_identifier"] for row in rows} == {"SYN000001AA1", "SYN000001AB9"}
    assert len({row["maturity_date"] for row in rows}) == 2


def test_multiple_issuers_currency_and_large_amount(tmp_path):
    result = ingest_corporate_issuance_pages(
        pages(), database_path=str(tmp_path / "issuance.db"), retrieved_at=NOW
    )
    assert result["records"] == 3
    with database(tmp_path / "issuance.db") as conn:
        rows = get_observations(conn)
    issuer_names = {
        json.loads(row["metadata_json"])["issuance_event"]["issuer_name"] for row in rows
    }
    assert len(issuer_names) == 2
    eur = next(
        row
        for row in rows
        if row["logical_key"].endswith("|principal_amount") and row["unit"] == "EUR"
    )
    event = json.loads(eur["metadata_json"])["issuance_event"]
    assert event["currency"] == "EUR"
    assert eur["unit"] == "EUR"
    assert eur["value_numeric"] == 950_000_000.5


def test_optional_missing_and_null_values_remain_null():
    item = normalize_record(load("corporate_issuance_page_2.json")["data"][0])
    for field in (
        "issuer_identifier",
        "security_identifier",
        "deal_identifier",
        "yield",
        "spread",
        "rating",
        "callable_flag",
        "benchmark_maturity",
    ):
        assert item.fields[field] is None
    assert item.fields["currency"] == "EUR"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2030-06-15", "2030-06-15"),
        ("06/15/2030", "2030-06-15"),
        ("2030/06/15", "2030-06-15"),
        ("2030-06-15T23:00:00-05:00", "2030-06-15"),
    ],
)
def test_supported_date_forms_preserve_written_calendar_date(raw, expected):
    record = dict(load()["data"][0], maturity_date=raw)
    assert normalize_record(record).fields["maturity_date"].isoformat() == expected


def test_identical_duplicate_rows_deduplicate_across_pages(tmp_path):
    p1, p2 = pages()
    p2["data"].append(dict(p1["data"][0]))
    result = ingest_corporate_issuance_pages(
        [p1, p2], database_path=str(tmp_path / "idempotent.db"), retrieved_at=NOW
    )
    assert result["records"] == 3
    assert result["pages"] == 2


def test_conflicting_duplicate_is_rejected_before_database_creation(tmp_path):
    p1, p2 = pages()
    p2["data"].append(dict(p1["data"][0], principal_amount="1"))
    target = tmp_path / "must-not-exist.db"
    with pytest.raises(CorporateIssuanceError, match="conflicting duplicate"):
        ingest_corporate_issuance_pages([p1, p2], database_path=str(target), retrieved_at=NOW)
    assert not target.exists()


def test_repeated_ingestion_is_idempotent_and_preserves_provenance(tmp_path):
    target = tmp_path / "repeat.db"
    first = ingest_corporate_issuance_pages(pages(), database_path=str(target), retrieved_at=NOW)
    repeat = ingest_corporate_issuance_pages(pages(), database_path=str(target), retrieved_at=NOW)
    assert first["inserted"] > 0
    assert repeat["inserted"] == 0
    assert repeat["unchanged"] == first["inserted"]
    with database(target) as conn:
        rows = get_observations(conn)
        row = next(r for r in rows if r["logical_key"].endswith("|principal_amount"))
        linked = conn.execute(
            "SELECT payload, retrieval_time FROM raw_records WHERE id=?", (row["raw_record_id"],)
        ).fetchone()
    assert linked["retrieval_time"] == "2025-10-01T14:00:00.000000Z"
    assert json.loads(linked["payload"])["meta"]["fixture_notice"].startswith("Synthetic")
    assert (
        json.loads(row["metadata_json"])["source_native_fields"]["spread_basis"] == "basis_points"
    )


def test_changed_source_record_is_an_immutable_revision(tmp_path):
    target = tmp_path / "revision.db"
    first_pages = pages()
    ingest_corporate_issuance_pages(first_pages, database_path=str(target), retrieved_at=NOW)
    revised = pages()
    revised[0]["data"][0]["coupon"] = "4.26000"
    later = datetime(2025, 10, 2, 14, 0, tzinfo=UTC)
    result = ingest_corporate_issuance_pages(revised, database_path=str(target), retrieved_at=later)
    assert result["revisions"] > 0
    with database(target) as conn:
        rows = [
            r
            for r in get_observations(conn)
            if r["logical_key"].endswith("|coupon") and "2Y" in r["logical_key"]
        ]
    assert [r["revision"] for r in rows] == [1, 2]
    assert rows[1]["revision_of_id"] == rows[0]["id"]
    assert rows[0]["value_numeric"] == 4.25 and rows[1]["value_numeric"] == 4.26


def test_provenance_observation_publication_and_settlement_times(tmp_path):
    target = tmp_path / "times.db"
    ingest_corporate_issuance_pages(pages(), database_path=str(target), retrieved_at=NOW)
    with database(target) as conn:
        rows = get_observations(conn)
    row = next(r for r in rows if r["logical_key"].endswith("|principal_amount"))
    assert row["observation_time"] == "2025-06-10T00:00:00.000000Z"
    assert row["publication_time"] == "2025-06-10T18:20:00.000000Z"
    assert row["retrieval_time"] == "2025-10-01T14:00:00.000000Z"
    event = json.loads(row["metadata_json"])["issuance_event"]
    assert event["settlement_date"] == "2025-06-17"


def test_duration_is_labeled_estimate_and_maturity_only_proxy():
    row = load()["data"][0]
    item = normalize_record(row)
    assert item.duration_years == pytest.approx(date_days("2025-06-10", "2030-06-15"))
    assert item.duration_kind == "estimate"
    assert item.duration_method == "maturity_years_proxy_act_365_25"
    no_maturity = normalize_record(dict(row, maturity_date=None))
    assert no_maturity.duration_years is None
    assert no_maturity.duration_kind is None


def date_days(start: str, end: str) -> float:
    from datetime import date

    return (date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25


@pytest.mark.parametrize(
    "change,match",
    [
        ({"source_record_id": None}, "source_record_id"),
        ({"issuer_name": None}, "issuer_name"),
        ({"pricing_date": None, "settlement_date": None}, "event date"),
        ({"coupon": "not numeric"}, "numeric"),
        ({"currency": "US Dollars"}, "currency"),
        ({"callable_flag": "maybe"}, "callable_flag"),
    ],
)
def test_unsupported_records_rejected_clearly(change, match):
    with pytest.raises(CorporateIssuanceError, match=match):
        normalize_record(dict(load()["data"][0], **change))


def test_pagination_metadata_checked_before_writes(tmp_path):
    p1, p2 = pages()
    p2["meta"]["total-count"] = "4"
    target = tmp_path / "no-partial.db"
    with pytest.raises(CorporateIssuanceError, match="totals changed"):
        ingest_corporate_issuance_pages([p1, p2], database_path=str(target), retrieved_at=NOW)
    assert not target.exists()

