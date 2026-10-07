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
