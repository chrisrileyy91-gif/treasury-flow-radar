from __future__ import annotations

import io
import re
import sqlite3
import zipfile
from datetime import UTC, date, datetime, timedelta

import pytest

from dashboard import for_you as fy
from dashboard.overview import overview, stock_risk_lines
from treasury_flow_radar.analytics.stock_risk import stock_risk, vix_band
from treasury_flow_radar.sources.damodaran_erp import (
    ErpError,
    ErpWorkbook,
    fetch_workbook,
    ingest_erp,
    parse_workbook,
)

BANNED = re.compile(r"\b(buy|sell now|should sell|should buy|reduce|trim|exit|hedge your|price target)\b",
                    re.IGNORECASE)
HEADERS = ["Start of month", "S&P 500", "T.Bond Rate", "ERP (T12 m with sustainable payout)", "ERP (T12m)"]


def _xlsx(rows, *, date1904=True, headers=HEADERS, sheet="Historical ERP"):
    """A minimal real .xlsx: shared-string headers, numeric cells, Damodaran's sheet layout."""
    strings = list(headers)
    cols = "ABCDEFGHIJ"

    def cell(ref, value):
        if value is None:
            return ""
        if isinstance(value, str):
            if value not in strings:
                strings.append(value)
            return f'<c r="{ref}" t="s"><v>{strings.index(value)}</v></c>'
        return f'<c r="{ref}"><v>{value!r}</v></c>'
    body = ['<row r="1">' + "".join(cell(f"{cols[i]}1", h) for i, h in enumerate(headers)) + "</row>"]
    for n, row in enumerate(rows, start=2):
        body.append(f'<row r="{n}">' + "".join(cell(f"{cols[i]}{n}", v) for i, v in enumerate(row)) + "</row>")
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><workbookPr{" date1904=\"1\"" if date1904 else ""}/>'
                   f'<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s.replace('&', '&amp;')}</t></si>"
                                                                   for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(body)}</sheetData></worksheet>')
    return out.getvalue()


def _serial1904(day):
    return (day - date(1904, 1, 1)).days


# Values copied from the live workbook's first and latest rows (probe run 2026-11-10).
LIVE_ROWS = [
    [_serial1904(date(2008, 9, 1)), 1252, 0.0372, None, 0.0422],
    [_serial1904(date(2026, 10, 1)), 7652, 0.0529, 0.042, 0.037],
    [_serial1904(date(2026, 11, 1)), 7580, 0.0494, 0.0441, 0.0379],
]


class _Response:
    def __init__(self, body):
        self.body, self.status = body, 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def test_parser_honors_1904_dates_and_keeps_exact_cells():
    rows, system = parse_workbook(_xlsx(LIVE_ROWS))
    assert system == "1904"
    assert [r.month for r in rows] == [date(2008, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]
    latest = rows[-1].values
    assert latest["DAMODARAN_ERP"] == ("0.0441", pytest.approx(0.0441))
    assert latest["DAMODARAN_TBOND"][1] == pytest.approx(0.0494)
    assert "DAMODARAN_ERP" not in rows[0].values          # blank cell stays missing, not filled from T12m
    assert rows[0].values["DAMODARAN_ERP_T12M"][1] == pytest.approx(0.0422)


def test_parser_reads_1900_dates_when_the_flag_is_absent():
    serial = (date(2026, 11, 1) - date(1899, 12, 30)).days
    rows, system = parse_workbook(_xlsx([[serial, 7580, 0.0494, 0.0441, 0.0379]], date1904=False))
    assert system == "1900" and rows[0].month == date(2026, 11, 1)


def test_parser_fails_closed_on_layout_changes():
    with pytest.raises(ErpError, match="missing expected column"):
        parse_workbook(_xlsx(LIVE_ROWS, headers=[*HEADERS[:3], "ERP (renamed)", HEADERS[4]]))
    with pytest.raises(ErpError, match="no sheet named"):
        parse_workbook(_xlsx(LIVE_ROWS, sheet="Other"))
    with pytest.raises(ErpError, match="not a readable"):
        parse_workbook(b"<html>not a workbook</html>")
    with pytest.raises(ErpError, match="conflicting"):
        parse_workbook(_xlsx([LIVE_ROWS[2], [*LIVE_ROWS[2][:3], 0.05, 0.0379]]))


def test_fetch_rejects_a_misread_date_system():
    old = (date(2022, 10, 31) - date(1899, 12, 30)).days   # what the 1904 file looks like read as 1900
    body = _xlsx([[old, 7580, 0.0494, 0.0441, 0.0379]], date1904=False)
    with pytest.raises(ErpError, match="implausible"):
        fetch_workbook(opener=lambda req, timeout: _Response(body),
                       clock=lambda: datetime(2026, 11, 10, tzinfo=UTC))
    ok = fetch_workbook(opener=lambda req, timeout: _Response(_xlsx(LIVE_ROWS)),
                        clock=lambda: datetime(2026, 11, 10, tzinfo=UTC))
    assert ok.rows[-1].month == date(2026, 11, 1)


def test_ingest_is_idempotent_and_records_revisions(tmp_path):
    db = tmp_path / "t.sqlite3"
    when = datetime(2026, 11, 10, tzinfo=UTC)
    rows, system = parse_workbook(_xlsx(LIVE_ROWS))
    first = ingest_erp(database_path=db, workbook=ErpWorkbook(rows, system, b"x", when))
    assert first["inserted"] == 8                                   # 2 + 3 + 3 non-blank cells
    again = ingest_erp(database_path=db, workbook=ErpWorkbook(rows, system, b"x", when))
    assert again == {"inserted": 0, "unchanged": 8, "missing": 0}
    revised = [list(r) for r in LIVE_ROWS]
    revised[2][3] = 0.0445
    rows2, _ = parse_workbook(_xlsx(revised))
    third = ingest_erp(database_path=db, workbook=ErpWorkbook(rows2, system, b"y", when + timedelta(days=1)))
    assert third["inserted"] == 1
    conn = sqlite3.connect(db)
    revs = conn.execute("""select o.revision, o.raw_value, o.revision_of_id is not null from observations o
        join series s on s.id=o.series_id where s.identifier='DAMODARAN_ERP' and o.logical_key='2026-11-01'
        order by o.revision""").fetchall()
    assert revs == [(1, "0.0441", 0), (2, "0.0445", 1)]


def _obs(series, points):
    return [{"series_identifier": series, "observation_time": d.isoformat(), "value_numeric": v} for d, v in points]


def _days(n, end=date(2026, 11, 9)):
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def _risk(vix_level, *, funding=False, vol=False, hy_change=0.0, crowded=False, erp=True):
    rows = _obs("VIXCLS", [(d, 15.0) for d in _days(299, date(2026, 11, 8))] + [(date(2026, 11, 9), vix_level)])
    rows += _obs("BAMLH0A0HYM2", [(d, 3.0) for d in _days(29, date(2026, 11, 8))] +
                 [(date(2026, 11, 9), 3.0 + hy_change / 100)])
    if erp:
        months = [date(2008 + (i + 8) // 12, (i + 8) % 12 + 1, 1) for i in range(218)]
        rows += _obs("DAMODARAN_ERP", [(m, 0.05 + 0.0001 * (i % 30)) for i, m in enumerate(months)]
                     + [(date(2026, 11, 1), 0.0441)])
        rows += _obs("DAMODARAN_TBOND", [(date(2026, 11, 1), 0.0494)])
    basis = {"state": {"funding_tight": funding, "vol_elevated": vol, "crowded": crowded}}
    return stock_risk(rows, basis)


@pytest.mark.parametrize(("vix", "kw", "state"), [
    (14.0, {}, "quiet"),
    (24.0, {}, "fear_elsewhere"),
    (16.0, {"funding": True}, "early_warning"),
    (16.0, {"funding": True, "hy_change": 60.0}, "unpriced_stress"),
    (22.0, {"funding": True, "vol": True}, "together"),
])
def test_each_combination_of_pipes_and_nerves(vix, kw, state):
    assert _risk(vix, **kw)["state"] == state


def test_credit_light_uses_the_widening_rule_and_missing_inputs_are_partial():
    risk = _risk(16.0, hy_change=49.0)
    assert risk["credit"]["widening"] is False and risk["bond"]["lights_on"] == 0
    assert stock_risk(_obs("VIXCLS", [(date(2026, 11, 9), 18.0)]), {"state": {}})["state"] == "partial"
    assert stock_risk([], {}) is None


def test_vix_bands_and_erp_ranking():
    assert [vix_band(v) for v in (14.9, 15.0, 19.99, 20.0, 29.9, 30.0)] == [
        "calm", "normal", "normal", "nervous", "nervous", "fearful"]
    erp = _risk(16.0)["erp"]
    assert erp["erp_percent"] == pytest.approx(4.41) and erp["band"] == "thin" and erp["percentile"] < 1
    assert erp["implied_return_percent"] == pytest.approx(9.35)
    assert erp["history_start"] == "2008-09-01"


def test_card_explains_what_is_happening_on_each_side():
    risk = _risk(16.0, funding=True, hy_change=60.0, crowded=True)
    html = overview({"stock_risk": risk})
    assert 'id="g-stocks"' in html and "Stress in the pipes that stocks haven&#x27;t priced." in html
    assert "What's happening" in html and "What this means for you" in html
    assert html.count('class="g-you-s"') == 3                       # sector lines when stress is unpriced
    lines = " ".join(stock_risk_lines(risk))
    assert "2 of 3 lights on" in lines and "funding tight" in lines and "(widening)" in lines
    assert "VIX <b>16.0</b> (normal)" in lines and "Risk premium</b>: 4.41%" in lines
    assert "about 9.3% a year" in lines or "about 9.4% a year" in lines
    assert "crowded" in lines
    assert 'id="g-stocks"' not in overview({"stock_risk": None})


def test_quiet_card_has_no_sector_lines_and_no_text_gives_trade_instructions():
    html = overview({"stock_risk": _risk(14.0)})
    assert "Quiet on both sides." in html and 'class="g-you-s"' not in html
    texts = [t for head, body, sectors in fy.STOCK_STATE.values() for t in (head, body, *sectors.values())]
    texts += [fy.erp_you({"erp_percent": 4.4, "band": b}) for b in ("thin", "wide", "middle", None)]
    assert texts and not [t for t in texts if BANNED.search(t)]
