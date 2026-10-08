"""Rank candidate explanations for the latest Treasury move against explicit checks.

The output is an INFERENCE aid, not a causal finding. Each candidate (a corporate deal
from the curated ledger, a Treasury auction, or a macro release) is tested against the
measurable fingerprint its mechanism predicts. Every check is a deterministic
CALCULATION shown with its numbers; the ranking orders candidates by the share of
applicable checks they pass. A candidate that passes every check is still only
consistent with the move: hedge trades, order flow, and news content are not observed.

The candidate list is incomplete by construction: only deals entered in the ledger are
considered, and only the selected FRED releases.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from statistics import pstdev
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType

PASS, FAIL, NA = "pass", "fail", "n/a"
DEAL_LOOKBACK_SESSIONS = 10
WINDOW_SESSIONS = 5
LARGE_DEAL_10Y_EQUIVALENT_USD = 10e9
LARGE_AUCTION_USD = 40e9
TERM_SERIES = {"2-Year": "DGS2", "5-Year": "DGS5", "7-Year": "DGS7", "10-Year": "DGS10",
               "20-Year": "DGS30", "30-Year": "DGS30"}


def level_context(series: Mapping[date, float]) -> dict[str, Any] | None:
    """Where the latest 10-year level sits in the stored history (CALCULATION)."""
    if not series:
        return None
    days = sorted(series)
    latest, value = days[-1], series[days[-1]]
    history = [series[d] for d in days]
    percentile = 100 * sum(v <= value for v in history) / len(history)
    higher_before = [d for d in days[:-1] if series[d] >= value]
    label = "High" if percentile >= 80 else "Low" if percentile <= 20 else "Mid-range"
    return {"date": latest.isoformat(), "percent": value, "percentile": percentile, "label": label,
            "history_start": days[0].isoformat(), "sessions": len(days),
            "highest_in_history": not higher_before,
            "last_at_or_above": higher_before[-1].isoformat() if higher_before else None,
            "evidence_type": EvidenceType.CALCULATION.value}


def _change(series: Mapping[date, float], start: date | None, end: date | None) -> float | None:
    if start is None or end is None or start not in series or end not in series:
        return None
    return (series[end] - series[start]) * 100


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def _bp(value: float | None) -> str:
    if value is None:
        return "unknown"
    rounded = round(value)
    return f"{'+' if rounded > 0 else '−' if rounded < 0 else ''}{abs(rounded)} bp"


def _session_index(sessions: list[date], day: date) -> int | None:
    """Index of the first session on or after `day` (the session that reflects it)."""
    for index, session in enumerate(sessions):
        if session >= day:
            return index
    return None


def evaluate_candidates(*, yields: Mapping[str, Mapping[date, float]],
                        decomposition_by_date: Mapping[str, Any],
                        auctions: list[Mapping[str, Any]], releases: list[Mapping[str, Any]],
                        deals: list[Mapping[str, Any]],
                        market: Mapping[str, Mapping[date, float]] | None = None) -> dict[str, Any]:
    ten = yields.get("DGS10", {})
    sessions = sorted(ten)
    if len(sessions) <= WINDOW_SESSIONS:
        return {"status": "INSUFFICIENT DATA", "candidates": [], "window": None}
    end = sessions[-1]
    start = sessions[-1 - WINDOW_SESSIONS]
    in_window = [d for d in sessions if start < d <= end]
    daily = {d: _change(ten, sessions[i - 1], d) for i, d in enumerate(sessions) if i > 0}
    biggest_day = max(in_window, key=lambda d: abs(daily.get(d) or 0)) if in_window else None
    candidates = []
    for deal in deals:
        candidates.append(_deal(deal, yields, sessions, market))
    for auction in auctions:
        day = date.fromisoformat(str(auction["auction_date"]))
        if start < day <= end:
            candidates.append(_auction(auction, day, yields, sessions))
    for release in releases:
        day = date.fromisoformat(str(release["date"]))
        if start < day <= end:
            candidates.append(_release(release, day, decomposition_by_date, daily, biggest_day))
    candidates = [c for c in candidates if c is not None]
    for c in candidates:
        applicable = [x for x in c["checks"] if x["status"] != NA]
        c["passed"] = sum(x["status"] == PASS for x in applicable)
        c["applicable"] = len(applicable)
        c["evidence_type"] = EvidenceType.INFERENCE.value
    for c in candidates:
        # Timing alone cannot rank a candidate: it needs at least one testable check besides it.
        c["testable"] = c["applicable"] >= 2
    candidates.sort(key=lambda c: (not c["testable"],
                                   -(c["passed"] / c["applicable"] if c["applicable"] else 0),
                                   -c["passed"], -(c.get("size_usd") or 0)))
    return {"status": "AVAILABLE" if candidates else "NO CANDIDATES IN WINDOW",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "sessions": WINDOW_SESSIONS},
            "biggest_day": None if biggest_day is None else {"date": biggest_day.isoformat(),
                                                             "change_bps": daily.get(biggest_day)},
            "candidates": candidates,
            "limitations": [
                "Deals come from SEC EDGAR pricing filings (registered term sheets; 144A press releases where readable) plus the curated ledger; deals announced only elsewhere are missed.",
                "Scheduled events covered: FOMC decisions; CPI, PPI and PCE; the jobs report, JOLTS and weekly jobless claims; GDP and retail sales; 2- to 30-year Treasury auctions. Unscheduled news (speeches, geopolitics, other countries' markets) is not covered.",
                "Passing checks means the timing and pattern are consistent with a candidate, not that it caused the move.",
            ]}


def _deal(deal: Mapping[str, Any], yields: Mapping[str, Mapping[date, float]],
          sessions: list[date], market: Mapping[str, Mapping[date, float]] | None = None) -> dict[str, Any] | None:
    pricing = date.fromisoformat(deal["pricing_date"])
    p_index = _session_index(sessions, pricing)
    recent = p_index is not None and p_index >= len(sessions) - DEAL_LOOKBACK_SESSIONS
    if not recent:
        return None
    launch = date.fromisoformat(deal["launch_date"]) if deal.get("launch_date") else pricing
    l_index = _session_index(sessions, launch)
    base = sessions[l_index - 1] if l_index else None          # close before the deal was launched
    priced = sessions[p_index]
    after = sessions[p_index + 1] if p_index + 1 < len(sessions) else None
    ten, two, thirty = yields.get("DGS10", {}), yields.get("DGS2", {}), yields.get("DGS30", {})
    rise = _change(ten, base, priced)
    pre_two, pre_thirty = _change(two, base, priced), _change(thirty, base, priced)
    post_two, post_thirty = _change(two, priced, after), _change(thirty, priced, after)
    checks = [_check("Timing", PASS, f"priced {pricing.isoformat()}, within the last {DEAL_LOOKBACK_SESSIONS} sessions")]
    checks.append(_check(
        "10-year rose before pricing",
        NA if rise is None else PASS if rise > 0.5 else FAIL,
        f"{_bp(rise)} from {base.isoformat() if base else 'unknown'} (close before launch) to pricing day"))
    led = None if pre_two is None or pre_thirty is None else pre_thirty > pre_two + 0.5
    checks.append(_check(
        "Long end led the rise", NA if led is None else PASS if led else FAIL,
        f"30-year {_bp(pre_thirty)} vs 2-year {_bp(pre_two)} over the same sessions"))
    if after is None or post_two is None or post_thirty is None:
        checks.append(_check("Long-end reversal after pricing", NA, "next session not yet observed"))
    else:
        reversal = post_thirty < -0.5 and abs(post_thirty) >= abs(post_two)
        checks.append(_check(
            "Long-end reversal after pricing", PASS if reversal else FAIL,
            f"next session ({after.isoformat()}): 30-year {_bp(post_thirty)}, 2-year {_bp(post_two)}"
            + ("" if reversal else "; the decline was led by the short end" if post_thirty < 0 else "")))
    checks.append(_spread_check((market or {}).get("BAMLC0A0CM") or {}, base, after or priced, launch))
    risk = deal_rate_risk(deal, ten.get(priced))
    equivalent = None if risk is None else risk["ten_year_equivalent_usd"]
    checks.append(_check(
        "Large in 10-year terms",
        NA if equivalent is None else PASS if equivalent >= LARGE_DEAL_10Y_EQUIVALENT_USD else FAIL,
        "unknown" if equivalent is None else
        f"about ${equivalent / 1e9:.{0 if equivalent >= 10e9 else 1}f}B of 10-year Treasuries; DV01 about ${risk['dv01_usd'] / 1e6:.1f}M per bp"))
    return {"type": "corporate_deal", "name": deal["name"], "date": pricing.isoformat(),
            "dates": {k: deal.get(k) for k in ("launch_date", "pricing_date", "settlement_date_expected",
                                               "settlement_status", "transaction_close_date")},
            "size_usd": deal.get("size_usd"), "ten_year_equivalent_usd": equivalent, "rate_risk": risk,
            "mechanism": "Dealers or issuers may hedge rate risk with Treasuries before pricing and unwind after.",
            "sources": deal.get("sources", []), "checks": checks}


def _spread_check(ig: Mapping[date, float], start: date | None, end: date, launch: date) -> dict[str, Any]:
    """Supply check: did investment-grade spreads widen around pricing by more than usual?

    Change in the ICE BofA IG option-adjusted spread from the close before launch to the
    session after pricing, against the typical change over that many sessions measured on
    earlier history (daily standard deviation × √sessions)."""
    name = "Investment-grade spreads widened around pricing"
    if not ig or start is None:
        return _check(name, NA, "investment-grade spread data not stored for these dates")
    if start not in ig or end not in ig:
        return _check(name, NA, f"spread not yet published for {start.isoformat() if start not in ig else end.isoformat()}")
    days = sorted(ig)
    span = sum(1 for d in days if start < d <= end)
    history = [(ig[d] - ig[days[i - 1]]) * 100 for i, d in enumerate(days) if i and d < launch]
    if span == 0 or len(history) < 30:
        return _check(name, NA, "not enough spread history to judge a usual move")
    change = (ig[end] - ig[start]) * 100
    typical = pstdev(history) * span ** 0.5
    return _check(name, PASS if change > typical else FAIL,
                  f"{_bp(change)} from {start.isoformat()} to {end.isoformat()}; a usual move over {span} "
                  f"session{'s' if span > 1 else ''} is about ±{typical:.1f} bp. This is the whole investment-grade "
                  f"market, so it reflects all new supply and news those days, not this deal alone")


def par_modified_duration(coupon_percent: float, years: float) -> float:
    """Modified duration of a semiannual par bond: (1 - (1 + y/2)^(-2n)) / y."""
    y = coupon_percent / 100
    if years <= 0:
        return 0.0
    if y == 0:
        return float(years)
    return (1 - (1 + y / 2) ** (-2 * years)) / y


def deal_rate_risk(deal: Mapping[str, Any], ten_year_yield_percent: float | None) -> dict[str, Any] | None:
    """Approximate rate risk of the USD fixed-rate tranches (CALCULATION, par-bond assumption).

    DV01 = sum(amount × modified duration) × 0.0001, using each tranche's published
    coupon as its yield and whole years to maturity from the pricing year. The 10-year
    equivalent divides the same sum by the modified duration of a par 10-year Treasury
    at the 10-year yield. Floating-rate loans and non-USD tranches are excluded.
    """
    pricing = date.fromisoformat(deal["pricing_date"])
    weighted = 0.0
    used = skipped = 0
    for tranche in deal.get("tranches", []):
        if (tranche.get("currency") != "USD" or tranche.get("amount") is None
                or tranche.get("coupon_percent") is None or not tranche.get("maturity_year")):
            skipped += 1
            continue
        years = max(int(tranche["maturity_year"]) - pricing.year, 0)
        weighted += float(tranche["amount"]) * par_modified_duration(float(tranche["coupon_percent"]), years)
        used += 1
    if not used:
        return None
    ten_duration = None if ten_year_yield_percent is None else par_modified_duration(ten_year_yield_percent, 10)
    return {"dv01_usd": weighted * 1e-4, "ten_year_equivalent_usd": None if not ten_duration else weighted / ten_duration,
            "tranches_used": used, "tranches_excluded": skipped,
            "method": "par-bond modified duration from published coupon; whole years to maturity",
            "evidence_type": EvidenceType.CALCULATION.value}


def _auction(auction: Mapping[str, Any], day: date, yields: Mapping[str, Mapping[date, float]],
             sessions: list[date]) -> dict[str, Any]:
    term = str(auction.get("security_term") or "")
    series = TERM_SERIES.get(term)
    index = _session_index(sessions, day)
    before = sessions[index - 3] if index is not None and index >= 3 else None
    matched = None if series is None else _change(yields.get(series, {}), before, sessions[index] if index is not None else None)
    size = auction.get("offering_amount")
    checks = [
        _check("Timing", PASS, f"auctioned {day.isoformat()}, inside the window"),
        _check(f"{term or 'Matching'} yield rose into the auction",
               NA if matched is None else PASS if matched > 0.5 else FAIL,
               f"{_bp(matched)} over the 3 sessions to auction day" if series else "no matching yield series"),
        _check("Large auction", NA if size is None else PASS if float(size) >= LARGE_AUCTION_USD else FAIL,
               "unknown" if size is None else f"${float(size) / 1e9:.0f}B offered"),
    ]
    return {"type": "treasury_auction", "name": f"{term} {auction.get('security_type') or ''} auction".strip(),
            "date": day.isoformat(), "size_usd": size,
            "mechanism": "New supply can require a price concession that lifts yields into the auction.",
            "checks": checks}


def _release(release: Mapping[str, Any], day: date, decomposition_by_date: Mapping[str, Any],
             daily: Mapping[date, float | None], biggest_day: date | None) -> dict[str, Any]:
    kind = release.get("kind")
    record = (decomposition_by_date.get(day.isoformat()) or {}).get("windows", {}).get("1")
    curve = (record or {}).get("curve")
    nominal, breakeven = (record or {}).get("nominal_bps"), (record or {}).get("breakeven_bps")
    checks = [_check("Timing", PASS, f"released {day.isoformat()}, inside the window")]
    if day not in daily:
        checks.append(_check("Biggest move of the window on release day", NA, "no yield session on that date"))
    else:
        is_biggest = biggest_day == day
        checks.append(_check("Biggest move of the window on release day", PASS if is_biggest else FAIL,
                             f"10-year {_bp(daily.get(day))} that day"))
    if kind == "inflation":
        fit = (None if nominal is None or breakeven is None or curve is None else
               (abs(nominal) > 0.5 and abs(breakeven) >= abs(nominal) / 3) or curve["led_by"] == "short end")
        detail = f"breakeven {_bp(breakeven)} of a {_bp(nominal)} 10-year move; curve led by {curve['led_by'] if curve else 'unknown'}"
        name = "Breakevens or the short end moved"
    else:
        fit = None if curve is None else curve["led_by"] == "short end"
        detail = f"curve led by {curve['led_by'] if curve else 'unknown'} (2-year {_bp(curve['short_change_bps']) if curve else 'unknown'})"
        name = "Short end led the move"
    checks.append(_check(name, NA if fit is None else PASS if fit else FAIL, detail))
    mechanisms = {"inflation": "Inflation news reprices inflation compensation and expected policy.",
                  "labor": "Labor data reprice the expected path of policy rates.",
                  "policy": "Policy decisions reprice expected short rates directly."}
    return {"type": "macro_release", "name": release.get("short") or release.get("name"),
            "date": day.isoformat(), "size_usd": None, "mechanism": mechanisms.get(kind, ""),
            "checks": checks}
