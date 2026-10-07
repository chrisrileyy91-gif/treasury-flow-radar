"""Corporate bond pricings discovered automatically from SEC EDGAR filings.

Two official, free EDGAR paths (no API key):

1. Registered deals: final pricing term sheets filed as FWP documents. These list
   principal amount, coupon, maturity, benchmark Treasury, spread, trade date and
   settlement date per tranche, and are parsed with plain pattern matching.
2. Unregistered (Rule 144A) deals: pricing press releases attached to 8-K filings.
   Pattern matching handles the common "$X aggregate principal amount of Y% notes due
   YEAR" wording. Optionally, when ANTHROPIC_API_KEY is set, at most
   MAX_MODEL_DOCUMENTS press releases per run that pattern matching cannot read are
   sent (first 6,000 characters) to a small model for extraction. Every amount and
   coupon the model returns must appear verbatim in the document, or the result is
   discarded.

Search uses the EDGAR full-text search endpoint; documents are read from the public
Archives. Parsed tranches are stored through the provider-neutral corporate issuance
adapter with the filing URL as provenance; the fetched document text is stored in
raw_records. Equity-linked (convertible/exchangeable) notes are excluded because they
are not hedged as rate exposure in the same way. Nothing is inferred when a field is
absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from treasury_flow_radar.database import (
    DEFAULT_DB_PATH,
    database,
    initialize_database,
    insert_raw_record,
    register_source,
)
from treasury_flow_radar.sources.corporate_issuance import ingest_corporate_issuance_pages

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SOURCE_IDENTIFIER = "SEC_EDGAR_PRICINGS"
SOURCE_NAME = "SEC EDGAR corporate bond pricing filings"
SOURCE_URL = "https://efts.sec.gov/LATEST/search-index"
DEFAULT_USER_AGENT = "TreasuryFlowRadar research https://github.com/chrisrileyy91-gif/treasury-flow-radar"
LOOKBACK_DAYS = 21
MAX_DOCUMENTS = 60
MAX_MODEL_DOCUMENTS = 3
MIN_DEAL_USD = 500e6
MODEL = "claude-haiku-4-5-20251001"
QUERIES = (
    ("FWP", '"Spread to Benchmark Treasury"'),
    ("8-K", '"aggregate principal amount" "priced" "notes due"'),
)
MONTH_DATE = r"([A-Z][a-z]+\.? \d{1,2}, \d{4})"


class EdgarError(RuntimeError):
    """EDGAR could not be reached or returned an unusable response."""


@dataclass
class Filing:
    form: str
    adsh: str
    cik: str
    filename: str
    file_date: date
    issuer: str

    @property
    def url(self) -> str:
        return f"{ARCHIVES}/{int(self.cik)}/{self.adsh.replace('-', '')}/{self.filename}"


@dataclass
class Pricing:
    filing: Filing
    trade_date: date | None
    settlement_date: date | None
    tranches: list[dict[str, Any]] = field(default_factory=list)
    method: str = "pattern"


class _Text(HTMLParser):
    BREAKS = frozenset({"br", "p", "div", "tr", "td", "th", "li", "table", "h1", "h2", "h3", "h4"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.BREAKS:
            self.parts.append(" \n ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BREAKS:
            self.parts.append(" \n ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(document: str) -> str:
    parser = _Text()
    parser.feed(document)
    text = "".join(parser.parts).replace("\xa0", " ").replace(" ", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _parse_day(text: str | None) -> date | None:
    if not text:
        return None
    cleaned = text.replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC).date()  # calendar date only
        except ValueError:
            continue
    return None


def _money(amount: str, scale: str | None) -> float:
    value = float(amount.replace(",", ""))
    if scale:
        value *= 1e9 if scale.lower().startswith("b") else 1e6
    return value


def parse_term_sheet(text: str) -> tuple[date | None, date | None, list[dict[str, Any]]]:
    """Parse a final pricing term sheet (FWP). Returns trade date, settlement date, tranches."""
    flat = re.sub(r"\s+", " ", text)
    amounts = [m for m in re.finditer(
        r"(?:Aggregate )?Principal Amount(?: Offered)?\s*:?\s*(?:US)?\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)?",
        flat, re.IGNORECASE)]
    if not amounts:
        return None, None, []

    def field_values(pattern: str) -> list[re.Match[str]]:
        return list(re.finditer(pattern, flat, re.IGNORECASE))

    coupons = field_values(r"Coupon(?: \(Interest Rate\))?(?: Rate)?\s*:?\s*(\d{1,2}\.\d+)\s*%")
    maturities = field_values(r"Maturity(?: Date)?\s*:?\s*" + MONTH_DATE)
    benchmarks = field_values(r"Benchmark Treasury\s*:?\s*(\d{1,2}\.\d+)\s*% due " + MONTH_DATE)
    spreads = field_values(r"Spread to Benchmark(?: Treasury)?\s*:?\s*\+?\s*T?\s*\+\s*([\d.]+)\s*(?:bps|basis points)")
    yields = field_values(r"(?:Re-?offer )?Yield to Maturity\s*:?\s*(\d{1,2}\.\d+)\s*%")
    trade = re.search(r"(?:Trade|Pricing) Date\s*:?\s*" + MONTH_DATE, flat, re.IGNORECASE)
    settle = re.search(r"Settlement(?: Date)?\s*:?\s*(?:\(?T\s*\+\s*\d+\)?\s*[;:,]?\s*)?" + MONTH_DATE, flat, re.IGNORECASE)

    def pick(matches: list[re.Match[str]], index: int) -> re.Match[str] | None:
        if len(matches) == len(amounts):
            return matches[index]
        if len(matches) == 1:
            return matches[0]
        # Otherwise use the last match that appears before the next tranche's amount.
        limit = amounts[index + 1].start() if index + 1 < len(amounts) else len(flat)
        start = amounts[index - 1].end() if index else 0
        inside = [m for m in matches if start <= m.start() < limit]
        return inside[-1] if inside else None

    tranches = []
    for index, amount in enumerate(amounts):
        coupon, maturity, bench = pick(coupons, index), pick(maturities, index), pick(benchmarks, index)
        spread, ytm = pick(spreads, index), pick(yields, index)
        tranches.append({
            "principal_amount": _money(amount.group(1), amount.group(2)),
            "coupon": None if coupon is None else float(coupon.group(1)),
            "maturity_date": None if maturity is None else _parse_day(maturity.group(1)),
            "benchmark_maturity": None if bench is None else _parse_day(bench.group(2)),
            "spread": None if spread is None else float(spread.group(1)),
            "yield": None if ytm is None else float(ytm.group(1)),
        })
    return (None if trade is None else _parse_day(trade.group(1)),
            None if settle is None else _parse_day(settle.group(1)), tranches)


PRESS_TRANCHE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)?\s+(?:in\s+)?(?:aggregate principal amount|principal amount)\s+of\s+"
    r"(?:its\s+|the\s+Company's\s+|our\s+)?(\d{1,2}\.\d+)\s*%\s+([^$.;]{0,80}?)\s*(?:notes|Notes|debentures|Debentures)\s+due\s+(\d{4})")
EQUITY_LINKED = re.compile(r"convertible|exchangeable", re.IGNORECASE)


def parse_press_release(text: str) -> tuple[date | None, list[dict[str, Any]]]:
    """Parse a pricing press release. Returns expected settlement date and tranches."""
    flat = re.sub(r"\s+", " ", text)
    tranches, seen = [], set()
    for m in PRESS_TRANCHE.finditer(flat):
        if EQUITY_LINKED.search(m.group(4)):
            continue
        key = (m.group(1), m.group(3), m.group(5))
        if key in seen:
            continue
        seen.add(key)
        tranches.append({"principal_amount": _money(m.group(1), m.group(2)), "coupon": float(m.group(3)),
                         "maturity_date": None, "maturity_year": int(m.group(5)),
                         "benchmark_maturity": None, "spread": None, "yield": None})
    settle = re.search(r"expected to (?:close|settle)[^.]{0,80}?on (?:or about )?" + MONTH_DATE, flat)
    return (None if settle is None else _parse_day(settle.group(1))), tranches


def model_extract(text: str, *, api_key: str, opener: Callable[..., Any] = urlopen) -> list[dict[str, Any]]:
    """Ask a small model for tranches; keep only values that appear verbatim in the text."""
    excerpt = text[:6000]
    prompt = ("Extract the bond tranches priced in this press release. Reply with JSON only: "
              '{"tranches":[{"amount":"<exactly as written, e.g. $1,250,000,000 or $1.25 billion>",'
              '"coupon":"<exactly as written, e.g. 6.30%>","due_year":"<YYYY>"}]}. '
              "Exclude convertible or exchangeable notes and loans. If none, return {\"tranches\":[]}.\n\n" + excerpt)
    body = json.dumps({"model": MODEL, "max_tokens": 400,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    request = Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                      headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"})
    try:
        with opener(request, timeout=60) as response:
            reply = json.loads(response.read().decode())
    except (HTTPError, URLError, OSError, ValueError):
        return []
    content = "".join(part.get("text", "") for part in reply.get("content", []) if isinstance(part, dict))
    match = re.search(r"\{.*\}", content, re.DOTALL)
    try:
        items = json.loads(match.group(0)).get("tranches", []) if match else []
    except (json.JSONDecodeError, AttributeError):
        return []
    flat = re.sub(r"\s+", " ", text)
    kept = []
    for item in items:
        amount, coupon, year = str(item.get("amount", "")), str(item.get("coupon", "")), str(item.get("due_year", ""))
        money = re.fullmatch(r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)?", amount.strip())
        rate = re.fullmatch(r"(\d{1,2}\.\d+)\s*%", coupon.strip())
        # Guard against invented values: each must appear in the source text as written.
        if not (money and rate and re.fullmatch(r"\d{4}", year) and amount in flat and coupon in flat
                and f"due {year}" in flat):
            continue
        kept.append({"principal_amount": _money(money.group(1), money.group(2)), "coupon": float(rate.group(1)),
                     "maturity_date": None, "maturity_year": int(year),
                     "benchmark_maturity": None, "spread": None, "yield": None})
    return kept


class EdgarClient:
    def __init__(self, *, user_agent: str | None = None, opener: Callable[..., Any] = urlopen,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.user_agent = user_agent or os.getenv("SEC_USER_AGENT") or DEFAULT_USER_AGENT
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(UTC))

    def _get(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
        try:
            with self._opener(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise EdgarError(f"EDGAR request failed with status {exc.code}") from None
        except (URLError, OSError) as exc:
            raise EdgarError(f"EDGAR network request failed: {exc}") from None

    def search(self, form: str, phrase: str, start: date, end: date) -> list[Filing]:
        filings, offset = [], 0
        while True:
            query = urlencode({"q": phrase, "forms": form, "dateRange": "custom", "startdt": start.isoformat(),
                               "enddt": end.isoformat(), "from": offset})
            document = json.loads(self._get(f"{SEARCH_URL}?{query}"))
            hits = (document.get("hits") or {}).get("hits") or []
            for hit in hits:
                source = hit.get("_source") or {}
                adsh, _, filename = str(hit.get("_id", "")).partition(":")
                if not adsh or not filename or not source.get("ciks"):
                    continue
                name = re.sub(r"\s*\(.*$", "", (source.get("display_names") or [""])[0]).strip()
                filings.append(Filing(form, adsh, source["ciks"][0], filename,
                                      date.fromisoformat(source["file_date"]), name))
            total = ((document.get("hits") or {}).get("total") or {}).get("value", 0)
            offset += len(hits)
            if not hits or offset >= total or offset >= 500:
                return filings

    def document(self, filing: Filing) -> str:
        return self._get(filing.url)


def discover(client: EdgarClient, start: date, end: date, *, max_documents: int = MAX_DOCUMENTS,
             known: set[str] | None = None, api_key: str | None = None,
             model_opener: Callable[..., Any] = urlopen) -> tuple[list[Pricing], list[tuple[Filing, str]]]:
    """Search, fetch, and parse filings. Returns pricings and (filing, html) for provenance."""
    known = known or set()
    pricings: list[Pricing] = []
    fetched: list[tuple[Filing, str]] = []
    model_calls = 0
    seen_deals: set[tuple] = set()
    for form, phrase in QUERIES:
        for filing in client.search(form, phrase, start, end):
            key = f"{filing.adsh}:{filing.filename}"
            if key in known or len(fetched) >= max_documents:
                continue
            html = client.document(filing)
            fetched.append((filing, html))
            text = html_to_text(html)
            if form == "FWP":
                trade, settle, tranches = parse_term_sheet(text)
                method = "pattern"
            else:
                settle, tranches = parse_press_release(text)
                trade, method = filing.file_date, "pattern"
                if not tranches and api_key and model_calls < MAX_MODEL_DOCUMENTS and "pric" in text.lower():
                    model_calls += 1
                    tranches, method = model_extract(text, api_key=api_key, opener=model_opener), "model, verified verbatim"
            tranches = [t for t in tranches if t["principal_amount"] and t["coupon"] is not None]
            if not tranches or sum(t["principal_amount"] for t in tranches) < MIN_DEAL_USD:
                continue
            fingerprint = (trade, tuple(sorted((t["principal_amount"], t["coupon"]) for t in tranches)))
            if fingerprint in seen_deals:          # co-registrants file the same terms twice
                continue
            seen_deals.add(fingerprint)
            pricings.append(Pricing(filing, trade or filing.file_date, settle, tranches, method))
    return pricings, fetched


def ingest_edgar_pricings(*, database_path: str | Path = DEFAULT_DB_PATH, client: EdgarClient | None = None,
                          start_date: date | None = None, end_date: date | None = None,
                          max_documents: int = MAX_DOCUMENTS) -> dict[str, int]:
    edgar = client or EdgarClient()
    end = end_date or edgar._clock().date()
    start = max(start_date or end - timedelta(days=LOOKBACK_DAYS), end - timedelta(days=LOOKBACK_DAYS))
    initialize_database(database_path)
    with database(database_path) as conn:
        known = {row[0] for row in conn.execute(
            "SELECT r.external_record_id FROM raw_records r JOIN sources s ON s.id = r.source_id "
            "WHERE s.identifier = ?", (SOURCE_IDENTIFIER,))}
    pricings, fetched = discover(edgar, start, end, max_documents=max_documents, known=known,
                                 api_key=os.getenv("ANTHROPIC_API_KEY") or None)
    retrieved = edgar._clock()
    with database(database_path) as conn:
        source_id = register_source(conn, identifier=SOURCE_IDENTIFIER, name=SOURCE_NAME,
                                    source_type="corporate_issuance", url=SOURCE_URL,
                                    metadata={"event_level_source_selected": True, "provider": "SEC EDGAR"})
        for filing, html in fetched:
            insert_raw_record(conn, source_id=source_id, external_record_id=f"{filing.adsh}:{filing.filename}",
                              payload=html, content_type="text/html", retrieval_time=retrieved,
                              metadata={"url": filing.url, "form": filing.form, "issuer": filing.issuer,
                                        "file_date": filing.file_date.isoformat()})
    rows = []
    for pricing in pricings:
        filing = pricing.filing
        for number, tranche in enumerate(pricing.tranches, 1):
            rows.append({
                "source_record_id": f"{filing.adsh}:{number}", "issuer_name": filing.issuer,
                "issuer_identifier": filing.cik, "deal_identifier": filing.adsh, "tranche_identifier": str(number),
                "currency": "USD", "principal_amount": str(tranche["principal_amount"]),
                "coupon": str(tranche["coupon"]), "yield": None if tranche["yield"] is None else str(tranche["yield"]),
                "spread": None if tranche["spread"] is None else str(tranche["spread"]),
                "maturity_date": tranche["maturity_date"].isoformat() if tranche["maturity_date"] else None,
                "benchmark_maturity": tranche["benchmark_maturity"].isoformat() if tranche["benchmark_maturity"] else None,
                "pricing_date": pricing.trade_date.isoformat() if pricing.trade_date else None,
                "settlement_date": pricing.settlement_date.isoformat() if pricing.settlement_date else None,
                "issuance_type": "registered pricing term sheet (FWP)" if filing.form == "FWP" else "pricing press release (8-K)",
                "source_native_fields": {"filing_url": filing.url, "form": filing.form, "extraction": pricing.method,
                                         "maturity_year": tranche.get("maturity_year") or (
                                             tranche["maturity_date"].year if tranche["maturity_date"] else None),
                                         "document_sha256": hashlib.sha256(
                                             next(h for f, h in fetched if f is filing).encode()).hexdigest()},
            })
    if not rows:
        return {"inserted": 0, "unchanged": 0, "missing": 0, "documents": len(fetched), "deals": 0}
    page = {"data": rows, "meta": {"total-count": len(rows), "total-pages": 1}}
    result = ingest_corporate_issuance_pages(
        [page], database_path=str(database_path), source_identifier=SOURCE_IDENTIFIER, source_name=SOURCE_NAME,
        source_url=SOURCE_URL, production_source_configured=True, retrieved_at=retrieved)
    return {**result, "documents": len(fetched), "deals": len(pricings)}
