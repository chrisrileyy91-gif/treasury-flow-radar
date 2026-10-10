"""Plain-English synthesis of the basis-trade setup read.

Rule-based text over :func:`basis_crowding.basis_setup` output. Every sentence is generated
from a stored measurement and carries its evidence category. The synthesis states what the
measurements show and which documented mechanism they bear on; it never predicts yields,
equity prices, or Federal Reserve decisions, and it never recommends a trade.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from treasury_flow_radar.analytics.basis_crowding import (
    CROWDED_PERCENTILE,
    FUNDING_MEDIAN_SESSIONS,
    VOL_ELEVATED_PERCENTILE,
)
from treasury_flow_radar.analytics.descriptive import EvidenceType

MINUS = "−"
SMALL_PERCENTILE = 33.0   # below this, "smaller than in most of the stored history"
HEAVY_PERCENTILE = 60.0   # at or above this (and below crowded), "larger than in most weeks"
NEAR_LINE_POINTS = 5.0    # within this many percentile points of the crowded line, say so

EQUITY_MECHANISM = (
    "On its own, this setup says nothing about where stocks go. It matters for equities mainly if it "
    "breaks: a forced unwind of leveraged Treasury positions can drain liquidity and push funds to sell "
    "other assets too, as in March 2020."
)
FED_CALM = (
    "There is no sign of the funding pressure under which the Fed has added reserves by buying "
    "Treasury bills. Such purchases support liquidity; they are not QE in the 2020 sense of buying "
    "longer-dated bonds to lower yields."
)
FED_UNKNOWN = (
    "Overnight funding is not measured yet, so this read cannot say whether the funding pressure "
    "behind past Fed reserve purchases is present."
)
FED_TIGHT = (
    "Overnight repo trading above the Fed's reserve rate is the condition under which the Fed has added "
    "reserves by buying Treasury bills. That is liquidity support, not QE in the 2020 sense; large-scale "
    "bond buying has followed only a disorderly market break."
)


def _millions(value: float) -> str:
    sign = MINUS if value < 0 else ""
    if abs(value) < 1e6:
        return f"{sign}{abs(value) / 1e3:.0f}K"
    return f"{sign}{abs(value) / 1e6:.1f}M"


def _bp(value: float) -> str:
    rounded = round(value)
    sign = "+" if rounded > 0 else MINUS if rounded < 0 else ""
    return f"{sign}{abs(rounded)} bp"


def _ordinal(value: float) -> str:
    n = round(value)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _line(text: str, kind: EvidenceType | str) -> dict[str, str]:
    return {"text": text, "evidence_type": kind.value if isinstance(kind, EvidenceType) else kind}


def _positioning(agg: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not agg:
        return None
    pct = agg.get("short_percentile")
    net = agg["net_10y_equivalents"]
    size = _millions(abs(net))
    change = agg.get("weekly_change_10y_equivalents")
    week = ""
    if change is not None and abs(change) >= 1e4:
        week = (f", and added {_millions(abs(change))} of short this week" if change < 0
                else f", and cut {_millions(change)} of short this week")
    if pct is None:
        side = "short" if net < 0 else "long"
        return _line(f"Leveraged funds are net {side} {size} 10-year-note equivalents in Treasury futures"
                     f"{week}; too little history is stored to say whether that is large.",
                     EvidenceType.CALCULATION)
    if net >= 0:
        return _line(f"Leveraged funds are net long {size} 10-year-note equivalents in Treasury futures, "
                     f"so there is no aggregate short to unwind{week}.", EvidenceType.CALCULATION)
    if pct >= CROWDED_PERCENTILE:
        where = "among the largest in the stored history"
    elif pct >= HEAVY_PERCENTILE:
        where = ("larger than in most of the stored history, just under the crowded line"
                 if CROWDED_PERCENTILE - pct <= NEAR_LINE_POINTS else
                 "larger than in most of the stored history")
    elif pct < SMALL_PERCENTILE:
        where = "smaller than in most of the stored history"
    else:
        where = "in the middle of its stored range"
    return _line(f"Leveraged funds hold a Treasury futures short of {size} 10-year-note equivalents, "
                 f"{where} ({_ordinal(pct)} percentile of {agg['history_weeks']} weeks){week}.",
                 EvidenceType.CALCULATION)


def _funding(funding: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not funding or funding.get("sofr_minus_iorb_median_bps") is None:
        return None
    median = funding["sofr_minus_iorb_median_bps"]
    streak = funding.get("sessions_above_iorb_streak") or 0
    if median > 0:
        lasting = f", above it for the last {streak} session{'s' if streak != 1 else ''}" if streak else ""
        text = (f"Overnight funding is tightening: SOFR trades above the Fed's reserve rate "
                f"({FUNDING_MEDIAN_SESSIONS}-session median {_bp(median)}{lasting}).")
    else:
        text = (f"Overnight funding is calm: SOFR trades at or below the Fed's reserve rate "
                f"({FUNDING_MEDIAN_SESSIONS}-session median {_bp(median)}).")
    return _line(text, EvidenceType.CALCULATION)


def _volatility(vol: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not vol or vol.get("realized_vol_percentile") is None:
        return None
    pct = vol["realized_vol_percentile"]
    word = "elevated" if pct >= VOL_ELEVATED_PERCENTILE else "ordinary"
    return _line(f"Rate volatility is {word}: the 10-year yield has moved about "
                 f"{vol['realized_vol_bps_per_day']:.1f} bp a day over the last {vol['sessions']} sessions "
                 f"({_ordinal(pct)} percentile).", EvidenceType.CALCULATION)


def _watch(agg: Mapping[str, Any] | None, funding: Mapping[str, Any] | None,
           vol: Mapping[str, Any] | None, state: Mapping[str, Any]) -> list[str]:
    items = []
    if funding and not state.get("funding_tight"):
        items.append(f"SOFR moving above IORB on a {FUNDING_MEDIAN_SESSIONS}-session median basis "
                     f"(now {_bp(funding['sofr_minus_iorb_median_bps'])}).")
    elif funding:
        items.append("Whether SOFR stays above IORB, and whether the 99th-percentile repo rate "
                     f"(now {_bp(funding['sofr99_minus_iorb_bps'])} over IORB) keeps widening."
                     if funding.get("sofr99_minus_iorb_bps") is not None else
                     "Whether SOFR stays above IORB.")
    threshold = (agg or {}).get("crowded_threshold_10y_equivalents")
    if threshold is not None and not state.get("crowded"):
        items.append(f"The leveraged-fund short growing past {_millions(threshold)} 10-year equivalents, "
                     f"the {_ordinal(CROWDED_PERCENTILE)} percentile of stored weeks "
                     f"(now {_millions(-agg['net_10y_equivalents'])}; peak "
                     f"{_millions(agg['peak_short_10y_equivalents'])}).")
    vol_threshold = (vol or {}).get("elevated_threshold_bps_per_day")
    if vol_threshold is not None and not state.get("vol_elevated"):
        items.append(f"Daily 10-year moves averaging above about {vol_threshold:.1f} bp "
                     f"(the {_ordinal(VOL_ELEVATED_PERCENTILE)} percentile).")
    return items


def synthesize(basis: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a headline, evidence-tagged lines, a Fed/QE note, an equity note, and a watch list."""
    basis = basis or {}
    state = basis.get("state") or {}
    agg = (basis.get("crowding") or {}).get("aggregate")
    funding = basis.get("funding")
    vol = basis.get("volatility")
    crowded, tight, elevated = state.get("crowded"), state.get("funding_tight"), state.get("vol_elevated")
    lines = [x for x in (_positioning(agg), _funding(funding), _volatility(vol)) if x]
    known = [v for v in (crowded, tight, elevated) if v is not None]

    if crowded and tight and elevated:
        tone, headline = "strain", "Treasury plumbing is under strain."
        stress = ("All three conditions for forced deleveraging are present at once: a large position, "
                  "tightening funding, and elevated volatility. That is the combination seen before past "
                  "disorderly unwinds; it does not say one will happen.")
    elif crowded and tight:
        tone, headline = "watch", "The setup for a forced unwind is building."
        stress = ("A large position is meeting tighter funding. Volatility has not joined in, which is "
                  "what usually turns a crowded trade into forced selling.")
    elif crowded:
        tone, headline = "watch", "The trade is crowded, but funding is holding."
        stress = ("The position is large, so the market is more exposed than usual, but cheap, stable "
                  "funding means nothing is forcing it to unwind.")
    elif len(known) < 3:
        tone, headline = "partial", "Partial read: some inputs are missing."
        stress = "Nothing measured here points to forced selling, but not every input is available."
    elif tight or elevated:
        tone, headline = "watch", "Plumbing is mostly quiet, with one condition flashing."
        stress = ("The position is not large, so funding or volatility stress has less to force out. "
                  "Nothing here points to forced selling yet.")
    else:
        heavy = ((agg or {}).get("short_percentile") or 0) >= HEAVY_PERCENTILE
        if heavy:
            tone, headline = "quiet", "Treasury plumbing is quiet, but the trade is large."
            stress = ("The position is larger than in most stored weeks, but calm funding and ordinary "
                      "volatility mean nothing is forcing it to unwind.")
        else:
            tone, headline = "quiet", "Treasury plumbing is quiet."
            stress = "Nothing here points to forced selling."

    return {
        "tone": tone,
        "headline": headline,
        "lines": [*lines, _line(stress, EvidenceType.INFERENCE)],
        "fed": (_line(FED_UNKNOWN, EvidenceType.OBSERVATION) if tight is None else
                _line(FED_TIGHT, EvidenceType.MECHANISM) if tight else
                _line(FED_CALM, EvidenceType.INFERENCE)),
        "equities": _line(EQUITY_MECHANISM, EvidenceType.MECHANISM),
        "watch": _watch(agg, funding, vol, state),
        "as_of": {
            "positioning": (agg or {}).get("report_date"),
            "funding": (funding or {}).get("date"),
            "volatility": (vol or {}).get("date"),
        },
        "limit": ("A rule-based reading of the measurements above. It is not a forecast of yields, "
                  "stocks, or Fed policy, and not a trading signal."),
    }
