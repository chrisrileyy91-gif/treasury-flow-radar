"""Descriptive yield-event context with explicit source dates and evidence labels."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from treasury_flow_radar.analytics.attribution import attribute_window
from treasury_flow_radar.analytics.candidates import evaluate_candidates, level_context
from treasury_flow_radar.analytics.decomposition import decompose_moves
from treasury_flow_radar.analytics.descriptive import (
    EvidenceType,
    Observation,
    curve_metrics,
    level_changes,
    positioning_metrics,
    yield_metrics,
)
from treasury_flow_radar.analytics.event_study import MARKET_SERIES, YIELD_SERIES, event_study
from treasury_flow_radar.analytics.evidence import evidence_from_rows
from treasury_flow_radar.analytics.issuance import analyze_issuance_event
from treasury_flow_radar.analytics.market_context import SERIES as CONTEXT_SERIES
from treasury_flow_radar.analytics.market_context import market_context
from treasury_flow_radar.analytics.positioning_moves import unusual_dealer_moves
from treasury_flow_radar.analytics.temporal import align_observation
from treasury_flow_radar.sources.fred_releases import (
    EXCLUDED_RELEASE_IDS,
    MAX_RELEASE_DATES_PER_YEAR,
    RELEASES,
)


def large_yield_moves(
    observations: Iterable[Observation | Mapping[str, Any]],
    *,
    series_id: str = "DGS10",
    threshold_bps: float = 5.0,
) -> list[dict[str, Any]]:
    """Return changes between consecutive valued yield sessions meeting an absolute bp threshold.

    No-value dates (e.g. bond-market closures) are skipped, so the first session after
    a closure is compared with the last session before it; the skipped dates are listed.
    """
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
            "skipped_no_value_dates": [day.isoformat() for day in current.skipped_no_value_dates],
            "calendar_days_since_prior": (current.observation_time - prior.observation_time).days,
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
    deals: list[dict[str, Any]] | None = None,
    fomc_decisions: list[dict[str, Any]] | None = None,
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
        for sid in ("DGS2", "DGS5", "DGS7", "DGS10", "DGS30")
    }
    curves = curve_metrics(observations)
    decomposition = decompose_moves(observations)
    yield_levels = {sid: {m.observation_time: m.yield_percent for m in metrics if m.yield_percent is not None}
                    for sid, metrics in yields.items()}
    market_levels: dict[str, dict[date, float]] = {sid: {} for sid in CONTEXT_SERIES}
    for r in source_rows:
        sid = r.get("series_identifier")
        if sid in CONTEXT_SERIES and r.get("value_numeric") is not None:
            market_levels[sid][date.fromisoformat(str(r["observation_time"])[:10])] = float(r["value_numeric"])
    releases, calendar_notes = _release_calendar(source_rows)
    releases += list(fomc_decisions or [])
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
                "change_20_observations_bps": m.change_20_observations_bps,
                "skipped_no_value_dates": [d.isoformat() for d in m.skipped_no_value_dates],
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
            match = align_observation(series_obs, event_day, direction="prior")
            if match is None:
                contexts.append({"event_date": event["event_date"], "position": None})
                continue
            latest_day = match.source_observation_date
            latest = indexed[latest_day]
            eligible = [day for day in indexed if day <= event_day]
            prior_candidates = [day for day in eligible if day < latest_day]
            prior_day = max(prior_candidates) if prior_candidates else None
            prior = indexed[prior_day] if prior_day else None
            contexts.append({
                "event_date": event["event_date"],
                "position": latest.value,
                "unit": latest.unit,
                "reporting_frequency": latest.frequency,
                "observation_date": latest_day.isoformat(),
                "lag_days": match.lag_days,
                "temporal_relationship": match.relationship,
                "prior_position": prior.value if prior else None,
                "prior_observation_date": prior_day.isoformat() if prior_day else None,
                "change_from_prior_observation": latest.change_from_prior,
                "change_evidence_type": EvidenceType.CALCULATION.value,
                "alignment_rule": "latest observation on or before event; weekly observations are not forward-filled",
                "availability_note": "source does not provide an exact historical publication time; date alignment alone does not establish that the weekly value was published by the event",
                "evidence_type": EvidenceType.FACT.value,
                "provenance": _provenance_for_date(dealer_rows, latest_day),
            })
        dealer_context[series] = contexts

    cftc_rows = [r for r in source_rows if r.get("source_identifier") == "CFTC"]
    # Index CFTC rows by (series, report date) once; provenance lookups then scan only
    # that report's rows instead of every CFTC row. Results are unchanged.
    cftc_index: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for r in cftc_rows:
        cftc_index[(r.get("series_identifier"), date.fromisoformat(str(r["observation_time"])[:10]))].append(r)
    cftc_metrics = positioning_metrics([Observation.from_mapping(r) for r in cftc_rows])
    grouped_cftc: dict[tuple[str, date], dict[str, Any]] = {}
    for metric in cftc_metrics:
        grouped_cftc.setdefault((metric.series_id, metric.observation_time), {})[metric.participant] = metric
    positioning_flags = unusual_dealer_moves(cftc_metrics)
    cftc_summaries = []
    cftc_grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for metric in cftc_metrics:
        cftc_grouped[(metric.series_id, metric.participant)].append(metric)
    for (series, participant), metrics in sorted(cftc_grouped.items()):
        metrics.sort(key=lambda item: item.observation_time)
        latest = metrics[-1]
        previous = metrics[-2] if len(metrics) > 1 else None
        provenance_row = _provenance_for_key(cftc_index.get((series, latest.observation_time), []),
                                             series, latest.observation_time, participant)
        series_source = next((r for r in cftc_rows if r.get("series_identifier") == series), {})
        cftc_summaries.append({
            "contract": series_source.get("series_name") or series,
            "series_identifier": series,
            "participant_category": participant,
            "long": latest.long_contracts,
            "short": latest.short_contracts,
            "spreading": latest.spreading_contracts,
            "net": latest.net_position_contracts,
            "net_change": latest.net_change_contracts,
            "previous_net": previous.net_position_contracts if previous else None,
            "positioning_date": latest.observation_time.isoformat(),
            "reporting_frequency": latest.reporting_frequency,
            "temporal_relationship": "SOURCE OBSERVATION DATE",
            "source_observation_date": latest.observation_time.isoformat(),
            "event_date": None,
            "lag_days": None,
            "evidence_type": EvidenceType.FACT.value,
            "net_evidence_type": EvidenceType.CALCULATION.value,
            "provenance": provenance_row,
        })
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
                "temporal_relationship": "BEFORE" if report_day < event_day else "SAME_DAY",
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
                            cftc_index.get((series, report_day), []), series, report_day, p
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
            "auctions": [
                {**a, "temporal_alignment": {
                    "source_observation_date": a["auction_date"],
                    "event_date": event["event_date"],
                    "lag_days": (event_day - date.fromisoformat(a["auction_date"])).days,
                    "relationship": (
                        "BEFORE" if date.fromisoformat(a["auction_date"]) < event_day
                        else "AFTER" if date.fromisoformat(a["auction_date"]) > event_day
                        else "SAME_DAY"
                    ),
                }} for a in selected
            ],
        })

    corporate_rows = [r for r in source_rows
                      if "corporate" in str(r.get("source_type", "")).casefold()
                      or "corporate_issuance" in str(r.get("source_identifier", "")).casefold()]
    corporate_events = _corporate_events(corporate_rows)
    all_deals = merge_deals(list(deals or []), edgar_deals(corporate_rows))
    corporate_configured = any(
        bool((r.get("source_metadata") or {}).get("event_level_source_selected"))
        for r in corporate_rows
    )
    corporate_status = ("AVAILABLE" if corporate_configured and corporate_events
                        else "Corporate issuance event feed not configured")
    selected_issuance_events = [item for item in corporate_events
                                if item.get("production_source_configured")]
    issuance_analytics = [analyze_issuance_event(item, observations)
                          for item in selected_issuance_events]

    market_summary = _market_confirmation(source_rows)
    evidence_records = []
    for move in moves:
        event_day = date.fromisoformat(move["event_date"])
        prior_day = date.fromisoformat(move["prior_observation_date"])
        support = [r for r in source_rows if r.get("series_identifier", "").upper() == "DGS10"
                   and date.fromisoformat(str(r["observation_time"])[:10]) in {prior_day, event_day}]
        evidence_records.append(evidence_from_rows(
            EvidenceType.OBSERVATION,
            f"A DGS10 change of {move['change_bps']:.6g} basis points was observed on {event_day.isoformat()}.",
            support,
        ).to_dict())

    # The event study reads only yield and market-price series; passing just those rows
    # (instead of every positioning/auction row) gives identical windows far faster.
    study_series = {*YIELD_SERIES, *MARKET_SERIES}
    study_rows = [r for r in source_rows
                  if str(r.get("series_identifier") or "").upper() in study_series]
    event_summaries = []
    for event in moves:
        day = date.fromisoformat(event["event_date"])
        two = yield_by_series_day["DGS2"].get(day)
        curve = curve_by_day.get(day)
        study = event_study(day, study_rows)
        nearby_issuance = []
        for issuance in issuance_analytics:
            issuance_date = _issuance_anchor(issuance)
            if issuance_date is None or abs((day - issuance_date).days) > 5:
                continue
            nearby_issuance.append({
                **issuance,
                "temporal_alignment": {
                    "source_observation_date": issuance_date.isoformat(),
                    "event_date": day.isoformat(),
                    "lag_days": (day - issuance_date).days,
                    "relationship": ("BEFORE" if issuance_date < day else
                                     "AFTER" if issuance_date > day else "SAME_DAY"),
                },
            })
        event_summaries.append({
            "decomposition": decomposition["by_date"].get(event["event_date"]),
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
            "event_study": study,
            "market_confirmation": study["market_confirmation"],
            "corporate_issuance_context": {
                "status": corporate_status,
                "events": nearby_issuance,
            },
        })

    provenance = sorted({
        (str(r.get("source_identifier") or ""), str(r.get("source_url") or ""),
         str(r.get("series_identifier") or "")) for r in source_rows
    })
    dealer_summaries = []
    for series in dealer_series:
        series_metrics = level_changes([o for o in observations if o.series_id == series], series)
        if not series_metrics:
            continue
        latest = series_metrics[-1]
        prior = series_metrics[-2] if len(series_metrics) > 1 else None
        matching = next((r for r in dealer_rows
                         if r.get("series_identifier") == series
                         and date.fromisoformat(str(r["observation_time"])[:10]) == latest.observation_time), None)
        dealer_summaries.append({
            "series_identifier": series,
            "series_name": (matching or {}).get("series_name") or series,
            "position": latest.value,
            "previous_position": prior.value if prior else None,
            "change": latest.change_from_prior,
            "unit": latest.unit,
            "observation_date": latest.observation_time.isoformat(),
            "reporting_frequency": latest.frequency,
            "source_observation_date": latest.observation_time.isoformat(),
            "event_date": None,
            "lag_days": None,
            "temporal_relationship": "SOURCE OBSERVATION DATE",
            "evidence_type": EvidenceType.FACT.value,
            "change_evidence_type": EvidenceType.CALCULATION.value,
            "provenance": _provenance_for_date(dealer_rows, latest.observation_time),
            # Reported weekly values in the report range, for charting; never filled.
            "history": [{"observation_date": m.observation_time.isoformat(), "position": m.value}
                        for m in series_metrics if start_date <= m.observation_time <= end_date],
        })
    for series_id, metrics in yields.items():
        latest = next((m for m in reversed(metrics) if m.yield_percent is not None), None)
        if latest is None:
            continue
        support = [r for r in source_rows if r.get("series_identifier") == series_id
                   and date.fromisoformat(str(r["observation_time"])[:10]) == latest.observation_time]
        evidence_records.append(evidence_from_rows(
            EvidenceType.FACT,
            f"{series_id} was reported at {latest.yield_percent:.6g} percent on {latest.observation_time.isoformat()}.",
            support,
        ).to_dict())
    for dealer in dealer_summaries:
        evidence_records.append(evidence_from_rows(
            EvidenceType.FACT,
            f"NY Fed dealer position for {dealer['series_name']} was {dealer['position']} {dealer['unit']} on {dealer['observation_date']}.",
            [r for r in dealer_rows if r.get("series_identifier") == dealer["series_identifier"]
             and str(r.get("observation_time", ""))[:10] == dealer["observation_date"]],
        ).to_dict())
    category_aliases = {
        "dealer": {"dealer", "dealer_intermediary", "dealer/intermediary"},
        "asset_manager": {"asset_manager", "asset_manager_institutional", "asset manager"},
        "leveraged_fund": {"leveraged_funds", "leveraged fund", "lev_money"},
        "other_reportable": {"other_reportables", "other reportables"},
        "nonreportable": {"nonreportable", "non_reportable"},
    }
    for position in cftc_summaries:
        categories = category_aliases.get(position["participant_category"],
                                          {position["participant_category"]})
        support = [r for r in cftc_rows
                   if r.get("series_identifier") == position["series_identifier"]
                   and str(r.get("observation_time", ""))[:10] == position["positioning_date"]
                   and str((r.get("metadata") or {}).get("participant_category", "")).casefold()
                   in categories]
        evidence_records.append(evidence_from_rows(
            EvidenceType.FACT,
            f"CFTC {position['contract']} {position['participant_category']} report dated {position['positioning_date']}: long {position['long']}, short {position['short']}, spreading {position['spreading']} contracts.",
            support,
        ).to_dict())
        if position["net"] is not None:
            evidence_records.append(evidence_from_rows(
                EvidenceType.CALCULATION,
                f"CFTC outright net position was {position['net']} contracts (long minus short); spreading is excluded.",
                support,
            ).to_dict())
    for auction in auctions[-30:]:
        auction_id = (auction.get("provenance") or {}).get("source_record_id")
        support = [r for r in auction_rows if (r.get("metadata") or {}).get("source_identifier") == auction_id]
        evidence_records.append(evidence_from_rows(
            EvidenceType.FACT,
            f"Treasury auction {auction.get('security_term')} {auction.get('security_type')} occurred on {auction['auction_date']}.",
            support,
        ).to_dict())
    # Per source: the most recent retrieval and, separately, the most recent observation
    # date that carries a value. (Previously the observation date was taken from whichever
    # row had the latest retrieval time, which could be the oldest observation.)
    latest_retrieval: dict[str, dict[str, str | None]] = {}
    for row in source_rows:
        source = str(row.get("source_identifier") or "")
        if not source:
            continue
        entry = latest_retrieval.setdefault(
            source, {"retrieval_time": None, "observation_date": None, "publication_time": None})
        retrieval = str(row.get("retrieval_time") or "")
        if retrieval and retrieval > str(entry["retrieval_time"] or ""):
            entry["retrieval_time"] = retrieval
            entry["publication_time"] = row.get("publication_time")
        observed = str(row.get("observation_time") or "")[:10]
        has_value = row.get("value_numeric") is not None or row.get("value_text") is not None
        if observed and has_value and observed > str(entry["observation_date"] or ""):
            entry["observation_date"] = observed
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
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
        "move_decomposition": {"latest": decomposition["latest"],
                               "term_premium": decomposition["term_premium"]},
        "level": level_context(yield_levels.get("DGS10", {})),
        "candidates": evaluate_candidates(
            yields=yield_levels, decomposition_by_date=decomposition["by_date"],
            auctions=auctions, releases=releases, deals=all_deals, market=market_levels),
        "market_context": market_context(
            ten_year=yield_levels.get("DGS10", {}), market=market_levels,
            window=sorted(yield_levels.get("DGS10", {}))[-5:]),
        "attribution": attribute_window(
            yields=yield_levels, decomposition_by_date=decomposition["by_date"],
            deals=all_deals, auctions=auctions, releases=releases),
        "calendar_notes": calendar_notes,
        "curated_deal_count": len(deals or []),
        "edgar_deal_count": len(all_deals) - len(deals or []),
        "dealer_positions": dealer_summaries,
        "cftc_positions": cftc_summaries,
        "positioning_moves": positioning_flags,
        "treasury_auctions": auctions,
        "event_study_windows": ["T-5 through T+5", "T-3 through T+3", "T-1 through T+1"],
        "market_confirmation": market_summary,
        "corporate_issuance": {
            "status": corporate_status,
            "events": issuance_analytics,
            "source_feed_configured": corporate_configured,
        },
        "evidence": evidence_records,
        "availability": {
            "corporate_issuance": corporate_status,
            "market_confirmation": market_summary["status"],
            "NYFED": "AVAILABLE" if dealer_summaries else "UNAVAILABLE",
            "CFTC": "AVAILABLE" if cftc_summaries else "UNAVAILABLE",
            "Treasury auctions": "AVAILABLE" if auctions else "UNAVAILABLE",
            "FRED yields": "AVAILABLE" if any(yields.values()) else "UNAVAILABLE",
        },
        "data_freshness": latest_retrieval,
        "source_provenance": [
            {"source_identifier": s, "source_url": url, "series_identifier": series}
            for s, url, series in provenance
        ],
        "evidence_taxonomy": [e.value for e in EvidenceType],
        "interpretation_limit": "Descriptive measurements only; timing and co-occurrence do not establish causality or intent.",
    }


def edgar_deals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild deals from stored EDGAR tranche facts, in the ledger's deal shape."""
    usable = []
    for row in rows:
        metadata = row.get("metadata") or {}
        native = metadata.get("source_native_fields") or {}
        fields = metadata.get("issuance_event")
        if isinstance(fields, dict) and native.get("filing_url") and metadata.get("deal_identifier"):
            usable.append((int(native.get("parser_version") or 0), int(row.get("revision") or 0), metadata, fields, native))
    # Only tranches written by the newest parser count: every run re-parses all stored filings,
    # so a tranche an older parser produced and the current one rejects has no current-version row.
    newest = max((u[0] for u in usable), default=0)
    tranches: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for _version, _revision, metadata, fields, native in sorted(
            (u for u in usable if u[0] == newest), key=lambda u: u[1]):
        tranches[metadata["deal_identifier"]][metadata["source_record_id"]] = {**fields, "_native": native}
    deals = []
    for deal_id, items in tranches.items():
        first = next(iter(items.values()))
        parts = []
        for item in items.values():
            year = None
            for key in ("benchmark_maturity", "maturity_date"):   # hedged tenor: benchmark when stated
                if item.get(key):
                    year = int(str(item[key])[:4])
                    break
            year = year or item["_native"].get("maturity_year")
            parts.append({"currency": item.get("currency") or "USD", "amount": item.get("principal_amount"),
                          "coupon_percent": item.get("coupon"), "maturity_year": year,
                          "benchmark": bool(item.get("benchmark_maturity"))})
        size = sum(float(p["amount"] or 0) for p in parts)
        pricing = first.get("pricing_date") or first.get("settlement_date")
        if not pricing:
            continue
        deals.append({"id": f"edgar-{deal_id}", "name": f"{first.get('issuer_name')} notes",
                      "pricing_date": pricing, "launch_date": pricing,
                      "settlement_date_expected": first.get("settlement_date"), "size_usd": size,
                      "tranches": parts, "origin": "SEC EDGAR",
                      "sources": [{"url": first["_native"]["filing_url"], "title": f"SEC filing ({first['_native'].get('form')})",
                                   "supports": ["pricing_date", "settlement_date_expected", "size_usd", "tranches"]}],
                      "extraction": first["_native"].get("extraction")})
    return sorted(deals, key=lambda d: (d["pricing_date"], d["id"]))


def merge_deals(curated: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Curated ledger first; a discovered deal with the same pricing date and size within 5% is a duplicate."""
    merged = list(curated)
    for deal in discovered:
        duplicate = any(c["pricing_date"] == deal["pricing_date"] and c.get("size_usd") and deal.get("size_usd")
                        and abs(c["size_usd"] - deal["size_usd"]) <= 0.05 * c["size_usd"] for c in curated)
        if not duplicate:
            merged.append(deal)
    return merged


def _release_calendar(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Release dates for configured FRED releases, dropping any that are not a real calendar."""
    by_release: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        if not str(r.get("series_identifier") or "").startswith("FRED_RELEASE_"):
            continue
        rid = (r.get("metadata") or {}).get("release_id")
        if rid in EXCLUDED_RELEASE_IDS or rid not in RELEASES:
            continue
        by_release[int(rid)].add(str(r.get("observation_time"))[:10])
    releases, notes = [], []
    for rid, days in sorted(by_release.items()):
        ordered = sorted(days)
        span_years = max((date.fromisoformat(ordered[-1]) - date.fromisoformat(ordered[0])).days / 365.25, 1.0)
        if len(ordered) / span_years > MAX_RELEASE_DATES_PER_YEAR:
            notes.append(f"{RELEASES[rid]['name']} excluded: {len(ordered)} dates is not a publication calendar.")
            continue
        info = RELEASES[rid]
        releases.extend({"date": d, "release_id": rid, "name": info["name"], "short": info["short"],
                         "kind": info["kind"], "weight": info["weight"]} for d in ordered)
    return releases, notes


def _corporate_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for row in rows:
        metadata = row.get("metadata") or {}
        record_id = str(metadata.get("source_record_id") or "")
        fields = metadata.get("issuance_event")
        if not record_id or not isinstance(fields, dict):
            continue
        events.setdefault(record_id, {
            "source_record_id": record_id,
            "fields": fields,
            "duration_method": metadata.get("duration_method"),
            "duration_kind": metadata.get("duration_classification"),
            "source_identifier": row.get("source_identifier"),
            "source_url": row.get("source_url"),
            "publication_time": row.get("publication_time"),
            "retrieval_time": row.get("retrieval_time"),
            "raw_record_id": row.get("raw_record_id"),
            "production_source_configured": bool(
                (row.get("source_metadata") or {}).get("event_level_source_selected")
            ),
        })
        if metadata.get("source_field") == "duration_years_estimate":
            events[record_id]["duration_years"] = row.get("value_numeric")
    return [events[key] for key in sorted(events)]


def _issuance_anchor(event: Mapping[str, Any]) -> date | None:
    fields = event.get("fields") or {}
    raw = fields.get("pricing_date") or fields.get("settlement_date")
    return date.fromisoformat(str(raw)[:10]) if raw else None


def _market_confirmation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ident = str(row.get("series_identifier") or "").upper()
        if ident in MARKET_SERIES and row.get("value_numeric") is not None:
            by_series[ident].append(row)
    instruments = {}
    for ident in MARKET_SERIES:
        ordered = sorted(by_series.get(ident, []), key=lambda r: str(r.get("observation_time")))
        if not ordered:
            instruments[ident] = {"status": "Market confirmation unavailable", "latest": None}
            continue
        current = ordered[-1]
        previous = ordered[-2] if len(ordered) > 1 else None
        prior = None if previous is None else float(previous["value_numeric"])
        change = (None if prior in (None, 0) else
                  (float(current["value_numeric"]) / prior - 1) * 100)
        instruments[ident] = {
            "status": "AVAILABLE" if change is not None else "INSUFFICIENT OBSERVATIONS",
            "latest": current["value_numeric"],
            "unit": current.get("unit"),
            "source_observation_date": str(current.get("observation_time"))[:10],
            "prior_observation_date": str(previous.get("observation_time"))[:10] if previous else None,
            "return_window_days": ((date.fromisoformat(str(current.get("observation_time"))[:10]) -
                                    date.fromisoformat(str(previous.get("observation_time"))[:10])).days
                                   if previous else None),
            "event_date": str(current.get("observation_time"))[:10],
            "lag_days": 0,
            "temporal_relationship": "SAME_DAY",
            "return_percent": change,
            "source_identifier": current.get("source_identifier"),
            "source_url": current.get("source_url"),
            "retrieval_time": current.get("retrieval_time"),
            "evidence_type": EvidenceType.CALCULATION.value,
        }
    available = any(value.get("status") == "AVAILABLE" for value in instruments.values())
    return {
        "status": "AVAILABLE" if available else "Market confirmation unavailable",
        "classification": "OBSERVATION — cross-market co-movement" if available else None,
        "instruments": instruments,
        "causality_established": False,
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
            "source_record_id": auction_id,
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
            "offering_amount_unit": item.get("offering_amt_unit") or "us_dollars",
            "accepted_amount": item.get("total_accepted"),
            "accepted_amount_unit": item.get("total_accepted_unit") or "us_dollars",
            "bid_to_cover": item.get("bid_to_cover_ratio"),
            "yield_or_rate": item.get(rate_field) if rate_field else None,
            "yield_or_rate_field": rate_field,
            "yield_or_rate_unit": item.get(f"{rate_field}_unit") if rate_field else None,
            "cusip": item.get("cusip"),
            "provenance": provenance.get(key),
            "evidence_type": EvidenceType.FACT.value,
        })
    return sorted(result, key=lambda item: (item["auction_date"], item.get("cusip") or ""))

