from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from treasury_flow_radar.analytics.positioning_moves import unusual_dealer_moves


@dataclass
class M:
    series_id: str
    participant: str
    observation_time: date
    net_change_contracts: float


def _history(series: str, last: dict[str, tuple[float, float]]) -> list[M]:
    """40 weeks of ±10K changes per group, then the given (prior, latest) changes per group."""
    out, start = [], date(2025, 1, 7)
    for p in ("dealer", "asset_manager", "leveraged_fund"):
        changes = [10_000.0 if i % 2 else -10_000.0 for i in range(40)] + list(last.get(p, (0.0, 0.0)))
        out += [M(series, p, start + timedelta(weeks=i), c) for i, c in enumerate(changes)]
    return out


def test_reversal_and_client_flow_are_named():
    moves = unusual_dealer_moves(_history("ZN", {"dealer": (140_000, -156_000), "asset_manager": (0, 245_000)}),
                                 {"ZN": "10-year"})
    row = moves["flagged"][0]
    assert row["contract"] == "10-year" and row["larger_weeks"] == 0 and row["unusual"]
    assert "toward short" in row["read"]
    assert "mostly reverses the prior week (+140.0K)" in row["read"] and "−16.0K" in row["read"]
    assert "other side of client trades" in row["read"]


def test_unmatched_move_is_consistent_with_own_book_never_proof():
    row = unusual_dealer_moves(_history("ZB", {"dealer": (5_000, 70_000), "asset_manager": (0, 3_000)}))["flagged"][0]
    assert "toward long" in row["read"] and "own book" in row["read"] and "cannot show who traded" in row["read"]


def test_ordinary_week_is_not_flagged():
    moves = unusual_dealer_moves(_history("ZT", {"dealer": (10_000, -12_000)}))
    assert moves["rows"] and not moves["flagged"]
