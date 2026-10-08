from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from treasury_flow_radar.sources.sec_edgar import (
    EdgarClient,
    html_to_text,
    ingest_edgar_pricings,
    model_extract,
    parse_press_release,
    parse_term_sheet,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "edgar_fwp_term_sheet.html").read_text()

PRESS = """<html><body><p>Example Corp Announces Pricing of Senior Notes Offering</p>
<p>Example Corp today announced that it has priced $1,250,000,000 aggregate principal amount of its 5.875% Senior
Notes due 2034 and $750 million aggregate principal amount of 6.250% Senior Notes due 2054, and $400 million
aggregate principal amount of 1.500% Convertible Senior Notes due 2031. The offering is expected to close on
October 9, 2026, subject to customary closing conditions.</p></body></html>"""


def test_term_sheet_tranches_benchmarks_and_dates():
    trade, settle, tranches = parse_term_sheet(html_to_text(FIXTURE))
    assert trade == date(2026, 9, 22) and settle == date(2026, 10, 6)
    assert [t["principal_amount"] for t in tranches] == [1.5e9, 1.0e9, 1.4e9]
    assert [t["coupon"] for t in tranches] == [7.1, 7.25, 7.35]
    assert [t["benchmark_maturity"] for t in tranches] == [date(2031, 8, 31), date(2033, 8, 31), date(2036, 8, 15)]
    assert [t["spread"] for t in tranches] == [228, 237.2, 240.1]
    assert all(t["maturity_date"] == date(2056, 10, 6) for t in tranches)


def test_press_release_excludes_convertibles_and_never_invents_a_maturity_day():
    settle, tranches = parse_press_release(html_to_text(PRESS))
    assert settle == date(2026, 10, 9)
    assert [(t["principal_amount"], t["coupon"], t["maturity_year"]) for t in tranches] == [
        (1.25e9, 5.875, 2034), (750e6, 6.25, 2054)]
    assert all(t["maturity_date"] is None for t in tranches)


class _Resp:
    def __init__(self, body: str) -> None:
        self.body = body.encode()

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_model_values_must_appear_verbatim_in_the_document():
    text = "Acme priced $2,000,000,000 aggregate principal amount of 6.10% Notes due 2036 today."
    reply = {"content": [{"type": "text", "text": json.dumps({"tranches": [
        {"amount": "$2,000,000,000", "coupon": "6.10%", "due_year": "2036"},
        {"amount": "$9,000,000,000", "coupon": "7.00%", "due_year": "2040"}]})}]}
    kept = model_extract(text, api_key="k", opener=lambda request, timeout: _Resp(json.dumps(reply)))
    assert [(t["principal_amount"], t["coupon"], t["maturity_year"]) for t in kept] == [(2e9, 6.10, 2036)]


def _hit(adsh: str, filename: str, cik: str, name: str, day: str, form: str) -> dict:
    return {"_id": f"{adsh}:{filename}", "_source": {"ciks": [cik], "display_names": [f"{name}  (CIK {cik})"],
                                                     "file_date": day, "form": form}}


def _opener(search: dict, documents: dict, calls: list):
    def open_(request, timeout):
        url = request.full_url
        calls.append(url)
        if "search-index" in url:
            form = "FWP" if "forms=FWP" in url else "8-K"
            hits = search.get(form, [])
            return _Resp(json.dumps({"hits": {"total": {"value": len(hits)}, "hits": hits}}))
        return _Resp(documents[url.rsplit("/", 1)[1]])
    return open_


def test_ingest_dedupes_coregistrants_skips_known_documents_and_stores_provenance(tmp_path):
    db = tmp_path / "edgar.sqlite3"
    search = {"FWP": [_hit("0001-26-000001", "a_fwp.htm", "0000096021", "SYSCO CORP", "2026-09-23", "FWP"),
                      _hit("0001-26-000002", "b_fwp.htm", "0002134688", "Sysco Holdings Corp", "2026-09-23", "FWP")],
              "8-K": [_hit("0002-26-000003", "ex99.htm", "0000000123", "EXAMPLE CORP", "2026-10-02", "8-K")]}
    documents = {"a_fwp.htm": FIXTURE, "b_fwp.htm": FIXTURE, "ex99.htm": PRESS}
    calls: list[str] = []
    client = EdgarClient(opener=_opener(search, documents, calls), clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    result = ingest_edgar_pricings(database_path=db, client=client, end_date=date(2026, 10, 7))
    assert result["documents"] == 3 and result["deals"] == 2          # duplicate co-registrant dropped
    from treasury_flow_radar.analytics.report import load_observations_read_only
    from treasury_flow_radar.analytics.research import edgar_deals
    deals = edgar_deals(load_observations_read_only(db))
    sysco = next(d for d in deals if d["name"].startswith("SYSCO"))
    assert sysco["pricing_date"] == "2026-09-22" and sysco["settlement_date_expected"] == "2026-10-06"
    assert sysco["size_usd"] == pytest.approx(3.9e9)
    assert sorted(t["maturity_year"] for t in sysco["tranches"]) == [2031, 2033, 2036]   # benchmark tenor
    assert sysco["sources"][0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/96021/")
    example = next(d for d in deals if d["name"].startswith("EXAMPLE"))
    assert example["pricing_date"] == "2026-10-02" and example["size_usd"] == pytest.approx(2.0e9)
    calls.clear()
    again = ingest_edgar_pricings(database_path=db, client=client, end_date=date(2026, 10, 7))
    assert again["documents"] == 0                                    # already-stored filings are not refetched
    assert not [c for c in calls if "Archives" in c]


def test_curated_deal_wins_over_matching_discovery():
    from treasury_flow_radar.analytics.research import merge_deals
    curated = [{"id": "c", "pricing_date": "2026-09-30", "size_usd": 41.4e9}]
    found = [{"id": "e1", "pricing_date": "2026-09-30", "size_usd": 41.0e9},
             {"id": "e2", "pricing_date": "2026-09-30", "size_usd": 3.0e9}]
    assert [d["id"] for d in merge_deals(curated, found)] == ["c", "e2"]


def test_one_failing_search_is_retried_then_isolated(tmp_path):
    from urllib.error import HTTPError
    attempts = {"8-K": 0}

    def open_(request, timeout):
        url = request.full_url
        if "search-index" in url and "forms=8-K" in url:
            attempts["8-K"] += 1
            raise HTTPError(url, 500, "Server Error", {}, None)
        if "search-index" in url:
            hit = _hit("0001-26-000001", "a_fwp.htm", "0000096021", "SYSCO CORP", "2026-09-23", "FWP")
            return _Resp(json.dumps({"hits": {"total": {"value": 1}, "hits": [hit]}}))
        return _Resp(FIXTURE)

    client = EdgarClient(opener=open_, clock=lambda: datetime(2026, 10, 7, tzinfo=UTC), sleep=lambda s: None)
    result = ingest_edgar_pricings(database_path=tmp_path / "e.sqlite3", client=client, end_date=date(2026, 10, 7))
    assert attempts["8-K"] == 3                     # retried twice on a server error
    assert result["deals"] == 1                     # the FWP search still produced its deal
    assert result["warnings"] and "8-K search request failed with status 500" in result["warnings"][0]


def test_all_searches_failing_is_reported_as_failure(tmp_path):
    from urllib.error import HTTPError

    def open_(request, timeout):
        raise HTTPError(request.full_url, 503, "Unavailable", {}, None)

    client = EdgarClient(opener=open_, clock=lambda: datetime(2026, 10, 7, tzinfo=UTC), sleep=lambda s: None)
    from treasury_flow_radar.sources.sec_edgar import EdgarError
    with pytest.raises(EdgarError, match="FWP search request failed with status 503"):
        ingest_edgar_pricings(database_path=tmp_path / "e.sqlite3", client=client, end_date=date(2026, 10, 7))


# Layouts seen in real filings (values shortened): one label followed by every tranche's
# value, settlement written after a T+n note, and a "UST" benchmark prefix or suffix.
COLUMNAR = """<table>
<tr><td>Trade Date:</td><td>September 22, 2026</td></tr>
<tr><td>Expected Settlement Date**:</td><td>T + 10; October 6, 2026</td></tr>
<tr><td>Principal Amount:</td><td>$1,500,000,000</td><td>$1,000,000,000</td><td>$1,400,000,000</td></tr>
<tr><td>Coupon:</td><td>7.100%</td><td>7.250%</td><td>7.350%</td></tr>
<tr><td>Benchmark Treasury:</td><td>UST 4.375% due August 31, 2031</td><td>UST 4.500% due August 31, 2033</td>
<td>UST 4.625% due August 15, 2036</td></tr>
<tr><td>Spread to Benchmark Treasury:</td><td>T + 228 bps</td><td>T + 237 bps</td><td>T + 240 bps</td></tr>
</table>"""


def test_columnar_term_sheet_with_t_plus_settlement_and_ust_prefix():
    trade, settle, tranches = parse_term_sheet(html_to_text(COLUMNAR))
    assert trade == date(2026, 9, 22) and settle == date(2026, 10, 6)
    assert [t["principal_amount"] for t in tranches] == [1.5e9, 1.0e9, 1.4e9]
    assert [t["coupon"] for t in tranches] == [7.1, 7.25, 7.35]
    assert [t["benchmark_maturity"] for t in tranches] == [date(2031, 8, 31), date(2033, 8, 31), date(2036, 8, 15)]
    assert [t["spread"] for t in tranches] == [228, 237, 240]


@pytest.mark.parametrize(("line", "expected"), [
    ("Settlement Date*: T + 2 (September 21, 2026)", date(2026, 9, 21)),
    ("Settlement Date (T+4)**: September 25, 2026", date(2026, 9, 25)),
    ("Settlement Date: October 5, 2026 (T+5)", date(2026, 10, 5)),
])
def test_settlement_date_formats(line, expected):
    text = f"Trade Date: September 18, 2026\n{line}\nPrincipal Amount: $700,000,000\nCoupon: 6.000%\n" \
           "Benchmark Treasury: 4.625% UST due August 15, 2036"
    _, settle, tranches = parse_term_sheet(text)
    assert settle == expected
    assert tranches[0]["benchmark_maturity"] == date(2036, 8, 15)   # "UST" after the coupon also works


def test_settlement_stays_unknown_when_only_t_plus_n_is_stated():
    text = "Trade Date: October 5, 2026\nPrincipal Amount: $700,000,000\nCoupon: 5.500%\n" \
           "The notes will settle on the second business day (T+2)."
    assert parse_term_sheet(text)[1] is None         # no calendar date is computed or guessed


def test_field_with_mismatched_value_count_stays_unknown():
    text = "Principal Amount: $600,000,000 $400,000,000\nCoupon: 7.800%\nTrade Date: September 24, 2026"
    tranches = parse_term_sheet(text)[2]
    assert [t["coupon"] for t in tranches] == [None, None]     # one coupon cannot be assigned to two tranches


def test_add_on_offering_counts_only_the_new_notes():
    text = ("LandBridge priced an offering of $125 million aggregate principal amount of 6.250% Senior Notes due "
            "2033 (the \"New Notes\"). The New Notes are additional notes under the indenture under which the "
            "Company previously issued $500 million aggregate principal amount of 6.250% Senior Notes due 2033 "
            "(the \"Existing Notes\"). The offering is expected to close on October 1, 2026.")
    settle, tranches = parse_press_release(text)
    assert [t["principal_amount"] for t in tranches] == [125e6]
    assert settle == date(2026, 10, 1)


def _row(deal: str, tranche: int, amount: float, version: int | None) -> dict:
    native = {"filing_url": "https://www.sec.gov/Archives/edgar/data/1/x/y.htm", "form": "FWP"}
    if version is not None:
        native["parser_version"] = version
    return {"revision": 1, "metadata": {
        "deal_identifier": deal, "source_record_id": f"{deal}:{tranche}", "source_native_fields": native,
        "issuance_event": {"issuer_name": "X", "principal_amount": amount, "coupon": 6.0,
                           "pricing_date": "2026-09-22", "benchmark_maturity": "2031-08-31"}}}


def test_only_tranches_from_the_newest_parser_count():
    from treasury_flow_radar.analytics.research import edgar_deals
    rows = [_row("A", 1, 1.5e9, None), _row("A", 2, 9.9e9, None),     # old parse: wrong second tranche
            _row("A", 1, 1.5e9, 2),                                     # current parse of the same filing
            _row("B", 1, 625e6, None)]                                  # current parser rejects B entirely
    deals = edgar_deals(rows)
    assert [(d["id"], d["size_usd"]) for d in deals] == [("edgar-A", 1.5e9)]


def test_stored_filings_are_reparsed_without_refetching(tmp_path):
    db = tmp_path / "edgar.sqlite3"
    search = {"FWP": [_hit("0001-26-000001", "a_fwp.htm", "0000096021", "SYSCO CORP", "2026-09-23", "FWP")]}
    calls: list[str] = []
    client = EdgarClient(opener=_opener(search, {"a_fwp.htm": FIXTURE}, calls),
                         clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    ingest_edgar_pricings(database_path=db, client=client, end_date=date(2026, 10, 7))
    calls.clear()
    again = ingest_edgar_pricings(database_path=db, client=client, end_date=date(2026, 10, 7))
    assert again["documents"] == 0 and again["deals"] == 1 and again["inserted"] == 0
    assert not [c for c in calls if "Archives" in c]
