"""Flag unusual weekly changes in dealers' net Treasury futures positions (CFTC TFF).

For each contract, the latest weekly change in the dealer group's net position is compared
with the standard deviation of that contract's earlier weekly changes (CALCULATION). A
change of at least UNUSUAL_SD standard deviations is flagged. Each flagged move is then
read against two plain alternatives, using only the same report:

* reversal: the prior week moved the other way, so the two weeks largely cancel;
* client flow: asset managers moved the other way by at least half as much, so dealers
  look like the other side of client trades.

If neither applies, the move is described as not matched by asset-manager flow, which is
consistent with dealers trading for their own book (for example hedging), but never proof:
the report shows futures only, not who traded, why, or the cash-bond side.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from statistics import pstdev
from typing import Any

UNUSUAL_SD = 2.0
MIN_WEEKS = 26
DEALER, ASSET_MANAGER, LEVERAGED = "dealer", "asset_manager", "leveraged_fund"


def _weekly(metrics: Iterable[Any]) -> dict[tuple[str, str], list[tuple[Any, float]]]:
    """(series, participant) -> [(date, weekly net change)], oldest first, changes only."""
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for m in metrics:
        grouped[(m.series_id, m.participant)].append(m)
    out = {}
    for key, items in grouped.items():
        items.sort(key=lambda m: m.observation_time)
        out[key] = [(m.observation_time, float(m.net_change_contracts)) for m in items
                    if m.net_change_contracts is not None]
    return out


def unusual_dealer_moves(metrics: Iterable[Any], labels: dict[str, str] | None = None) -> dict[str, Any]:
    weekly = _weekly(metrics)
    labels = labels or {}
    rows = []
    for (series, participant), changes in sorted(weekly.items()):
        if participant != DEALER or len(changes) < MIN_WEEKS + 1:
            continue
        day, change = changes[-1]
        history = [c for _, c in changes[:-1]]
        sd = pstdev(history)
        if not sd:
            continue
        prior = changes[-2][1]
        same_day = {p: dict(weekly.get((series, p), [])).get(day) for p in (ASSET_MANAGER, LEVERAGED)}
        larger = sum(1 for c in history if abs(c) >= abs(change))
        row = {"series": series, "contract": labels.get(series, series), "date": day.isoformat(),
               "change": change, "sd": sd, "z": change / sd, "unusual": abs(change) >= UNUSUAL_SD * sd,
               "weeks": len(history), "larger_weeks": larger, "prior_change": prior,
               "two_week": change + prior, "asset_manager_change": same_day[ASSET_MANAGER],
               "leveraged_change": same_day[LEVERAGED]}
        row["read"] = _read(row) if row["unusual"] else None
        rows.append(row)
    flagged = [r for r in rows if r["unusual"]]
    return {"date": max((r["date"] for r in rows), default=None), "rows": rows, "flagged": flagged,
            "threshold_sd": UNUSUAL_SD, "evidence_type": "CALCULATION"}


def _read(row: dict[str, Any]) -> str:
    change, prior, am = row["change"], row["prior_change"], row["asset_manager_change"]
    direction = "toward short (they sold futures on net)" if change < 0 else "toward long (they bought futures on net)"
    notes = [f"Dealers' net position moved {_k(change)}, {direction}."]
    reversal = prior * change < 0 and abs(row["two_week"]) <= 0.5 * abs(change)
    if reversal:
        notes.append(f"It mostly reverses the prior week ({_k(prior)}); over two weeks the net change is {_k(row['two_week'])}.")
    client = am is not None and am * change < 0 and abs(am) >= 0.5 * abs(change)
    if client:
        notes.append(f"Asset managers moved the other way ({_k(am)}), consistent with dealers taking the other "
                     "side of client trades.")
    if not reversal and not client:
        notes.append("It is not matched by asset managers moving the other way and does not reverse the prior week, "
                     "which is consistent with dealers trading for their own book (for example hedging). "
                     "This report cannot show who traded or why.")
    return " ".join(notes)


def _k(value: float) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value) / 1000:.1f}K"
