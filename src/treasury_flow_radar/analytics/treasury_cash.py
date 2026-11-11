"""Treasury cash (TGA) liquidity read: is Treasury pulling cash out of the banking system or
putting it back, and what does its own published plan imply next?

Market-side language, used everywhere this read is shown:

* **TGA rising = liquidity drain.** Buyers of new Treasury debt pay from bank accounts; that cash
  lands in Treasury's account at the Fed, outside the banking system, so bank reserves fall.
* **TGA falling = liquidity injection (spend-down).** Treasury payments (benefits, salaries,
  refunds, contracts) land in bank accounts and reserves rise.

Inputs
------
* FRED ``WTREGEN``: the TGA week average (week ending Wednesday) from the Fed's H.4.1 release,
  millions of U.S. dollars, a FACT. Shown in billions (value / 1,000), a documented unit change.
* ``events/treasury_cash_guidance.json``: Treasury's own quarter-end cash assumptions and
  projected peaks from the quarterly refunding documents, each fact tied to a home.treasury.gov
  source. These are Treasury's stated assumptions, not forecasts by this project.

Outputs
-------
* Week and four-report changes (CALCULATION) over actual stored reports; no weeks are filled in.
* ``direction`` (OBSERVATION): ``draining`` when the balance rose more than ``DIRECTION_BN`` over
  four reports, ``injecting`` when it fell more than that, otherwise ``flat``. A display rule,
  not a calibrated threshold.
* ``plan`` (INFERENCE): where the balance sits against Treasury's next quarter-end assumption and
  any projected peak before it. It restates what Treasury's own plan implies for the direction of
  cash; Treasury revises these numbers every quarter, and actual balances can miss them. It does
  not predict markets and is not a buy or sell signal.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType, Observation, _latest_per_date

SERIES_ID = "WTREGEN"
DIRECTION_BN = 25.0         # a four-report move larger than $25B counts as a direction (display rule)
ON_PLAN_BN = 50.0           # within $50B of the quarter-end assumption counts as "on plan" (display rule)
CHANGE_REPORTS = 4
HISTORY_REPORTS = 52


def _day(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _direction(change_bn: float | None) -> str | None:
    if change_bn is None:
        return None
    if change_bn > DIRECTION_BN:
        return "draining"
    if change_bn < -DIRECTION_BN:
        return "injecting"
    return "flat"


def _plan(balance_bn: float, as_of: date, guidance: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not guidance:
        return None
    targets = [t for t in guidance.get("targets") or [] if _day(t["quarter_end"]) >= as_of]
    if not targets:
        return {"phase": "no_guidance", "outlook": None,
                "next_release": guidance.get("next_release"),
                "next_release_source": guidance.get("next_release_source"),
                "evidence_type": EvidenceType.INFERENCE.value}
    target = targets[0]
    quarter_end = _day(target["quarter_end"])
    target_bn = float(target["cash_balance_usd_bn"])
    gap_bn = balance_bn - target_bn
    days_left = (quarter_end - as_of).days
    peaks = [p for p in guidance.get("peaks") or []
             if _day(p["window_end"]) >= as_of and _day(p["window_start"]) <= quarter_end]
    pending = next((p for p in peaks if balance_bn < float(p["level_usd_bn"]) - float(p["band_usd_bn"])), None)
    if pending is not None:
        phase, outlook = "building", "drain_then_injection"
    elif gap_bn > ON_PLAN_BN:
        phase, outlook = "spend_down", "injection_ahead"
    elif gap_bn < -ON_PLAN_BN:
        phase, outlook = "rebuild", "drain_ahead"
    else:
        phase, outlook = "on_plan", "neutral"
    following = targets[1] if len(targets) > 1 else None
    return {
        "phase": phase,
        "outlook": outlook,
        "target": {"quarter_end": quarter_end.isoformat(), "cash_balance_bn": target_bn,
                   "sources": [s["url"] for s in target.get("sources") or []]},
        "gap_to_target_bn": gap_bn,
        # Positive = Treasury's plan adds to its cash (drain); negative = it releases cash (injection).
        "planned_change_to_target_bn": target_bn - balance_bn,
        "days_to_quarter_end": days_left,
        "implied_weekly_change_bn": None if days_left < 7 else (target_bn - balance_bn) / (days_left / 7),
        "peak": None if pending is None else {
            "label": pending["label"], "window_start": pending["window_start"],
            "window_end": pending["window_end"], "level_bn": float(pending["level_usd_bn"]),
            "band_bn": float(pending["band_usd_bn"]),
            "rise_to_peak_bn": float(pending["level_usd_bn"]) - balance_bn,
            "release_after_peak_bn": target_bn - float(pending["level_usd_bn"]),
            "sources": [s["url"] for s in pending.get("sources") or []]},
        "following": None if following is None else {
            "quarter_end": following["quarter_end"],
            "cash_balance_bn": float(following["cash_balance_usd_bn"]),
            "change_from_target_bn": float(following["cash_balance_usd_bn"]) - target_bn,
            "sources": [s["url"] for s in following.get("sources") or []]},
        "next_release": guidance.get("next_release"),
        "window_rule": guidance.get("window_rule"),
        "rules": {"on_plan_bn": ON_PLAN_BN},
        "evidence_type": EvidenceType.INFERENCE.value,
    }


def treasury_cash(rows: Iterable[Mapping[str, Any] | Observation],
                  guidance: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The TGA read from normalized observation rows, or None when no TGA reports are stored."""
    reports = sorted((_day(o.observation_time), o.value / 1000.0) for o in _latest_per_date(rows)
                     if o.series_id == SERIES_ID and o.value is not None)
    if not reports:
        return None
    as_of, balance = reports[-1]
    prior = reports[-2] if len(reports) >= 2 else None
    base = reports[-1 - CHANGE_REPORTS] if len(reports) > CHANGE_REPORTS else None
    four = None if base is None else balance - base[1]
    return {
        "date": as_of.isoformat(),
        "balance_bn": balance,
        "balance_evidence_type": EvidenceType.FACT.value,
        "week_change_bn": None if prior is None else balance - prior[1],
        "four_report_change_bn": four,
        "four_report_base_date": None if base is None else base[0].isoformat(),
        "change_evidence_type": EvidenceType.CALCULATION.value,
        "direction": _direction(four),
        "direction_evidence_type": EvidenceType.OBSERVATION.value,
        "plan": _plan(balance, as_of, guidance),
        "history": [{"date": d.isoformat(), "value": v} for d, v in reports[-HISTORY_REPORTS:]],
        "method": {
            "series": "FRED WTREGEN (H.4.1 TGA, week average, week ending Wednesday); millions shown as billions",
            "direction_rule": f"four-report change above +${DIRECTION_BN:.0f}B = draining, below "
                              f"-${DIRECTION_BN:.0f}B = injecting, otherwise flat (display rule)",
            "language": "TGA up = liquidity drain from banks; TGA down = liquidity injection (spend-down)",
        },
    }
