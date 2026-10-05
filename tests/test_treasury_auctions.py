"""Offline contract and persistence tests for Treasury Fiscal Data auctions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from treasury_flow_radar.database import database, get_observations, get_observations_as_of
from treasury_flow_radar.sources.treasury_auctions import (
    API_URL,
    TreasuryAuctionClient,
    TreasuryAuctionRequestError,
    TreasuryAuctionResponseError,
    ingest_treasury_auctions,
    parse_response,
)

ROOT = Path(__file__).parent / "fixtures"
RETRIEVED = datetime(2025, 2, 20, 17, 30, tzinfo=UTC)


class Response:
    status = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json", "ETag": '"fixture"'}

    def __init__(self, content: str):
        self.content = content.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.content


class Opener:
    def __init__(self, payloads):
        self.payloads, self.requests = list(payloads), []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.payloads.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


def fixture(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def envelope(rows, count: int, pages: int) -> str:
    return json.dumps({"data": rows, "meta": {"total-count": str(count), "total-pages": str(pages)}})


def client(payloads, *, page_size=3, clock=lambda: RETRIEVED):
    opener = Opener(payloads)
    return TreasuryAuctionClient(opener=opener, clock=clock, page_size=page_size), opener


def test_parses_source_fields_security_types_and_reopening():
    one = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))
    two = json.loads(fixture("treasury_fiscaldata_auctions_page_2.json"))
    records, count, pages = parse_response(envelope(one["data"] + two["data"][1:], 5, 2))
    assert count == 5 and pages == 2 and len(records) == 5
    assert {(r.security_type, r.security_term) for r in records} == {
        ("Note", "2-Year"), ("Note", "5-Year"), ("Bond", "20-Year"), ("Bond", "30-Year")
    }
    reopening = next(r for r in records if r.reopening == "Yes")
    assert reopening.cusip == "91282CJM4"
    assert reopening.original_issue_date.isoformat() == "2025-01-15"
    assert reopening.issue_date.isoformat() == "2025-02-18"
    assert reopening.auction_date.isoformat() == "2025-02-05"


@pytest.mark.parametrize("source,expected", [
    ("2025-01-15", "2025-01-15"),
    ("2025-01-15T00:00:00-05:00", "2025-01-15"),
])
def test_calendar_dates_do_not_shift_through_timezone_conversion(source, expected):
    raw = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    raw["issue_date"] = source
    record = parse_response(envelope([raw], 1, 1))[0][0]
    assert record.issue_date.isoformat() == expected


def test_missing_numeric_fields_remain_null_and_numeric_strings_parse():
    raw = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    raw["total_accepted"] = None
    raw["high_yield"] = "4.2400"
    record = parse_response(envelope([raw], 1, 1))[0][0]
    values = {field: (raw_value, number) for field, raw_value, number in record.values}
    assert values["total_accepted"] == (None, None)
    assert values["high_yield"] == ("4.2400", 4.24)
    assert values["offering_amt"] == ("69000000.000000", 69_000_000.0)


def test_target_excludes_tips_frns_and_unrequested_terms_without_relabeling():
    raw = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    tips = dict(raw, inflation_index_security="Yes")
    frn = dict(raw, floating_rate="Yes")
    bill = dict(raw, security_type="Bill", security_term="13-Week")
    parsed = parse_response(envelope([raw, tips, frn, bill], 4, 1))[0]
    assert len(parsed) == 1 and parsed[0].security_type == "Note"


def test_multiple_pages_overlap_deduplicates_and_query_is_official():
    rows1 = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"]
    rows2 = json.loads(fixture("treasury_fiscaldata_auctions_page_2.json"))["data"]
    p1 = envelope(rows1, 5, 2)
    p2 = envelope(rows2, 5, 2)
    cli, opener = client([p1, p2])
    pages = cli.fetch_history()
    assert [p.number for p in pages] == [1, 2]
    assert sum(len(p.auctions) for p in pages) == 5
    request, timeout = opener.requests[0]
    query = parse_qs(urlsplit(request.full_url).query)
    assert urlsplit(request.full_url).netloc == "api.fiscaldata.treasury.gov"
    assert request.full_url.startswith(API_URL + "?")
    assert query["page[number]"] == ["1"] and query["page[size]"] == ["3"]
    assert "cusip" in query["fields"][0] and "announcemt_date" in query["fields"][0]
    assert timeout == 45 and request.get_header("Accept") == "application/json"


def test_conflicting_duplicate_across_pages_rejected():
    rows1 = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"]
    duplicate = dict(rows1[0], offering_amt="1")
    cli, _ = client([envelope(rows1, 5, 2), envelope([duplicate], 5, 2)])
    with pytest.raises(TreasuryAuctionResponseError, match="conflicting duplicate"):
        cli.fetch_history()


def test_duplicate_within_page_rejected_if_conflicting_and_idempotent_if_same():
    row = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    same, _, _ = parse_response(envelope([row, row], 1, 1))
    assert len(same) == 1
    bad = dict(row, total_accepted="0")
    with pytest.raises(TreasuryAuctionResponseError, match="conflicting duplicate"):
        cli, _ = client([envelope([row, bad], 1, 1)])
        cli.fetch_history()


def test_pagination_totals_and_row_completeness_are_checked():
    row = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    cli, _ = client([envelope([row], 2, 2), envelope([], 3, 2)])
    with pytest.raises(TreasuryAuctionResponseError, match="pagination totals changed"):
        cli.fetch_history()
    cli, _ = client([envelope([row], 2, 2), envelope([], 2, 2)])
    with pytest.raises(TreasuryAuctionResponseError, match="unique auctions"):
        cli.fetch_history()


def test_complete_ingestion_provenance_revisions_and_asof(tmp_path):
    rows1 = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"]
    page_one = fixture("treasury_fiscaldata_auctions_page_1.json")
    page_two = fixture("treasury_fiscaldata_auctions_page_2.json")
    db = tmp_path / "auction.sqlite3"
    first_client, _ = client([page_one, page_two])
    first = ingest_treasury_auctions(database_path=db, client=first_client)
    repeat_client, _ = client([page_one, page_two])
    repeated = ingest_treasury_auctions(database_path=db, client=repeat_client)
    changed = [dict(row) for row in rows1]
    changed[0]["high_yield"] = "4.230"
    changed_payload = envelope(changed, 5, 2)
    revised_client, _ = client([changed_payload, page_two], clock=lambda: datetime(2025, 2, 21, 17, 30, tzinfo=UTC))
    revised = ingest_treasury_auctions(database_path=db, client=revised_client)
    assert first["auctions"] == 5 and first["inserted"] == 45
    assert repeated["inserted"] == 0 and repeated["unchanged"] == 45
    assert revised["inserted"] == 1
    with database(db) as conn:
        all_rows = get_observations(conn)
        assert len(all_rows) == 46
        assert all(row["publication_time"] is None for row in all_rows)
        sample = next(row for row in all_rows if row["raw_value"] == "69000000.000000")
        metadata = json.loads(sample["metadata_json"])
        assert metadata["source_field"] == "offering_amt"
        assert metadata["dates"]["announcement_date"] == "2025-01-02"
        assert metadata["dates"]["auction_date"] == "2025-01-06"
        assert metadata["dates"]["issue_date"] == "2025-01-15"
        assert metadata["reopening"] == "No" and metadata["new_issue_or_reopening"] == "new_issue"
        linked = conn.execute("""SELECT r.payload, r.retrieval_time, s.identifier, src.identifier
            FROM observations o JOIN raw_records r ON r.id=o.raw_record_id
            JOIN series s ON s.id=o.series_id JOIN sources src ON src.id=o.source_id WHERE o.id=?""",
            (sample["id"],)).fetchone()
        assert linked[0] == page_one and linked[1] == "2025-02-20T17:30:00.000000Z"
        assert linked[3] == "U.S. Treasury Fiscal Data" and "2_year" in linked[2]
        prior = next(row for row in all_rows if row["raw_value"] == "4.240" and row["logical_key"].endswith("high_yield"))
        revision = next(row for row in all_rows if row["logical_key"] == prior["logical_key"] and row["revision"] == 2)
        assert revision["revision"] == 2 and revision["revision_of_id"] == prior["id"]
        before = get_observations_as_of(conn, RETRIEVED + timedelta(hours=1))
        after = get_observations_as_of(conn, datetime(2025, 2, 22, tzinfo=UTC))
        assert next(row["value_numeric"] for row in before if row["logical_key"] == prior["logical_key"]) == 4.24
        assert next(row["value_numeric"] for row in after if row["logical_key"] == prior["logical_key"]) == 4.23


def test_conflict_fails_before_database_is_initialized(tmp_path):
    row = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    conflict = dict(row, offering_amt="1")
    cli, _ = client([envelope([row, conflict], 2, 1)])
    db = tmp_path / "no_partial.sqlite3"
    with pytest.raises(TreasuryAuctionResponseError):
        ingest_treasury_auctions(database_path=db, client=cli)
    assert not db.exists()


def test_bad_fields_and_http_error_fail_closed():
    row = json.loads(fixture("treasury_fiscaldata_auctions_page_1.json"))["data"][0]
    with pytest.raises(TreasuryAuctionResponseError, match="CUSIP|cusip"):
        parse_response(envelope([dict(row, cusip=None)], 1, 1))
    with pytest.raises(TreasuryAuctionResponseError, match="numeric"):
        parse_response(envelope([dict(row, offering_amt="bad")], 1, 1))
    cli, _ = client([URLError("network disabled")])
    with pytest.raises(TreasuryAuctionRequestError, match="request failed"):
        cli.fetch_history()


