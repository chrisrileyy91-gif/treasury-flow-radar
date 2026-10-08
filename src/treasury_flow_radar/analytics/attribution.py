"""Attribute the latest window's 10-year movement to candidate explanations.

Formula (deterministic; the result is an INFERENCE aid, not a causal finding):

For each session s in the window, a_s = |change in the 10-year yield| in basis points.
Each candidate c active on that session has a weight

    w_c,s = prior_c × fit_c,s

* prior_c (0..1) is how strongly that kind of event is expected to move Treasuries
  (stated below as assumptions); for deals it scales with size in 10-year terms.
* fit_c,s (0..1) is how well the session's fingerprint matches the mechanism:
  direction of the move and which end of the curve led it (from the 2-year and
  30-year changes), plus breakevens for inflation releases.

The session's basis points are split: c receives a_s × w_c,s / max(1, W_s), where
W_s = Σ w_c,s, and a_s × max(0, 1 − W_s) is left UNEXPLAINED. A candidate's score is
the basis points it receives across the window; the best potential reason is the
highest score. Nothing here observes hedge trades, order flow or news content, so a
high score means "consistent with the timing and the pattern", never "caused".
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from treasury_flow_radar.analytics.calibration import AUCTION_TYPE, calibrate_priors, event_type
from treasury_flow_radar.analytics.candidates import deal_rate_risk
from treasury_flow_radar.analytics.descriptive import EvidenceType

WINDOW_SESSIONS = 5
FLAT_BPS = 0.5
FULL_SIZE_DEAL_USD = 20e9          # 10-year equivalent at which a deal's prior reaches 1
UNKNOWN_SIZE_DEAL_PRIOR = 0.5
AUCTION_PRIOR = 0.5

ASSUMPTIONS = [
    (
    "Priors for releases, Fed decisions and auctions are measured from stored history before the window: the share "
    "of the 10-year's day-to-day variance on that event's days that ordinary days (no scheduled event) do not have, "
    "clamped to 0..1 (see the table). A type with too few days keeps a stated fallback. Corporate deals cannot be "
    "measured this way yet (too few dated deals), so their prior is a stated rule: 10-year-equivalent size / $20B, "
    "capped at the strongest measured event type's prior so an untested rule cannot outrank tested ones by default."
    ),
    (
    "Fit: a deal's hedging window (launch to pricing) fits a long-end-led rise (1.0; even 0.5; short-led 0.25); "
    "the session after pricing and the settlement session fit a long-end-led fall the same way. Jobs, growth and "
    "policy news fit a short-end-led move (1.0; even 0.75; long-led 0.5). Inflation news fits a move where "
    "breakevens carry at least a third or the short end leads (1.0; otherwise 0.5). Auctions fit a rise into the "
    "auction (1.0 if the matching end led, 0.5 otherwise). A move within ±0.5 bp is not attributed."
    ),
]


def _led(record: Mapping[str, Any] | None) -> str | None:
    curve = (record or {}).get("curve")
    return None if not curve else curve.get("led_by")


def _leadership_fit(led: str | None, wanted: str, *, even: float, other: float) -> float:
    if led is None:
        return 0.5
    if led == wanted:
        return 1.0
    return even if led == "evenly" else other


def _fit(role: str, kind: str | None, tenor_end: str | None, record: Mapping[str, Any] | None,
         change: float) -> float:
    led = _led(record)
    if role == "hedge_build":
        return 0.0 if change <= FLAT_BPS else _leadership_fit(led, "long end", even=0.5, other=0.25)
    if role == "unwind":
        return 0.0 if change >= -FLAT_BPS else _leadership_fit(led, "long end", even=0.5, other=0.25)
    if role == "auction":
        if change <= FLAT_BPS:
            return 0.0
        return 1.0 if tenor_end is not None and led == tenor_end else 0.5
    if abs(change) <= FLAT_BPS:
        return 0.0
    if kind == "inflation":
        nominal, breakeven = (record or {}).get("nominal_bps"), (record or {}).get("breakeven_bps")
        share_ok = nominal not in (None, 0) and breakeven is not None and abs(breakeven) >= abs(nominal) / 3
        return 1.0 if share_ok or led == "short end" else 0.5
    return _leadership_fit(led, "short end", even=0.75, other=0.5)


def _first_session_on_or_after(sessions: list[date], day: date) -> int | None:
    return next((i for i, s in enumerate(sessions) if s >= day), None)


def attribute_window(*, yields: Mapping[str, Mapping[date, float]],
                     decomposition_by_date: Mapping[str, Any], deals: list[Mapping[str, Any]],
                     auctions: list[Mapping[str, Any]], releases: list[Mapping[str, Any]],
                     window: int = WINDOW_SESSIONS) -> dict[str, Any]:
    ten = yields.get("DGS10", {})
    sessions = sorted(ten)
    if len(sessions) <= window:
        return {"status": "INSUFFICIENT DATA", "best": None, "candidates": [], "sessions": [], "calibration": None}
    in_window = sessions[-window:]
    session_set = set(sessions)
    event_days: dict[date, set[str]] = {}
    fallback: dict[str, float] = {AUCTION_TYPE: AUCTION_PRIOR}
    for release in releases:
        day = date.fromisoformat(str(release["date"]))
        if day in session_set:
            event_days.setdefault(day, set()).add(event_type(release))
        fallback.setdefault(event_type(release), float(release.get("weight") or 0.5))
    for auction in auctions:
        day = date.fromisoformat(str(auction["auction_date"]))
        if day in session_set:
            event_days.setdefault(day, set()).add(AUCTION_TYPE)
    all_changes = {s: (ten[s] - ten[sessions[i - 1]]) * 100 for i, s in enumerate(sessions) if i > 0}
    calibration = calibrate_priors(all_changes, event_days, before=in_window[0], fallback=fallback)

    measured = [v["prior"] for v in calibration["types"].values() if v.get("measured")]
    deal_cap = min(1.0, max(measured)) if measured else 1.0
    calibration["deal_prior_cap"] = deal_cap

    def prior_for(kind: str) -> float:
        value = (calibration["types"].get(kind) or {}).get("prior")
        return fallback.get(kind, 0.5) if value is None else value
    change = {s: (ten[s] - ten[sessions[i - 1]]) * 100 for i, s in enumerate(sessions) if i > 0}
    record = {s: (decomposition_by_date.get(s.isoformat()) or {}).get("windows", {}).get("1") for s in in_window}

    activations: dict[date, list[dict[str, Any]]] = {s: [] for s in in_window}
    meta: dict[str, dict[str, Any]] = {}

    def activate(key: str, day_index: int | None, role: str, prior: float, kind: str | None = None,
                 tenor_end: str | None = None) -> None:
        if day_index is None or day_index >= len(sessions) or sessions[day_index] not in activations:
            return
        activations[sessions[day_index]].append(
            {"key": key, "role": role, "prior": prior, "kind": kind, "tenor_end": tenor_end})

    for deal in deals:
        key = f"deal:{deal['id']}"
        risk = deal_rate_risk(deal, ten.get(sessions[-1]))
        equivalent = None if risk is None else risk["ten_year_equivalent_usd"]
        # The deal rule is assumed, not measured, so it is capped at the strongest measured event type:
        # an untested rule must not outrank tested ones by default.
        prior = min(deal_cap, UNKNOWN_SIZE_DEAL_PRIOR if equivalent is None else equivalent / FULL_SIZE_DEAL_USD)
        meta[key] = {"type": "corporate_deal", "name": deal["name"], "date": deal["pricing_date"],
                     "prior": prior, "ten_year_equivalent_usd": equivalent}
        pricing = _first_session_on_or_after(sessions, date.fromisoformat(deal["pricing_date"]))
        launch = _first_session_on_or_after(sessions, date.fromisoformat(deal.get("launch_date") or deal["pricing_date"]))
        if pricing is None or launch is None:
            continue
        for index in range(launch, pricing + 1):
            activate(key, index, "hedge_build", prior)
        activate(key, pricing + 1, "unwind", prior)
        settle = deal.get("settlement_date_expected")
        if settle:
            settle_index = _first_session_on_or_after(sessions, date.fromisoformat(settle))
            if settle_index is not None and settle_index != pricing + 1:
                activate(key, settle_index, "unwind", prior)
    for auction in auctions:
        day = date.fromisoformat(str(auction["auction_date"]))
        index = _first_session_on_or_after(sessions, day)
        term = str(auction.get("security_term") or "")
        years = int(term.split("-")[0]) if term.split("-")[0].isdigit() else None
        tenor_end = None if years is None else "short end" if years <= 5 else "long end" if years >= 10 else None
        key = f"auction:{day.isoformat()}:{term}"
        auction_prior = prior_for(AUCTION_TYPE)
        meta[key] = {"type": "treasury_auction", "name": f"{term} {auction.get('security_type') or ''} auction".strip(),
                     "date": day.isoformat(), "prior": auction_prior, "event_type": AUCTION_TYPE}
        if index is not None and auction_prior > 0:
            activate(key, index, "auction", auction_prior, tenor_end=tenor_end)
            activate(key, index - 1 if index > 0 else None, "auction", auction_prior, tenor_end=tenor_end)
    for release in releases:
        day = date.fromisoformat(str(release["date"]))
        index = _first_session_on_or_after(sessions, day)
        if index is None or sessions[index] != day:
            continue                     # released on a non-session date; not attributed
        key = f"release:{release.get('short') or release.get('name')}:{day.isoformat()}"
        prior = prior_for(event_type(release))
        meta[key] = {"type": "macro_release", "name": release.get("short") or release.get("name"),
                     "date": day.isoformat(), "prior": prior, "kind": release.get("kind"),
                     "event_type": event_type(release)}
        activate(key, index, "release", prior, kind=release.get("kind"))

    scores: dict[str, float] = {}
    details: dict[str, list[dict[str, Any]]] = {}
    session_rows = []
    unexplained_total = 0.0
    total = 0.0
    for s in in_window:
        moved = change.get(s)
        if moved is None:
            continue
        size = abs(moved)
        total += size
        weights = []
        for item in activations[s]:
            fit = _fit(item["role"], item["kind"], item["tenor_end"], record[s], moved)
            weights.append((item, fit, item["prior"] * fit))
        weight_sum = sum(w for _, _, w in weights)
        curve = (record[s] or {}).get("curve") or {}
        shares = []
        for item, fit, weight in weights:
            bps = size * weight / max(1.0, weight_sum) if weight > 0 else 0.0
            scores[item["key"]] = scores.get(item["key"], 0.0) + bps
            details.setdefault(item["key"], []).append(
                {"date": s.isoformat(), "role": item["role"], "fit": fit, "prior": item["prior"],
                 "change_bps": moved, "attributed_bps": bps, "led_by": curve.get("led_by"),
                 "short_change_bps": curve.get("short_change_bps"), "long_change_bps": curve.get("long_change_bps"),
                 "breakeven_bps": (record[s] or {}).get("breakeven_bps"),
                 "competitors": sum(1 for other, _, w in weights if w > 0 and other is not item)})
            shares.append({"key": item["key"], "attributed_bps": bps})
        leftover = size * max(0.0, 1.0 - weight_sum)
        unexplained_total += leftover
        session_rows.append({"date": s.isoformat(), "change_bps": moved, "led_by": _led(record[s]),
                             "attributed": shares, "unexplained_bps": leftover})

    ranked = sorted(
        ({"key": k, **meta[k], "attributed_bps": v, "share": v / total if total else None,
          "sessions": details.get(k, [])} for k, v in scores.items() if k in meta),
        key=lambda c: -c["attributed_bps"])
    best = ranked[0] if ranked and ranked[0]["attributed_bps"] > 0 else None
    return {"status": "AVAILABLE", "window": {"start": in_window[0].isoformat(), "end": in_window[-1].isoformat(),
                                              "sessions": len(in_window)},
            "total_abs_bps": total, "unexplained_bps": unexplained_total,
            "unexplained_share": unexplained_total / total if total else None,
            "best": best, "candidates": ranked, "sessions": session_rows,
            "assumptions": ASSUMPTIONS, "calibration": calibration, "evidence_type": EvidenceType.INFERENCE.value}
