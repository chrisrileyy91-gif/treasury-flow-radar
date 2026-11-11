"""Damodaran monthly implied equity risk premium (ERP) for the S&P 500.

Source: Aswath Damodaran (NYU Stern), ``ERPbymonth.xlsx`` on his data pages
(https://pages.stern.nyu.edu/~adamodar/), one row per start of month since September 2008.
The implied ERP is the expected return on the S&P 500, solved from index level, cash flows and
growth, minus the 10-year Treasury rate: the extra return investors are pricing in for owning
stocks. It is Damodaran's model estimate (a published CALCULATION of his), stored as reported.

Parsing notes, each checked against the live file:

* The workbook is parsed with the standard library (zip + XML); no spreadsheet dependency.
* The sheet is found by its name, ``Historical ERP``, and columns by their header text, never by
  letter position. A missing sheet or header fails the fetch before any database write.
* The workbook uses Excel's **1904 date system** (``workbookPr date1904="1"``). Read with the
  1900 system the latest row would look four years old; the flag is honored, and a workbook
  without it is read with the 1900 system.
* Values are decimal fractions (0.0441 = 4.41%), stored unchanged in unit ``decimal_fraction``;
  the exact cell text is kept as ``raw_value``. Blank cells (early rows lack some variants) stay
  missing; nothing is filled from another column.
* The complete workbook is stored base64-encoded in ``raw_records`` for audit and reprocessing.
* The row date is the start of the month the estimate describes. Damodaran does not publish an
  exact release time, so ``publication_time`` is NULL; a changed value is a new revision.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
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

WORKBOOK_URL = "https://pages.stern.nyu.edu/~adamodar/pc/implprem/ERPbymonth.xlsx"
SOURCE_PAGE = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/home.htm"
SOURCE_IDENTIFIER = "DAMODARAN"
SOURCE_NAME = "Aswath Damodaran, NYU Stern: implied equity risk premium"
SHEET_NAME = "Historical ERP"
DATE_HEADER = "Start of month"
UNIT = "decimal_fraction"
TIMEOUT_SECONDS = 60.0
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64"
# Header text in the workbook -> internal series identifier and description.
METRICS: dict[str, dict[str, str]] = {
    "ERP (T12 m with sustainable payout)": {
        "series": "DAMODARAN_ERP",
        "name": "Implied equity risk premium (trailing 12 months, sustainable payout)",
        "description": "Damodaran's headline monthly implied ERP for the S&P 500: expected return implied "
                       "by index level, trailing cash flows adjusted to a sustainable payout and expected "
                       "growth, minus the 10-year Treasury rate. Decimal fraction.",
    },
    "ERP (T12m)": {
        "series": "DAMODARAN_ERP_T12M",
        "name": "Implied equity risk premium (trailing 12-month cash flows)",
        "description": "Implied ERP using unadjusted trailing 12-month cash flows; the longer-running "
                       "variant (available from 2008). Decimal fraction.",
    },
    "T.Bond Rate": {
        "series": "DAMODARAN_TBOND",
        "name": "10-year Treasury rate used in Damodaran's ERP",
        "description": "The 10-year Treasury rate at the start of the month, as used in the ERP "
                       "calculation. Decimal fraction.",
    },
}

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_EPOCH_1900 = date(1899, 12, 30)   # Excel's 1900 system, including its 1900 leap-year quirk
_EPOCH_1904 = date(1904, 1, 1)


class ErpError(RuntimeError):
    """The ERP workbook could not be fetched or did not have the expected shape."""


@dataclass(frozen=True)
class ErpRow:
    month: date
    values: dict[str, tuple[str, float]]   # series identifier -> (exact cell text, value)


@dataclass(frozen=True)
class ErpWorkbook:
    rows: tuple[ErpRow, ...]
    date_system: str
    raw_bytes: bytes
    retrieval_time: datetime


def _column(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha())


def _excel_date(serial: str, date1904: bool) -> date:
    days = float(serial)
    if days != int(days):
        raise ErpError(f"date cell {serial!r} is not a whole day")
    return (_EPOCH_1904 if date1904 else _EPOCH_1900) + timedelta(days=int(days))


def parse_workbook(data: bytes) -> tuple[tuple[ErpRow, ...], str]:
    """Rows of the Historical ERP sheet, oldest first, and the date system used."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ErpError("ERP download is not a readable .xlsx workbook") from exc
    props = workbook.find("m:workbookPr", _NS)
    date1904 = props is not None and props.get("date1904") in ("1", "true")
    sheets = workbook.find("m:sheets", _NS)
    sheet = next((s for s in (sheets if sheets is not None else []) if s.get("name") == SHEET_NAME), None)
    if sheet is None:
        raise ErpError(f"ERP workbook has no sheet named {SHEET_NAME!r}")
    rel_id = sheet.get(f"{{{_NS['r']}}}id")
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    target = next((r.get("Target") for r in rels.findall("rel:Relationship", _NS) if r.get("Id") == rel_id), None)
    if not target:
        raise ErpError(f"ERP workbook sheet {SHEET_NAME!r} has no part")
    part = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    shared: list[str] = []
    if "xl/sharedStrings.xml" in book.namelist():
        for item in ET.fromstring(book.read("xl/sharedStrings.xml")).findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in item.iter(f"{{{_NS['m']}}}t")))
    sheet_rows = ET.fromstring(book.read(part)).find("m:sheetData", _NS)
    if sheet_rows is None:
        raise ErpError("ERP sheet has no data")

    def cells(row: ET.Element) -> dict[str, str]:
        out = {}
        for cell in row.findall("m:c", _NS):
            value = cell.find("m:v", _NS)
            if value is None or value.text is None:
                inline = cell.find("m:is", _NS)
                if inline is None:
                    continue
                text = "".join(t.text or "" for t in inline.iter(f"{{{_NS['m']}}}t"))
            else:
                text = shared[int(value.text)] if cell.get("t") == "s" else value.text
            out[_column(cell.get("r", ""))] = text
        return out

    all_rows = sheet_rows.findall("m:row", _NS)
    if not all_rows:
        raise ErpError("ERP sheet is empty")
    header = {text.strip(): col for col, text in cells(all_rows[0]).items()}
    missing = [h for h in (DATE_HEADER, *METRICS) if h not in header]
    if missing:
        raise ErpError(f"ERP sheet is missing expected column(s): {', '.join(missing)}")
    parsed: dict[date, ErpRow] = {}
    for row in all_rows[1:]:
        values = cells(row)
        raw_date = values.get(header[DATE_HEADER], "").strip()
        if not raw_date:
            continue
        try:
            month = _excel_date(raw_date, date1904)
        except ValueError:
            continue   # a note or trailer row, not a dated observation
        found = {}
        for heading, meta in METRICS.items():
            text = values.get(header[heading], "").strip()
            if not text:
                continue
            try:
                found[meta["series"]] = (text, float(text))
            except ValueError as exc:
                raise ErpError(f"ERP cell for {heading!r} on {month} is not numeric: {text!r}") from exc
        if month in parsed and parsed[month].values != found:
            raise ErpError(f"ERP sheet has conflicting rows for {month}")
        parsed[month] = ErpRow(month, found)
    if not parsed:
        raise ErpError("ERP sheet has no dated rows")
    # Guard against a misread date system: the series must end near the present.
    return tuple(parsed[m] for m in sorted(parsed)), "1904" if date1904 else "1900"


def fetch_workbook(*, opener: Callable[..., Any] = urlopen,
                   clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> ErpWorkbook:
    request = Request(WORKBOOK_URL, headers={
        "User-Agent": os.environ.get("TFR_USER_AGENT", "treasury-flow-radar/0.1 (research)")})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            body = response.read()
    except HTTPError as exc:
        raise ErpError(f"ERP workbook request failed with status {exc.code}") from None
    except (URLError, OSError) as exc:
        raise ErpError(f"ERP workbook network request failed: {exc}") from None
    if status < 200 or status >= 300:
        raise ErpError(f"ERP workbook request failed with status {status}")
    retrieved = clock()
    rows, system = parse_workbook(body)
    latest = rows[-1].month
    if latest < retrieved.date() - timedelta(days=400) or latest > retrieved.date() + timedelta(days=45):
        raise ErpError(f"ERP workbook's latest month {latest} is implausible for {retrieved.date()}; "
                       "the date system may have been misread")
    return ErpWorkbook(rows, system, body, retrieved)


def ingest_erp(*, database_path: str | Path = DEFAULT_DB_PATH, start_date: date | None = None,
               end_date: date | None = None, workbook: ErpWorkbook | None = None) -> dict[str, int]:
    """Fetch and validate the whole workbook first, then persist in one transaction."""
    book = workbook or fetch_workbook()
    initialize_database(database_path)
    inserted = unchanged = 0
    with database(database_path) as conn:
        source_id = register_source(conn, identifier=SOURCE_IDENTIFIER, name=SOURCE_NAME,
                                    source_type="research_estimate", url=SOURCE_PAGE,
                                    metadata={"workbook": WORKBOOK_URL})
        series_ids = {
            meta["series"]: register_series(
                conn, source_id=source_id, identifier=meta["series"], name=meta["name"],
                description=meta["description"], instrument_type="equity_risk_premium"
                if meta["series"] != "DAMODARAN_TBOND" else "treasury_yield",
                frequency="monthly", default_unit=UNIT,
                metadata={"provider": "Damodaran", "workbook_header": heading, "workbook": WORKBOOK_URL,
                          "observation_precision": "calendar_month_start"})
            for heading, meta in METRICS.items()}
        raw_id = insert_raw_record(
            conn, source_id=source_id, external_record_id="ERPbymonth.xlsx",
            payload=base64.b64encode(book.raw_bytes).decode("ascii"), content_type=CONTENT_TYPE,
            retrieval_time=book.retrieval_time,
            metadata={"url": WORKBOOK_URL, "sheet": SHEET_NAME, "date_system": book.date_system,
                      "encoding": "base64"})
        for row in book.rows:
            if start_date and row.month < start_date or end_date and row.month > end_date:
                continue
            for series, (text, value) in row.values.items():
                key = row.month.isoformat()
                versions = get_observations(conn, source_id=source_id, series_id=series_ids[series],
                                            logical_key=key)
                latest = max(versions, key=lambda v: v["revision"]) if versions else None
                if latest is not None and latest["raw_value"] == text:
                    unchanged += 1
                    continue
                insert_observation(
                    conn, source_id=source_id, series_id=series_ids[series], logical_key=key,
                    revision=1 if latest is None else latest["revision"] + 1,
                    revision_of_id=None if latest is None else latest["id"],
                    observation_time=datetime.combine(row.month, time.min, tzinfo=UTC),
                    publication_time=None, retrieval_time=book.retrieval_time, value_numeric=value,
                    value_text=None, unit=UNIT, raw_value=text, raw_record_id=raw_id,
                    metadata={"provider": "Damodaran", "series_id": series, "date_system": book.date_system,
                              "observation_precision": "calendar_month_start"})
                inserted += 1
    return {"inserted": inserted, "unchanged": unchanged, "missing": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=os.environ.get("TREASURY_FLOW_RADAR_DB", DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    print(json.dumps(ingest_erp(database_path=args.database)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
