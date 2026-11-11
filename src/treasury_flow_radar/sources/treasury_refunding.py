"""Treasury's published cash plan, read from its quarterly refunding documents.

Entry point: Treasury's "Most Recent Quarterly Refunding Documents" page, which always links the
current refunding's documents. Two of them carry the cash plan:

* **Financing Estimates** (the "Treasury Announces Marketable Borrowing Estimates" release, a
  Monday): one sentence per quarter, e.g. "During the October–December 2026 quarter, Treasury
  expects to borrow $618 billion …, assuming an end-of-December cash balance of $850 billion."
* **Policy Statement** (the Quarterly Refunding Statement, that Wednesday): repeats the near
  quarter-end assumption and may project a peak, e.g. "… the Treasury General Account (TGA)
  could peak at $1.025 trillion (plus or minus $50 billion) in late November and late January."

Stored as FACT observations (Treasury's own stated assumptions; not forecasts by this project):

* ``TREASURY_CASH_ASSUMPTION``: one logical key per quarter end; value in billions of U.S.
  dollars. A later release that changes a quarter's assumption becomes a new revision.
* ``TREASURY_TGA_PEAK``: one key per (statement date, peak window); value is the projected peak
  in billions; the ± band, the window and Treasury's wording are in metadata. "Early/mid/late
  <month>" is mapped to days 1–15 / 10–20 / 16–end (an interpretation, recorded as
  ``window_rule``). The year is the next occurrence of that month on or after the statement date.
* ``TREASURY_REFUNDING_STATEMENT``: one key per statement date; value is how many peak windows it
  projected (0 when it projected none), so the newest statement's peaks can replace older ones.

Fails closed, before any database write, when anything is uncertain: a missing document link, a
link off home.treasury.gov, a missing release date, a cash sentence that does not parse, a
quarter whose last month differs from the stated end-of-month, the Policy Statement disagreeing
with the Financing Estimates, a peak sentence that does not parse, or a "most recent" refunding
older than ``MAX_AGE_DAYS`` (the page stopped updating). The ingest runner then marks the source
FAILED, the page says "Inspect Treasury." and the card keeps the last good plan.

Treasury's pages carry small per-request differences, so the raw HTML is stored only when a run
records something new (a new release or a revision), not on every run.
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
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

RECENT_URL = ("https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding/"
              "most-recent-quarterly-refunding-documents")
SOURCE_IDENTIFIER = "TREASURY_REFUNDING"
SOURCE_NAME = "U.S. Treasury quarterly refunding documents"
TARGET_SERIES = "TREASURY_CASH_ASSUMPTION"
PEAK_SERIES = "TREASURY_TGA_PEAK"
STATEMENT_SERIES = "TREASURY_REFUNDING_STATEMENT"
UNIT = "billion_us_dollars"
MAX_AGE_DAYS = 120          # refundings are about 91 days apart
TIMEOUT_SECONDS = 30.0
WINDOW_RULE = "early = days 1-15, mid = days 10-20, late = day 16 to month end (interpretation of Treasury's wording)"
MONTHS = {name: i for i, name in enumerate(calendar.month_name) if name}
_MONTH = "(" + "|".join(MONTHS) + ")"
_AMOUNT = r"\$([\d,]+(?:\.\d+)?)\s*(billion|trillion)"
TARGET_RE = re.compile(
    rf"During the {_MONTH}\s*-\s*{_MONTH} (\d{{4}}) quarter, Treasury expects to borrow .{{0,200}}?"
    rf"assuming an end-of-{_MONTH} cash balance of {_AMOUNT}")
STATEMENT_ASSUMPTION_RE = re.compile(rf"assuming an {_AMOUNT} cash balance at the end of {_MONTH}")
PEAK_RE = re.compile(
    rf"(?:TGA|Treasury General Account)\)? could peak at {_AMOUNT}\s*\(\s*(?:plus or minus|\+/-|±)\s*"
    rf"{_AMOUNT}\s*\)\s*in ([^.]+?)\.")
WINDOW_RE = re.compile(rf"\b(early|mid|late)[- ]{_MONTH}\b")
TIME_RE = re.compile(r"<time[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*</time>")


class RefundingError(RuntimeError):
    """Treasury's refunding documents could not be read with certainty."""


@dataclass(frozen=True)
class CashTarget:
    quarter_end: date
    billions: float
    quarter: str            # "October-December 2026"
    sentence: str


@dataclass(frozen=True)
class PeakWindow:
    label: str              # "late November"
    start: date
    end: date


@dataclass(frozen=True)
class Peak:
    billions: float
    band_billions: float
    windows: tuple[PeakWindow, ...]
    sentence: str


@dataclass
class RefundingPlan:
    estimates_url: str
    estimates_date: date
    targets: list[CashTarget]
    statement_url: str
    statement_date: date
    peak: Peak | None
    documents: dict[str, str] = field(default_factory=dict)   # url -> html
    retrieval_time: datetime = field(default_factory=lambda: datetime.now(UTC))


class _Links(HTMLParser):
    """Every <a href> with its visible text; tolerant of unquoted attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, _clean(" ".join(self._text))))
            self._href = None


def _clean(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[‒–—−]", "-", text)
    text = text.replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
    return _clean(re.sub(r"<[^>]+>", " ", page))


def _billions(number: str, scale: str) -> float:
    value = float(number.replace(",", ""))
    return value * 1000.0 if scale == "trillion" else value


def _release_date(page: str, url: str) -> date:
    match = TIME_RE.search(page)
    if not match:
        raise RefundingError(f"no release date found on {url}")
    month_name, day, year = match.group(1).replace(",", "").split()
    if month_name not in MONTHS:
        raise RefundingError(f"unreadable release date {match.group(1)!r} on {url}")
    return date(int(year), MONTHS[month_name], int(day))


def document_links(page: str) -> tuple[str, str]:
    """The Financing Estimates and Policy Statement links on the "most recent" page."""
    parser = _Links()
    parser.feed(page)
    found = {}
    for href, text in parser.links:
        for key, prefix in (("estimates", "Financing Estimates"), ("statement", "Policy Statement")):
            if key not in found and text.startswith(prefix):
                url = urljoin(RECENT_URL, href)
                if urlparse(url).netloc != "home.treasury.gov" or not urlparse(url).scheme == "https":
                    raise RefundingError(f"{prefix} link points off home.treasury.gov: {url}")
                found[key] = url
    missing = [name for name in ("Financing Estimates", "Policy Statement")
               if ("estimates" if name.startswith("F") else "statement") not in found]
    if missing:
        raise RefundingError(f"Treasury's most-recent refunding page has no {' or '.join(missing)} link")
    return found["estimates"], found["statement"]


def parse_estimates(page: str, url: str) -> tuple[date, list[CashTarget]]:
    released = _release_date(page, url)
    text = _text(page)
    targets = []
    for m in TARGET_RE.finditer(text):
        first, last, year, end_month, number, scale = m.groups()
        if end_month != last:
            raise RefundingError(f"{url}: the {first}-{last} {year} quarter states an end-of-{end_month} balance")
        y = int(year)
        quarter_end = date(y, MONTHS[last], calendar.monthrange(y, MONTHS[last])[1])
        targets.append(CashTarget(quarter_end, _billions(number, scale), f"{first}-{last} {year}", m.group(0)))
    mentions = len(re.findall(r"assuming an end-of-", text))
    if not targets:
        raise RefundingError(f"{url}: no quarter-end cash assumption could be read")
    if mentions != len(targets):
        raise RefundingError(f"{url}: {mentions} cash assumptions mentioned but {len(targets)} could be read")
    ends = [t.quarter_end for t in targets]
    if len(ends) != len(set(ends)):
        raise RefundingError(f"{url}: two assumptions for the same quarter end")
    return released, sorted(targets, key=lambda t: t.quarter_end)


def _window(label_kind: str, month_name: str, after: date) -> PeakWindow:
    month = MONTHS[month_name]
    year = after.year if month >= after.month else after.year + 1
    last = calendar.monthrange(year, month)[1]
    start, end = {"early": (1, 15), "mid": (10, 20), "late": (16, last)}[label_kind]
    return PeakWindow(f"{label_kind} {month_name}", date(year, month, start), date(year, month, end))


def parse_statement(page: str, url: str) -> tuple[date, Peak | None, list[tuple[date, float]]]:
    """Release date, projected peak (None when the statement projects none), and any restated assumptions."""
    released = _release_date(page, url)
    text = _text(page)
    restated = []
    for m in STATEMENT_ASSUMPTION_RE.finditer(text):
        number, scale, month_name = m.groups()
        month = MONTHS[month_name]
        year = released.year if month >= released.month else released.year + 1
        restated.append((date(year, month, calendar.monthrange(year, month)[1]), _billions(number, scale)))
    sentences = [s for s in re.split(r"(?<=[a-z)])\.\s", text)
                 if re.search(r"\bpeak\b", s) and re.search(r"TGA|Treasury General Account|cash balance", s)]
    if not sentences:
        return released, None, restated
    m = PEAK_RE.search(text)
    if not m:
        raise RefundingError(f"{url}: a TGA peak is mentioned but its level, band or timing could not be read")
    level_n, level_s, band_n, band_s, when = m.groups()
    windows = tuple(_window(kind, month, released) for kind, month in WINDOW_RE.findall(when))
    leftover = WINDOW_RE.sub("", when)
    if not windows or re.sub(r"\b(and|or|in|,)\b|[\s,]", "", leftover):
        raise RefundingError(f"{url}: could not read the peak timing {when!r}")
    return released, Peak(_billions(level_n, level_s), _billions(band_n, band_s), windows, m.group(0)), restated


def _get(url: str, opener: Callable[..., Any]) -> str:
    request = Request(url, headers={"User-Agent": os.environ.get("TFR_USER_AGENT", "treasury-flow-radar/0.1 (research)")})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            body = response.read()
    except HTTPError as exc:
        raise RefundingError(f"{url} returned HTTP {exc.code}") from None
    except (URLError, OSError) as exc:
        raise RefundingError(f"{url} could not be reached: {exc}") from None
    if not 200 <= status < 300:
        raise RefundingError(f"{url} returned HTTP {status}")
    return body.decode("utf-8", "replace")


def fetch_plan(*, opener: Callable[..., Any] = urlopen,
               clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> RefundingPlan:
    """Fetch and validate the current plan; raises RefundingError rather than return anything uncertain."""
    recent = _get(RECENT_URL, opener)
    estimates_url, statement_url = document_links(recent)
    estimates_html, statement_html = _get(estimates_url, opener), _get(statement_url, opener)
    estimates_date, targets = parse_estimates(estimates_html, estimates_url)
    statement_date, peak, restated = parse_statement(statement_html, statement_url)
    if not 0 <= (statement_date - estimates_date).days <= 10:
        raise RefundingError(f"the Policy Statement ({statement_date}) and Financing Estimates ({estimates_date}) "
                             "are not from the same refunding")
    stated = {t.quarter_end: t.billions for t in targets}
    for quarter_end, billions in restated:
        if quarter_end in stated and abs(stated[quarter_end] - billions) > 0.5:
            raise RefundingError(f"the Policy Statement assumes ${billions:,.0f}B at {quarter_end} but the Financing "
                                 f"Estimates say ${stated[quarter_end]:,.0f}B")
        if quarter_end not in stated:
            raise RefundingError(f"the Policy Statement assumes a {quarter_end} balance the Financing Estimates don't give")
    now = clock()
    if (now.date() - estimates_date).days > MAX_AGE_DAYS:
        raise RefundingError(f"Treasury's most-recent refunding page still shows the {estimates_date} estimates")
    return RefundingPlan(estimates_url, estimates_date, targets, statement_url, statement_date, peak,
                         {estimates_url: estimates_html, statement_url: statement_html}, now)


def _at(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def ingest_refunding(*, database_path: str | Path = DEFAULT_DB_PATH, plan: RefundingPlan | None = None) -> dict[str, int]:
    plan = plan or fetch_plan()
    initialize_database(database_path)
    records: list[dict[str, Any]] = []
    for t in plan.targets:
        records.append({"series": TARGET_SERIES, "key": t.quarter_end.isoformat(), "time": _at(t.quarter_end),
                        "published": plan.estimates_date, "value": t.billions, "canonical": f"{t.billions:g}",
                        "url": plan.estimates_url,
                        "metadata": {"quarter": t.quarter, "quarter_end": t.quarter_end.isoformat(),
                                     "announced": plan.estimates_date.isoformat(), "url": plan.estimates_url,
                                     "sentence": t.sentence}})
    windows = plan.peak.windows if plan.peak else ()
    for w in windows:
        assert plan.peak is not None
        records.append({"series": PEAK_SERIES, "key": f"{plan.statement_date.isoformat()}:{w.start.isoformat()}",
                        "time": _at(w.start), "published": plan.statement_date, "value": plan.peak.billions,
                        "canonical": f"{plan.peak.billions:g}+-{plan.peak.band_billions:g} {w.label}",
                        "url": plan.statement_url,
                        "metadata": {"label": w.label, "window_start": w.start.isoformat(),
                                     "window_end": w.end.isoformat(), "band_bn": plan.peak.band_billions,
                                     "announced": plan.statement_date.isoformat(), "url": plan.statement_url,
                                     "window_rule": WINDOW_RULE, "sentence": plan.peak.sentence}})
    records.append({"series": STATEMENT_SERIES, "key": plan.statement_date.isoformat(), "time": _at(plan.statement_date),
                    "published": plan.statement_date, "value": float(len(windows)), "canonical": str(len(windows)),
                    "url": plan.statement_url,
                    "metadata": {"announced": plan.statement_date.isoformat(), "url": plan.statement_url,
                                 "peaks": len(windows)}})
    descriptions = {
        TARGET_SERIES: ("Treasury quarter-end cash balance assumption",
                        "Treasury's assumed end-of-quarter cash balance from its Financing Estimates, billions of USD."),
        PEAK_SERIES: ("Treasury projected TGA peak",
                      ("Treasury's projected Treasury General Account peak from its Quarterly Refunding "
                       "Statement, billions of USD; band and window in metadata.")),
        STATEMENT_SERIES: ("Treasury refunding statement marker",
                           ("One row per Quarterly Refunding Statement read; value is the number of peak "
                            "windows.")),
    }
    inserted = unchanged = 0
    with database(database_path) as conn:
        source_id = register_source(conn, identifier=SOURCE_IDENTIFIER, name=SOURCE_NAME, source_type="government",
                                    url=RECENT_URL, metadata={"documents": "Financing Estimates, Policy Statement"})
        series_ids = {name: register_series(conn, source_id=source_id, identifier=name, name=title, description=text,
                                            instrument_type="government_cash_plan", frequency="quarterly",
                                            default_unit=UNIT if name != STATEMENT_SERIES else "count",
                                            metadata={"provider": "U.S. Treasury"})
                      for name, (title, text) in descriptions.items()}
        raw_ids: dict[str, int] = {}
        for record in records:
            versions = get_observations(conn, source_id=source_id, series_id=series_ids[record["series"]],
                                        logical_key=record["key"])
            latest = max(versions, key=lambda v: v["revision"]) if versions else None
            if latest is not None and latest["raw_value"] == record["canonical"]:
                unchanged += 1
                continue
            url = record["url"]
            if url not in raw_ids:   # store the document only when it adds something new
                raw_ids[url] = insert_raw_record(conn, source_id=source_id, external_record_id=url,
                                                 payload=plan.documents.get(url, ""), content_type="text/html",
                                                 retrieval_time=plan.retrieval_time, metadata={"url": url})
            insert_observation(
                conn, source_id=source_id, series_id=series_ids[record["series"]], logical_key=record["key"],
                revision=1 if latest is None else latest["revision"] + 1,
                revision_of_id=None if latest is None else latest["id"],
                observation_time=record["time"], publication_time=_at(record["published"]),
                retrieval_time=plan.retrieval_time, value_numeric=record["value"], value_text=None,
                unit=UNIT if record["series"] != STATEMENT_SERIES else "count", raw_value=record["canonical"],
                raw_record_id=raw_ids[url], metadata=record["metadata"])
            inserted += 1
    return {"inserted": inserted, "unchanged": unchanged, "missing": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read Treasury's cash plan from its refunding documents.")
    parser.add_argument("--database", default=os.environ.get("TREASURY_FLOW_RADAR_DB", DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true", help="fetch and print the plan without writing")
    args = parser.parse_args(argv)
    if args.dry_run:
        plan = fetch_plan()
        print(json.dumps({
            "estimates": [plan.estimates_url, plan.estimates_date.isoformat()],
            "statement": [plan.statement_url, plan.statement_date.isoformat()],
            "targets": [[t.quarter_end.isoformat(), t.billions, t.quarter] for t in plan.targets],
            "peak": None if plan.peak is None else {
                "billions": plan.peak.billions, "band": plan.peak.band_billions,
                "windows": [[w.label, w.start.isoformat(), w.end.isoformat()] for w in plan.peak.windows]},
        }, indent=1))
        return 0
    print(json.dumps(ingest_refunding(database_path=args.database)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
