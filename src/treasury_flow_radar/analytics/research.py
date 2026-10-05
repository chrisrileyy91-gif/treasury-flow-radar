"""Descriptive yield-event context with explicit source dates and evidence labels."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from treasury_flow_radar.analytics.descriptive import (
    EvidenceType,
    Observation,
    curve_metrics,
    level_changes,
    positioning_metrics,
    yield_metrics,
)


def large_yield_moves(
    observations: Iterable[Observation | Mapping[str, Any]],
    *,
    series_id: str = "DGS10",
    threshold_bps: float = 5.0,
) -> list[dict[str, Any]]:
    """Return adjacent observed yield changes meeting an absolute bp threshold."""
    if not math.isfinite(threshold_bps) or threshold_bps < 0:
        raise ValueError("threshold_bps must be finite and nonnegative")
    rows = yield_metrics(observations, series_id)
    events = []
    for index, current in enumerate(rows):
        change = current.daily_change_bps
        if index == 0 or change is None or (
            abs(change) < threshold_bps and not math.isclose(abs(change), threshold_bps, abs_tol=1e-9)
        ):
            continue
        prior = rows[index - 1]
        if current.yield_percent is None or prior.yield_percent is None:
            continue
        events.append({
            "event_date": current.observation_time.isoformat(),
            "prior_observation_date": prior.observation_time.isoformat(),
            "prior_yield_percent": prior.yield_percent,
            "current_yield_percent": current.yield_percent,
            "change_bps": change,
            "direction": "UP" if change > 0 else "DOWN",
            "evidence_type": EvidenceType.OBSERVATION.value,
        })
    return events


def build_research_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    start_date: date,
    end_date: date,
    threshold_bps: float = 5.0,
    auction_window_days: int = 3,
) -> dict[str, Any]:
    """Build a JSON-ready report from normalized observation rows.

    Input rows are expected to include source/series identifiers and provenance
    columns returned by :func:`load_observations_read_only`.
    """
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if auction_window_days < 0:
        raise ValueError("auction_window_days must be nonnegative")
    source_rows = [dict(row) for row in rows]
    observations = [Observation.from_mapping(row) for row in source_rows]

    def source_rows_for(identifier: str) -> list[dict[str, Any]]:
        return [r for r in source_rows if r.get("source_identifier") == identifier]

    yields = {
        sid: yield_metrics(observations, sid)
        for sid in ("DGS2", "DGS10")
    }
    curves = curve_metrics(observations)
    curve_by_day = {m.observation_time: m for m in curves}
    yield_by_series_day = {
        sid: {m.observation_time: m for m in metrics} for sid, metrics in yields.items()
    }
    report_yields: dict[str, list[dict[str, Any]]] = {}
    for sid, metrics in yields.items():
        report_yields[sid] = [
            {
                "observation_date": m.observation_time.isoformat(),
                "yield_percent": m.yield_percent,
                "daily_change_percentage_points": m.daily_change_percentage_points,
                "daily_change_bps": m.daily_change_bps,
                "change_5_observations_bps": m.change_5_observations_bps,
                "change_10_observations_bps": m.change_10_observations_bps,
                "rolling_volatility_5_bps": m.rolling_volatility_5_bps,
                "evidence_type": EvidenceType.CALCULATION.value,
            }
            for m in metrics if start_date <= m.observation_time <= end_date
        ]

    moves = [m for m in large_yield_moves(observations, threshold_bps=threshold_bps)
             if start_date <= date.fromisoformat(m["event_date"]) <= end_date]

    dealer_rows = [r for r in source_rows if r.get("source_identifier") == "NYFED"]
    dealer_series = sorted({r["series_identifier"] for r in dealer_rows
                            if r.get("series_identifier")})
    dealer_context: dict[str, list[dict[str, Any]]] = {}
    for series in dealer_series:
        series_obs = [o for o in observations if o.series_id == series]
        metrics = level_changes(series_obs, series)
        indexed = {m.observation_time: m for m in metrics}
        contexts = []
        for event in moves:
            event_day = date.fromisoformat(event["event_date"])
            eligible = [day for day in indexed if day <= event_day]
            if not eligible:
                contexts.append({"event_date": event["event_date"], "position": None})
                continue
            latest_day = max(eligible)
            latest = indexed[latest_day]
            prior_candidates = [day for day in eligible if day < latest_day]
            prior_day = max(prior_candidates) if prior_candidates else None
            prior = indexed[prior_day] if prior_day else None
            contexts.append({
                "event_date": event["event_date"],
                "position": latest.value,
                "unit": latest.unit,
                "reporting_frequency": latest.frequency,
                "observation_date": latest_day.isoformat(),
                "prior_position": prior.value if prior else None,
                "prior_observation_date": prior_day.isoformat() if prior_day else None,
                "change_from_prior_observation": latest.change_from_prior,
                "change_evidence_type": EvidenceType.CALCULATION.value,
                "temporal_relationship": "latest observation on or before event; weekly observations are not forward-filled",
                "availability_note": "source does not provide an exact historical publication time; date alignment alone does not establish that the weekly value was published by the event",
                "evidence_type": EvidenceType.FACT.value,
                "provenance": _provenance_for_date(dealer_rows, latest_day),
            })
        dealer_context[series] = contexts

    cftc_rows = [r for r in source_rows if r.get("source_identifier") == "CFTC"]
    cftc_metrics = positioning_metrics([Observation.from_mapping(r) for r in cftc_rows])
    grouped_cftc: dict[tuple[str, date], dict[str, Any]] = {}
    for metric in cftc_metrics:
        grouped_cftc.setdefault((metric.series_id, metric.observation_time), {})[metric.participant] = metric
    cftc_context = []
    for event in moves:
        event_day = date.fromisoformat(event["event_date"])
        by_contract = []
        for series in sorted({s for s, _ in grouped_cftc}):
            prior_dates = [d for s, d in grouped_cftc if s == series and d <= event_day]
            if not prior_dates:
                continue
            report_day = max(prior_dates)
            participants = grouped_cftc[(series, report_day)]
            series_source = next((r for r in cftc_rows if r.get("series_identifier") == series), {})
            by_contract.append({
                "contract": series_source.get("series_name") or series,
                "series_identifier": series,
                "positioning_date": report_day.isoformat(),
                "days_before_event": (event_day - report_day).days,
                "evidence_type": EvidenceType.FACT.value,
                "lag_evidence_type": EvidenceType.CALCULATION.value,
                "reporting_frequency": "weekly",
                "availability_note": "CFTC report date is retained as the observation date; publication time is unavailable, so date alignment does not establish public availability by the event",
                "participant_categories": [
                    {
                        "category": p,
                        "long": m.long_contracts,
                        "short": m.short_contracts,
                        "spreading": m.spreading_contracts,
                        "net_position": m.net_position_contracts,
                        "net_evidence_type": EvidenceType.CALCULATION.value,
                        "evidence_type": EvidenceType.FACT.value,
                        "provenance": _provenance_for_key(
                            cftc_rows, series, report_day, p
                        ),
                    }
                    for p, m in sorted(participants.items())
                ],
            })
        cftc_context.append({"event_date": event["event_date"], "contracts": by_contract})

    auction_rows = [r for r in source_rows if r.get("source_identifier") == "U.S. Treasury Fiscal Data"]
    auctions = _auction_records(auction_rows)
    auction_context = []
    for event in moves:
        event_day = date.fromisoformat(event["event_date"])
        selected = [a for a in auctions
                    if abs((date.fromisoformat(a["auction_date"]) - event_day).days)
                    <= auction_window_days]
        auction_context.append({
            "event_date": event["event_date"],
            "window_calendar_days": auction_window_days,
            "evidence_type": EvidenceType.OBSERVATION.value,
            "auctions": selected,
        })

    event_summaries = []
    for event in moves:
        day = date.fromisoformat(event["event_date"])
        two = yield_by_series_day["DGS2"].get(day)
        curve = curve_by_day.get(day)
        event_summaries.append({
            "event": {
                **event,
                "dgs2_change_bps": two.daily_change_bps if two else None,
                "spread_10y_minus_2y_percentage_points": (
                    curve.spread_10y_minus_2y_percentage_points if curve else None
                ),
                "spread_10y_minus_2y_bps": curve.spread_10y_minus_2y_bps if curve else None,
                "spread_change_bps": curve.daily_change_bps if curve else None,
            },
            "dealer_context": {
                s: next((c for c in cs if c["event_date"] == event["event_date"]), None)
                for s, cs in dealer_context.items()
            },
            "cftc_context": next(c for c in cftc_context if c["event_date"] == event["event_date"]),
            "auction_context": next(c for c in auction_context if c["event_date"] == event["event_date"]),
        })

    provenance = sorted({
        (str(r.get("source_identifier") or ""), str(r.get("source_url") or ""),
         str(r.get("series_identifier") or "")) for r in source_rows
    })
    return {
        "schema_version": 1,
        "scope": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "method": {
            "yield_event_series": "DGS10",
            "large_move_threshold_bps": threshold_bps,
            "large_move_rule": "absolute change between adjacent supplied DGS10 observations",
            "auction_window_calendar_days": auction_window_days,
            "missing_dates": "not fabricated or interpolated",
            "weekly_series": "aligned to latest reported observation on or before event; never expanded to daily rows",
            "publication_timing": "NY Fed and CFTC publication_time values remain NULL when the sources do not provide an exact historical timestamp; observation-date alignment is not an availability assertion",
        },
        "yield_changes": report_yields,
        "spread": [
            {"observation_date": m.observation_time.isoformat(),
             "spread_percentage_points": m.spread_10y_minus_2y_percentage_points,
             "spread_bps": m.spread_10y_minus_2y_bps,
             "daily_change_bps": m.daily_change_bps,
             "evidence_type": EvidenceType.CALCULATION.value}
            for m in curves if start_date <= m.observation_time <= end_date
        ],
        "events": event_summaries,
        "source_provenance": [
            {"source_identifier": s, "source_url": url, "series_identifier": series}
            for s, url, series in provenance
        ],
        "evidence_taxonomy": [e.value for e in EvidenceType],
        "interpretation_limit": "Descriptive measurements only; timing and co-occurrence do not establish causality or intent.",
    }


def _provenance_for_date(rows: list[dict[str, Any]], day: date) -> dict[str, Any] | None:
    candidates = [r for r in rows if date.fromisoformat(str(r["observation_time"])[:10]) == day]
    if not candidates:
        return None
    r = candidates[0]
    return {"source_identifier": r.get("source_identifier"), "source_url": r.get("source_url"),
            "observation_id": r.get("observation_id"), "raw_record_id": r.get("raw_record_id"),
            "retrieval_time": r.get("retrieval_time"), "publication_time": r.get("publication_time")}


def _provenance_for_key(rows: list[dict[str, Any]], series: str, day: date,
                        participant: str) -> dict[str, Any] | None:
    aliases = {
        "dealer": {"dealer", "dealer_intermediary", "dealer/intermediary"},
        "asset_manager": {"asset_manager", "asset_manager_institutional", "asset manager"},
        "leveraged_fund": {"leveraged_funds", "leveraged fund", "lev_money"},
    }
    accepted = aliases.get(participant, {participant, participant.replace("_", " ")})
    match = next((r for r in rows if r.get("series_identifier") == series
                  and date.fromisoformat(str(r["observation_time"])[:10]) == day
                  and (r.get("metadata") or {}).get("participant_category", "").casefold()
                  in accepted), None)
    if not match:
        return None
    return {"source_identifier": match.get("source_identifier"), "source_url": match.get("source_url"),
            "observation_id": match.get("observation_id"), "raw_record_id": match.get("raw_record_id"),
            "retrieval_time": match.get("retrieval_time"), "publication_time": match.get("publication_time")}


def _auction_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = defaultdict(dict)
    provenance: dict[str, dict[str, Any]] = {}
    for row in rows:
        meta = row.get("metadata") or {}
        auction_id = str(meta.get("source_identifier") or row.get("logical_key") or row.get("observation_id"))
        field = str(meta.get("source_field") or "")
        item = records[auction_id]
        if field:
            item[field] = row.get("value_numeric")
            item[f"{field}_unit"] = row.get("unit")
        dates = meta.get("dates") or {}
        if dates.get("auction_date"):
            item["auction_date"] = dates["auction_date"]
        elif row.get("observation_time"):
            item["auction_date"] = str(row["observation_time"])[:10]
        for key in ("security_type", "security_term", "cusip", "reopening"):
            if meta.get(key) is not None:
                item[key] = meta[key]
        provenance[auction_id] = {
            "source_identifier": row.get("source_identifier"), "source_url": row.get("source_url"),
            "observation_id": row.get("observation_id"), "raw_record_id": row.get("raw_record_id"),
            "retrieval_time": row.get("retrieval_time"), "publication_time": row.get("publication_time"),
        }
    result = []
    for key, item in records.items():
        if not item.get("auction_date"):
            continue
        rate_field = next((name for name in ("high_yield", "high_investment_rate",
                           "high_discnt_rate", "int_rate") if item.get(name) is not None), None)
        result.append({
            "auction_date": item["auction_date"],
            "security_type": item.get("security_type"),
            "security_term": item.get("security_term"),
            "offering_amount": item.get("offering_amt"),
            "offering_amount_unit": item.get("offering_amt_unit") or "thousand_us_dollars",
            "accepted_amount": item.get("total_accepted"),
            "accepted_amount_unit": item.get("total_accepted_unit") or "thousand_us_dollars",
            "bid_to_cover": item.get("bid_to_cover_ratio"),
            "yield_or_rate": item.get(rate_field) if rate_field else None,
            "yield_or_rate_field": rate_field,
            "yield_or_rate_unit": item.get(f"{rate_field}_unit") if rate_field else None,
            "cusip": item.get("cusip"),
            "provenance": provenance.get(key),
            "evidence_type": EvidenceType.FACT.value,
        })
    return sorted(result, key=lambda item: (item["auction_date"], item.get("cusip") or ""))

