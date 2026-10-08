"""Cross-market context for each session in the window (OBSERVATION and CALCULATION).

Series (FRED): S&P 500 (SP500), investment-grade and high-yield option-adjusted spreads
(BAMLC0A0CM, BAMLH0A0HYM2), and the Fed's broad dollar index (DTWEXBGS).

For each session the day's change is compared with the typical daily change of that
series over stored history before the window (one standard deviation). A move larger
than that is "unusual". The fingerprint is a deterministic reading of signs only:

* flight to safety: the 10-year fell, and stocks fell AND high-yield spreads widened unusually;
* risk appetite: the 10-year rose, and stocks rose AND high-yield spreads tightened unusually.

Anything else is described without a label. These are patterns consistent with a story,
never evidence of who traded or why.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from statistics import pstdev
from typing import Any

SERIES = {
    "SP500": {"label": "S&P 500", "short": "S&P", "kind": "percent"},
    "BAMLH0A0HYM2": {"label": "High-yield spread", "short": "HY", "kind": "bp"},
    "BAMLC0A0CM": {"label": "Investment-grade spread", "short": "IG", "kind": "bp"},
    "DTWEXBGS": {"label": "Broad dollar", "short": "USD", "kind": "percent"},
}
MIN_HISTORY = 30


def _changes(levels: Mapping[date, float], kind: str) -> dict[date, float]:
    days = sorted(levels)
    out = {}
    for i, d in enumerate(days[1:], 1):
        prev = levels[days[i - 1]]
        if kind == "percent":
            out[d] = (levels[d] / prev - 1) * 100 if prev else None
        else:
            out[d] = (levels[d] - prev) * 100           # percent points -> basis points
    return {d: v for d, v in out.items() if v is not None}


def typical_change(levels: Mapping[date, float], kind: str, before: date) -> float | None:
    history = [v for d, v in _changes(levels, kind).items() if d < before]
    return pstdev(history) if len(history) >= MIN_HISTORY else None


def market_context(*, ten_year: Mapping[date, float], market: Mapping[str, Mapping[date, float]],
                   window: list[date]) -> dict[str, Any]:
    if not window:
        return {"status": "INSUFFICIENT DATA", "sessions": [], "series": []}
    available = [sid for sid in SERIES if market.get(sid)]
    if not available:
        return {"status": "UNAVAILABLE", "sessions": [], "series": []}
    changes = {sid: _changes(market[sid], SERIES[sid]["kind"]) for sid in available}
    typical = {sid: typical_change(market[sid], SERIES[sid]["kind"], window[0]) for sid in available}
    ten_days = sorted(ten_year)
    rows = []
    for d in window:
        i = ten_days.index(d) if d in ten_days else None
        ten_change = None if not i else (ten_year[d] - ten_year[ten_days[i - 1]]) * 100
        cells = {}
        for sid in available:
            value = changes[sid].get(d)
            norm = typical[sid]
            cells[sid] = {"change": value, "typical": norm,
                          "unusual": None if value is None or not norm else abs(value) > norm}
        rows.append({"date": d.isoformat(), "ten_year_bps": ten_change, "cells": cells,
                     "read": _read(ten_change, cells)})
    latest = {sid: max(market[sid]).isoformat() for sid in available}
    return {"status": "AVAILABLE", "series": [{"id": sid, **SERIES[sid], "latest": latest[sid],
                                               "typical": typical[sid]} for sid in available],
            "missing": [sid for sid in SERIES if sid not in available], "sessions": rows,
            "evidence_type": "OBSERVATION"}


def _read(ten: float | None, cells: Mapping[str, Mapping[str, Any]]) -> str | None:
    stocks, hy = cells.get("SP500") or {}, cells.get("BAMLH0A0HYM2") or {}
    if ten is None or stocks.get("change") is None or hy.get("change") is None:
        return None
    stocks_down, stocks_up = stocks["unusual"] and stocks["change"] < 0, stocks["unusual"] and stocks["change"] > 0
    wider, tighter = hy["unusual"] and hy["change"] > 0, hy["unusual"] and hy["change"] < 0
    if ten < -0.5 and stocks_down and wider:
        return "Consistent with a flight to safety: yields fell while stocks fell and high-yield spreads widened more than usual."
    if ten > 0.5 and stocks_up and tighter:
        return "Consistent with risk appetite: yields rose while stocks rallied and high-yield spreads tightened more than usual."
    unusual = [_describe(sid, c["change"]) for sid, c in cells.items() if c.get("unusual")]
    if not unusual:
        return "Stocks, credit and the dollar were within their usual daily range; the move looks rates-specific."
    return ("Unusual moves elsewhere: " + "; ".join(unusual)
            + ". Not a clean flight-to-safety or risk-appetite pattern.")


def _describe(sid: str, change: float) -> str:
    if sid == "SP500":
        return f"stocks {'rose' if change > 0 else 'fell'} {abs(change):.1f}%"
    if sid == "DTWEXBGS":
        return f"the dollar {'rose' if change > 0 else 'fell'} {abs(change):.1f}%"
    name = "high-yield" if sid == "BAMLH0A0HYM2" else "investment-grade"
    return f"{name} spreads {'widened' if change > 0 else 'tightened'} {abs(change):.0f} bp"
