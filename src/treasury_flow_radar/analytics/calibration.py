"""Measure, from stored history, how much each kind of scheduled event moves the 10-year.

For each event type the prior is the share of the 10-year's day-to-day variance on that
type's days that is not present on ordinary days (days with no scheduled event at all):

    prior = 1 - mean(change² on ordinary days) / mean(change² on event days)

If an event adds an independent shock to ordinary day-to-day noise, variances add, so
this is the expected share of an event day's move that comes from the event. It is
clamped to [0, 1]: a type whose days are no more volatile than ordinary days gets 0.

Days on which a type is the only scheduled event ("solo days") are used when there are
at least MIN_DAYS of them; otherwise every day the type occurs is used, which mixes in
other events on the same day. With fewer than MIN_DAYS either way, the stated fallback
is kept and flagged. Only sessions before the attribution window are used, so the window
being explained never calibrates itself. A 90% interval comes from a fixed-seed bootstrap
(deterministic). This is a CALCULATION over a short sample; it describes how this period
behaved, not a law.
"""
from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import date
from typing import Any

MIN_DAYS = 8
MIN_QUIET_DAYS = 30
RESAMPLES = 2000
SEED = 7
AUCTION_TYPE = "Treasury auction"


def event_type(release: Mapping[str, Any]) -> str:
    """Stable type key for a release or FOMC decision ("FOMC decision (with projections)" -> "FOMC decision")."""
    return str(release.get("short") or release.get("name") or "").split(" (")[0]


def _share(event_sq: list[float], quiet_sq: list[float]) -> float | None:
    event_var = sum(event_sq) / len(event_sq)
    if event_var <= 0:
        return None
    return 1.0 - (sum(quiet_sq) / len(quiet_sq)) / event_var


def calibrate_priors(changes_bps: Mapping[date, float], events: Mapping[date, set[str]], *, before: date,
                     fallback: Mapping[str, float]) -> dict[str, Any]:
    """Return {"types": {type: {...}}, "quiet_days", "start", "end"} measured on sessions before `before`."""
    sample = {d: v for d, v in changes_bps.items() if d < before}
    quiet = [v * v for d, v in sample.items() if not events.get(d)]
    types = sorted(set(fallback) | {t for d, ts in events.items() if d in sample for t in ts})
    rng = random.Random(SEED)
    out: dict[str, dict[str, Any]] = {}
    for kind in types:
        solo = [sample[d] ** 2 for d, ts in events.items() if d in sample and ts == {kind}]
        every = [sample[d] ** 2 for d, ts in events.items() if d in sample and kind in ts]
        if len(solo) >= MIN_DAYS:
            values, basis = solo, "days with no other scheduled event"
        elif len(every) >= MIN_DAYS:
            values, basis = every, "all its days (other events on some of them)"
        else:
            values, basis = [], "too few days to measure"
        estimate = None if not values or len(quiet) < MIN_QUIET_DAYS else _share(values, quiet)
        if estimate is None:
            out[kind] = {"prior": fallback.get(kind), "measured": False, "basis": basis if values else
                         "too few days to measure", "days": len(values), "estimate": None, "low": None, "high": None,
                         "event_rms_bps": None, "quiet_rms_bps": None, "distinguishable": None}
            continue
        boots = sorted(
            s for s in (_share([rng.choice(values) for _ in values], [rng.choice(quiet) for _ in quiet])
                        for _ in range(RESAMPLES)) if s is not None)
        low, high = boots[int(0.05 * len(boots))], boots[int(0.95 * len(boots)) - 1]
        out[kind] = {"prior": min(1.0, max(0.0, estimate)), "measured": True, "basis": basis, "days": len(values),
                     "estimate": estimate, "low": low, "high": high,
                     "event_rms_bps": (sum(values) / len(values)) ** 0.5,
                     "quiet_rms_bps": (sum(quiet) / len(quiet)) ** 0.5 if quiet else None,
                     "distinguishable": low > 0}
    ordered = sorted(sample)
    return {"types": out, "quiet_days": len(quiet), "sessions": len(sample),
            "start": ordered[0].isoformat() if ordered else None,
            "end": ordered[-1].isoformat() if ordered else None, "evidence_type": "CALCULATION"}
