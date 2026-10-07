from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from treasury_flow_radar.analytics.candidates import (
    deal_rate_risk,
    evaluate_candidates,
    level_context,
    par_modified_duration,
)
from treasury_flow_radar.events import LedgerError, load_deals, validate_deal


def sessions(start: str, n: int) -> list[date]:
    day, out = date.fromisoformat(start), []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def series(days: list[date], values: list[float]) -> dict[date, float]:
    return dict(zip(days, values, strict=True))


DEAL = {"id": "d1", "name": "Example financing", "pricing_date": "2026-09-30", "launch_date": "2026-09-28",
        "size_usd": 10e9,
        "tranches": [{"currency": "USD", "amount": 10e9, "maturity_year": 2036, "coupon_percent": 5.0}],
        "sources": [{"url": "https://example.org/x", "supports": ["pricing_date", "launch_date", "size_usd", "tranches"]}]}


def test_par_duration_matches_closed_form():
    assert par_modified_duration(0, 10) == 10
    assert par_modified_duration(5.0, 10) == pytest.approx((1 - 1.025 ** -20) / 0.05)
    assert par_modified_duration(8.0, 0) == 0


def test_deal_rate_risk_excludes_non_usd_and_uncouponed_tranches():
    deal = dict(DEAL, tranches=DEAL["tranches"] + [
        {"currency": "EUR", "amount": 1e9, "maturity_year": 2031, "coupon_percent": 7.0},
        {"currency": "USD", "amount": 1e9, "maturity_year": 2031}])
    risk = deal_rate_risk(deal, 5.0)
    d = par_modified_duration(5.0, 10)
    assert risk["dv01_usd"] == pytest.approx(10e9 * d * 1e-4)
    assert risk["ten_year_equivalent_usd"] == pytest.approx(10e9)  # same coupon/tenor as a 10-year at 5%
    assert risk["tranches_used"] == 1 and risk["tranches_excluded"] == 2


def test_level_context_percentile_and_highest():
    days = sessions("2026-09-01", 5)
    level = level_context(series(days, [4.0, 4.2, 4.1, 4.3, 4.25]))
    assert level["percentile"] == pytest.approx(80) and level["label"] == "High"
    assert level["highest_in_history"] is False and level["last_at_or_above"] == days[3].isoformat()
    assert level_context(series(days, [4, 4, 4, 4, 5]))["highest_in_history"] is True
    assert level_context({}) is None


def _yields(days, ten, two, thirty):
    return {"DGS10": series(days, ten), "DGS2": series(days, two), "DGS30": series(days, thirty)}


def test_deal_checks_pass_and_fail_on_the_predicted_fingerprint():
    days = sessions("2026-09-24", 9)   # Sep 24 .. Oct 6
    # launch Sep 28 (base = Sep 25 close), price Sep 30, next session Oct 1.
    ten = [5.18, 5.17, 5.24, 5.26, 5.29, 5.24, 5.28, 5.31, 5.33]
    two = [4.87, 4.81, 4.92, 4.89, 4.88, 4.78, 4.83, 4.84, 4.85]
    thirty = [5.47, 5.49, 5.56, 5.59, 5.64, 5.61, 5.63, 5.66, 5.70]
    result = evaluate_candidates(yields=_yields(days, ten, two, thirty), decomposition_by_date={},
                                 auctions=[], releases=[], deals=[DEAL])
    deal = result["candidates"][0]
    status = {c["check"]: c["status"] for c in deal["checks"]}
    assert status["10-year rose before pricing"] == "pass"           # 5.17 -> 5.29
    assert status["Long end led the rise"] == "pass"                 # 30y +15 vs 2y +7
    assert status["Long-end reversal after pricing"] == "fail"       # 2y -10 led the decline
    assert "led by the short end" in next(c["detail"] for c in deal["checks"]
                                          if c["check"] == "Long-end reversal after pricing")
    assert deal["passed"] == 4 and deal["applicable"] == 5 and deal["evidence_type"] == "INFERENCE"


def test_stale_deal_is_not_a_candidate_and_releases_rank_by_checks():
    days = sessions("2026-10-01", 12)
    flat = [5.0] * 12
    ten = flat[:-1] + [5.08]
    two = flat[:-1] + [5.10]
    result = evaluate_candidates(
        yields=_yields(days, ten, two, flat[:-1] + [5.02]),
        decomposition_by_date={days[-1].isoformat(): {"windows": {"1": {
            "nominal_bps": 8, "breakeven_bps": 1, "curve": {"led_by": "short end", "short_change_bps": 10}}}}},
        auctions=[], deals=[dict(DEAL, pricing_date="2026-08-03", launch_date="2026-08-03")],
        releases=[{"date": days[-1].isoformat(), "kind": "labor", "short": "Jobs report"}])
    names = [c["name"] for c in result["candidates"]]
    assert names == ["Jobs report"]
    jobs = result["candidates"][0]
    assert jobs["passed"] == jobs["applicable"] == 3
    assert result["biggest_day"]["date"] == days[-1].isoformat()


def test_ledger_requires_a_source_for_every_fact(tmp_path):
    validate_deal(dict(DEAL))
    with pytest.raises(LedgerError, match="no source supports size_usd"):
        validate_deal(dict(DEAL, sources=[{"url": "https://example.org", "supports": ["pricing_date", "launch_date", "tranches"]}]))
    with pytest.raises(LedgerError, match="https URL"):
        validate_deal(dict(DEAL, sources=[{"url": "ftp://x", "supports": ["pricing_date"]}]))
    path = tmp_path / "deals.json"
    path.write_text(json.dumps({"deals": [DEAL, DEAL]}))
    with pytest.raises(LedgerError, match="unique"):
        load_deals(path)
    assert load_deals(tmp_path / "missing.json") == []


def test_shipped_ledger_is_valid_and_tranches_match_stated_size():
    deals = load_deals()
    paramount = next(d for d in deals if d["id"].startswith("paramount"))
    usd = sum(t["amount"] for t in paramount["tranches"] if t["currency"] == "USD")
    assert usd == paramount["size_usd"]
