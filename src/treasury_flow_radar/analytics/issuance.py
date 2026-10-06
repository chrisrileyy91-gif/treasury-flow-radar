"""Descriptive issuance/rate-lock measurements; no causal attribution."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType, Observation
from treasury_flow_radar.analytics.temporal import align_observation


def analyze_issuance_event(
    event: Mapping[str, Any],
    yield_observations: Iterable[Observation | Mapping[str, Any]],
) -> dict[str, Any]:
    """Calculate issuance timing and duration proxies from supplied event/yield facts.

    Treasury-equivalent duration pressure is principal times an estimated duration
    in currency-years, not DV01, Treasury notional, or a causal market-pressure measure.
    """
    fields = dict(event.get("fields", event))
    pricing = _date(fields.get("pricing_date"))
    announcement = _date(fields.get("announcement_date"))
    settlement = _date(fields.get("settlement_date"))
    maturity = _date(fields.get("maturity_date"))
    currency = fields.get("currency")
    principal = _finite(fields.get("principal_amount"))
    duration = _finite(event.get("duration_years", fields.get("estimated_duration_years")))
    duration_method = event.get("duration_method") or fields.get("duration_method")
    if duration is None and maturity is not None:
        anchor = pricing or settlement
        if anchor is not None and maturity >= anchor:
            duration = (maturity - anchor).days / 365.25
            duration_method = "maturity_years_proxy_act_365_25"
    pressure = principal * duration if principal is not None and duration is not None else None

    series = _yield_series(fields.get("benchmark_maturity"), list(yield_observations))
    timeline = []
    for label, day in (("announcement", announcement), ("pricing", pricing), ("settlement", settlement)):
        match = align_observation(series, day, direction="prior") if day else None
        timeline.append({"milestone": label, "event_date": day.isoformat() if day else None,
                         "match": None if match is None else match.to_dict()})
    changes = {}
    for left, right, name in (("announcement", "pricing", "announcement_to_pricing"),
                              ("pricing", "settlement", "pricing_to_settlement")):
        left_match = next(x["match"] for x in timeline if x["milestone"] == left)
        right_match = next(x["match"] for x in timeline if x["milestone"] == right)
        changes[name] = _match_delta(left_match, right_match)

    settlement_match = next(x["match"] for x in timeline if x["milestone"] == "settlement")
    post = {}
    if settlement is not None:
        next_match = align_observation(series, settlement + timedelta(days=1), direction="following")
        post["immediately_after_settlement"] = {
            "target_date": (settlement + timedelta(days=1)).isoformat(),
            "match": None if next_match is None else next_match.to_dict(),
            "change_from_settlement_bps": _match_delta(
                settlement_match, None if next_match is None else next_match.to_dict()
            ),
        }
        for horizon in (1, 2, 3, 5):
            target = settlement + timedelta(days=horizon)
            match = align_observation(series, target, direction="following")
            post[f"{horizon}_calendar_days"] = {
                "target_date": target.isoformat(),
                "match": None if match is None else match.to_dict(),
                "change_from_settlement_bps": _match_delta(settlement_match,
                    None if match is None else match.to_dict()),
            }
    return {
        "source_record_id": event.get("source_record_id") or fields.get("source_record_id"),
        "issuer": fields.get("issuer_name"),
        "issuer_identifier": fields.get("issuer_identifier"),
        "deal_identifier": fields.get("deal_identifier"),
        "announcement_date": fields.get("announcement_date"),
        "pricing_date": fields.get("pricing_date"),
        "settlement_date": fields.get("settlement_date"),
        "principal_amount": principal,
        "currency": currency,
        "coupon": fields.get("coupon"),
        "yield": fields.get("yield"),
        "maturity_date": maturity.isoformat() if maturity else None,
        "estimated_duration_years": duration,
        "duration_method": duration_method,
        "duration_estimate_kind": event.get("duration_kind") or "estimate" if duration is not None else None,
        "treasury_equivalent_duration_pressure": pressure,
        "duration_pressure_unit": f"{currency}-years" if pressure is not None and currency else None,
        "days_pricing_to_settlement": (settlement - pricing).days if settlement and pricing else None,
        "treasury_yield_series": series[0].series_id if series else None,
        "milestones": timeline,
        "yield_changes": changes,
        "post_settlement_changes": post,
        "credit_spread": fields.get("credit_spread", fields.get("spread")),
        "credit_classification": fields.get("credit_classification"),
        "rating": fields.get("rating"),
        "treasury_benchmark_maturity": fields.get("benchmark_maturity"),
        "treasury_benchmark_yield": fields.get("benchmark_yield"),
        "source_identifier": event.get("source_identifier"),
        "source_url": event.get("source_url"),
        "publication_time": event.get("publication_time"),
        "retrieval_time": event.get("retrieval_time"),
        "raw_record_id": event.get("raw_record_id"),
        "evidence_type": EvidenceType.CALCULATION.value,
        "interpretation": "Temporally associated measurements do not establish issuance-driven hedging or causality.",
    }


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("issuance analytics values must be finite")
    return result


def _yield_series(benchmark: Any, rows: list[Observation | Mapping[str, Any]]) -> list[Observation | Mapping[str, Any]]:
    name = str(benchmark or "").casefold().replace(" ", "")
    # Map a stated Treasury benchmark to the matching FRED constant-maturity series.
    # An unrecognized benchmark yields no series rather than a guessed substitute.
    target = next((f"DGS{years}" for years in (2, 5, 7, 10, 30)
                   if name in {f"{years}y", f"{years}-year", f"{years}year", f"dgs{years}"}), None)
    observations = [item if isinstance(item, Observation) else Observation.from_mapping(item)
                    for item in rows]
    # Milestones align to valued yield sessions only; a no-value date (e.g. a market
    # closure) is never selected as the "prior" or "following" yield.
    selected = [item for item in observations
                if target is not None and item.series_id.upper() == target and item.value is not None]
    return selected


def _match_delta(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any]:
    before = None if left is None else left.get("value")
    after = None if right is None else right.get("value")
    return {
        "change_bps": None if before is None or after is None else (float(after) - float(before)) * 100,
        "from_observation_date": None if left is None else left.get("source_observation_date"),
        "to_observation_date": None if right is None else right.get("source_observation_date"),
        "event_dates": [None if left is None else left.get("event_date"),
                        None if right is None else right.get("event_date")],
        "evidence_type": EvidenceType.CALCULATION.value,
    }

