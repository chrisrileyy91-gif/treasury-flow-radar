from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from dashboard import for_you as fy
from dashboard.overview import cash_plan_lines, overview
from treasury_flow_radar.analytics.treasury_cash import DIRECTION_BN, treasury_cash
from treasury_flow_radar.events import LedgerError, load_cash_guidance

BANNED = re.compile(r"\b(buy|sell now|should sell|should buy|reduce|trim|exit|hedge your|price target)\b",
                    re.IGNORECASE)
TREASURY = "https://home.treasury.gov/news/press-releases/sb0678"


def _rows(values_bn, end=date(2026, 11, 4)):
    """Weekly WTREGEN rows (millions of dollars), oldest first, ending on ``end``."""
    n = len(values_bn)
    return [{"series_identifier": "WTREGEN", "observation_time": (end - timedelta(weeks=n - 1 - i)).isoformat(),
             "value_numeric": None if v is None else v * 1000.0, "unit": "million_us_dollars"}
            for i, v in enumerate(values_bn)]


def _guidance(target_end="2026-12-31", target_bn=850, peak=True, following=True):
    targets = [{"id": "t1", "quarter_end": target_end, "cash_balance_usd_bn": target_bn,
                "sources": [{"url": TREASURY, "supports": ["quarter_end", "cash_balance_usd_bn"]}]}]
    if following:
        targets.append({"id": "t2", "quarter_end": "2027-03-31", "cash_balance_usd_bn": 1000,
                        "sources": [{"url": TREASURY, "supports": ["quarter_end", "cash_balance_usd_bn"]}]})
    peaks = [{"id": "p1", "label": "late November", "window_start": "2026-11-16", "window_end": "2026-11-30",
              "level_usd_bn": 1025, "band_usd_bn": 50,
              "sources": [{"url": TREASURY, "supports": ["label", "level_usd_bn", "band_usd_bn"]}]}] if peak else []
    return {"targets": targets, "peaks": peaks, "next_release": "2027-02-01"}


def test_balance_is_shown_in_billions_and_changes_use_actual_reports():
    cash = treasury_cash(_rows([880, 900, 920, 940, 957]))
    assert cash["date"] == "2026-11-04" and cash["balance_bn"] == pytest.approx(957)
    assert cash["week_change_bn"] == pytest.approx(17)
    assert cash["four_report_change_bn"] == pytest.approx(77)
    assert cash["four_report_base_date"] == "2026-10-07"
    assert cash["balance_evidence_type"] == "FACT" and cash["change_evidence_type"] == "CALCULATION"


def test_direction_uses_market_side_language():
    assert treasury_cash(_rows([880, 0, 0, 0, 957]))["direction"] == "draining"      # TGA up = drain
    assert treasury_cash(_rows([957, 0, 0, 0, 880]))["direction"] == "injecting"     # TGA down = injection
    assert treasury_cash(_rows([900, 0, 0, 0, 900 + DIRECTION_BN]))["direction"] == "flat"  # at the line = flat
    assert treasury_cash(_rows([900, 950]))["direction"] is None                    # too few reports


def test_missing_values_are_not_counted_as_reports_and_no_data_gives_none():
    cash = treasury_cash(_rows([900, None, 920]))
    assert cash["week_change_bn"] == pytest.approx(20)        # compared with the prior valued report
    assert treasury_cash([]) is None
    assert treasury_cash([{"series_identifier": "SOFR", "observation_time": "2026-11-04",
                                 "value_numeric": 3.9}]) is None


def test_building_toward_projected_peak_then_spend_down():
    plan = treasury_cash(_rows([957]), _guidance())["plan"]
    assert plan["phase"] == "building" and plan["outlook"] == "drain_then_injection"
    assert plan["peak"]["rise_to_peak_bn"] == pytest.approx(68)
    assert plan["peak"]["release_after_peak_bn"] == pytest.approx(-175)
    assert plan["gap_to_target_bn"] == pytest.approx(107)
    assert plan["following"]["change_from_target_bn"] == pytest.approx(150)
    assert plan["evidence_type"] == "INFERENCE"


def test_inside_the_peak_band_counts_as_spend_down_not_building():
    plan = treasury_cash(_rows([1000], end=date(2026, 11, 25)), _guidance())["plan"]
    assert plan["phase"] == "spend_down" and plan["outlook"] == "injection_ahead"
    assert plan["planned_change_to_target_bn"] == pytest.approx(-150)
    assert plan["implied_weekly_change_bn"] == pytest.approx(-150 / (36 / 7))


def test_below_target_means_rebuild_and_near_target_is_on_plan():
    below = treasury_cash(_rows([700], end=date(2026, 12, 9)), _guidance(peak=False))["plan"]
    assert below["phase"] == "rebuild" and below["outlook"] == "drain_ahead"
    near = treasury_cash(_rows([870], end=date(2026, 12, 9)), _guidance(peak=False))["plan"]
    assert near["phase"] == "on_plan" and near["outlook"] == "neutral"


def test_expired_guidance_says_so_and_no_guidance_gives_no_plan():
    late = treasury_cash(_rows([900], end=date(2027, 4, 8)), _guidance())["plan"]
    assert late["phase"] == "no_guidance" and late["outlook"] is None and late["next_release"] == "2027-02-01"
    assert treasury_cash(_rows([900]))["plan"] is None


def test_shipped_ledger_loads_and_matches_treasury_statements():
    guidance = load_cash_guidance()
    assert [(t["quarter_end"], t["cash_balance_usd_bn"]) for t in guidance["targets"]] == [
        ("2026-12-31", 850), ("2027-03-31", 1000)]
    assert {p["level_usd_bn"] for p in guidance["peaks"]} == {1025}
    assert all(s["url"].startswith("https://home.treasury.gov/") for t in guidance["targets"] for s in t["sources"])


def test_ledger_rejects_unsourced_or_non_treasury_facts(tmp_path):
    bad = _guidance(peak=False, following=False)
    bad["targets"][0]["sources"][0]["supports"] = ["quarter_end"]
    path = tmp_path / "g.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(LedgerError, match="no source supports cash_balance_usd_bn"):
        load_cash_guidance(path)
    bad["targets"][0]["sources"][0] = {"url": "https://example.com/x", "supports": ["quarter_end", "cash_balance_usd_bn"]}
    path.write_text(json.dumps(bad))
    with pytest.raises(LedgerError, match="home.treasury.gov"):
        load_cash_guidance(path)
    assert load_cash_guidance(tmp_path / "missing.json") is None


def test_overview_card_states_direction_plan_and_what_it_means():
    cash = treasury_cash(_rows([880, 900, 920, 940, 957]), _guidance())
    html = overview({"cash": cash})
    assert 'id="g-cash"' in html and "$957B" in html and "+$77B in 4 weeks" in html
    assert "Liquidity draining." in html
    assert "TGA up = cash leaving banks (liquidity drain)" in html
    assert "peak near <b>$1,025B</b>" in html and "$175B handed back to banks (injection)" in html
    assert "$150B more pulled out of banks (drain)" in html
    assert "What this means for you" in html and html.count('class="g-you-s"') == 3   # sectors when draining
    assert 'id="g-cash"' not in overview({"cash": None})


def test_plan_lines_without_peak_give_pace_and_without_plan_say_so():
    cash = treasury_cash(_rows([1000], end=date(2026, 11, 25)), _guidance(peak=False, following=False))
    assert "$150B handed back to banks (injection) over about 5 weeks" in " ".join(cash_plan_lines(cash))
    assert "not loaded" in cash_plan_lines(treasury_cash(_rows([900])))[0]


def test_cash_lines_never_give_trade_instructions():
    entries = [fy.cash_you({"balance_bn": 900.0, "direction": d, "plan": {"outlook": o}})
               for d in fy.CASH_DIRECTION for o in fy.CASH_OUTLOOK]
    texts = [t for e in entries for t in (e[0], *e[1].values())]
    assert texts and not [t for t in texts if BANNED.search(t)]
    assert fy.cash_you(None) is None
