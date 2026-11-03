"""Every reading's "What this means for you" line: states, good news included, and no trade advice."""
from __future__ import annotations

import re

from dashboard import for_you as fy

BANNED = re.compile(r"\b(buy|sell now|should sell|should buy|reduce|trim|exit|hedge your|price target)\b", re.IGNORECASE)


def _hist(values):
    return [{"date": f"2026-01-{i:02d}", "value": v} for i, v in enumerate(values, 1)]


def _all_texts(entry):
    return [] if entry is None else [entry[0], *entry[1].values()]


def test_level_high_low_and_middle():
    high = fy.level_you({"percentile": 93.0, "sessions": 516}, -6.0)
    assert "headwind" in high[0] and "past 2 years'" in high[0] and set(high[1]) == {"AI & software", "Chips/semis", "Energy"}
    low = fy.level_you({"percentile": 10.0, "sessions": 516}, 12.0)
    assert "tailwind" in low[0] and "rose 12 bp" in low[0] and low[1]
    mid = fy.level_you({"percentile": 50.0, "sessions": 516}, -15.0)
    assert "middle" in mid[0] and "fell 15 bp" in mid[0] and mid[1] == {}
    assert fy.level_you(None, None) is None


def test_drivers_names_top_fit_and_warns_when_mostly_unexplained():
    cand = {"bars": [{"name": "Jobless claims", "share": 0.07}, {"name": "Unexplained", "share": 0.83, "unexplained": True}],
            "items": [{"name": "Jobless claims", "type": "macro_release"}]}
    text = fy.drivers_you(cand)[0]
    assert "Jobless claims (about 7%)" in text and "83%" in text and "expects the Fed" in text and "be wary" in text
    calm = fy.drivers_you({"bars": [{"name": "CPI", "share": 0.7}, {"name": "Unexplained", "share": 0.3, "unexplained": True}]})
    assert "be wary" not in calm[0]


def test_dealer_full_light_and_normal():
    hist = _hist(range(100, 152))
    assert "shelves full" in fy.dealer_you({"position_bn": 150.0, "history": hist})[0]
    assert "room to absorb" in fy.dealer_you({"position_bn": 101.0, "history": hist})[0]
    assert "normal amount" in fy.dealer_you({"position_bn": 125.0, "history": hist})[0]


def test_market_credit_widening_tightening_and_steady():
    def ctx(hy):
        return {"status": "AVAILABLE", "sessions": [{"ten_year_bps": 2.0, "cells": {"SP500": {"change": -1.0},
                                                                                     "BAMLH0A0HYM2": {"change": hy}}}]}
    assert "getting nervous" in fy.market_you(ctx(25.0))[0]
    assert "lenders are relaxed" in fy.market_you(ctx(-25.0))[0]
    steady = fy.market_you(ctx(3.0))[0]
    assert "steady" in steady and "high-yield" in steady and "−1.0%" in steady   # hyphens kept, minus is true minus


def test_market_flags_a_widening_latest_day_hidden_by_the_weekly_total():
    ctx = {"status": "AVAILABLE", "sessions": [
        {"date": "2026-10-29", "ten_year_bps": -2.0, "cells": {"BAMLH0A0HYM2": {"change": -6.0}}},
        {"date": "2026-10-30", "ten_year_bps": -4.0, "cells": {"BAMLH0A0HYM2": {"change": 12.0, "unusual": True}}}]}
    text = fy.market_you(ctx)[0]
    assert "latest session (Oct 30)" in text and "flight-to-safety" in text and "steady" not in text


def test_channel_real_vs_breakeven_and_flat():
    real = fy.channel_you({"five": {"nominal_bps": -6.0, "real_bps": -6.0, "breakeven_bps": 0.0}})[0]
    assert "real yields" in real and "tailwind" in real
    up = fy.channel_you({"five": {"nominal_bps": 9.0, "real_bps": 8.0, "breakeven_bps": 1.0}})[0]
    assert "headwind" in up
    be = fy.channel_you({"five": {"nominal_bps": 8.0, "real_bps": 1.0, "breakeven_bps": 7.0}})[0]
    assert "inflation expectations" in be and "energy" in be
    assert "barely moved" in fy.channel_you({"five": {"nominal_bps": 1.0, "real_bps": 1.0, "breakeven_bps": 0.0}})[0]


def test_supply_never_calls_a_past_auction_next():
    auctions = [{"date": "2026-10-21", "security": "20-Year Bond", "offering_usd": 13e9, "upcoming": True},
                {"date": "2026-11-10", "security": "10-Year Note", "offering_usd": 42e9, "upcoming": True}]
    text = fy.supply_you(auctions, "2026-11-03T01:00:00Z")[0]
    assert "10-Year Note $42B on Nov 10" in text and "20-Year" not in text
    assert "Next up" not in fy.supply_you(auctions[:1], "2026-11-03")[0]


def test_futures_and_read_and_events_have_lines_in_every_state():
    for verdict in ["crowded", "large", "typical", "small", ""]:
        assert fy.futures_you(verdict)[0].startswith("This table is where the basis trade lives")
    for tone in ["quiet", "watch", "strain", "partial"]:
        assert "not a buy or sell signal" in fy.read_you(tone)[0]
    assert "5 bp or more" in fy.events_you(5)[0]


def test_no_line_anywhere_gives_trade_instructions():
    entries = [*fy.FUNDING.values(), *fy.CROWDING.values(), *fy.VOLATILITY.values(),
               fy.level_you({"percentile": 93.0, "sessions": 516}, -12.0),
               fy.level_you({"percentile": 5.0, "sessions": 516}, 12.0),
               fy.drivers_you({"bars": [{"name": "X", "share": 0.2}, {"name": "U", "share": 0.8, "unexplained": True}],
                               "items": [{"name": "X", "type": "auction"}]}),
               fy.channel_you({"five": {"nominal_bps": 8.0, "real_bps": 1.0, "breakeven_bps": 7.0}}),
               fy.supply_you([{"date": "2026-11-10", "security": "10-Year Note", "offering_usd": 42e9, "upcoming": True}]),
               fy.events_you(5), *(fy.read_you(t) for t in fy.OVERALL)]
    texts = [t.replace(fy.DISCLAIMER, "") for e in entries for t in _all_texts(e)]   # the disclaimer itself says "buy"
    assert texts and not [t for t in texts if BANNED.search(t)]


def test_box_escapes_and_lists_sectors():
    html = fy.you_html(("a < b", {"AI & software": "x"}))
    assert "a &lt; b" in html and 'class="g-you-s">AI &amp; software' in html
    assert fy.you_html(None) == ""
