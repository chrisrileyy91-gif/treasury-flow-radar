"""Self-contained, compact HTML renderer for the shared research report."""
from __future__ import annotations

import json
from html import escape
from typing import Any

from dashboard.presentation import build_dashboard_view


class _Html(str):
    """Markup this renderer built itself; table cells insert it without re-escaping."""


def _value(value: Any) -> str:
    if isinstance(value, _Html):
        return value
    if value is None or value == "":
        return "UNAVAILABLE"
    if isinstance(value, float):
        rounded = round(value, 2)
        return f"{rounded:.1f}" if rounded.is_integer() else f"{rounded:.2f}".rstrip("0")
    return escape(str(value))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{escape(column)}</th>" for column in headers)
    body = "".join("<tr>" + "".join(f"<td>{_value(value)}</td>" for value in row) + "</tr>" for row in rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}">UNAVAILABLE — NO PRODUCTION OBSERVATIONS</td></tr>'
    return f"<div class=table-wrap><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _status(value: Any) -> str:
    text = _value(value)
    css = "available" if str(value).startswith("AVAILABLE") else "unavailable" if "UNAVAILABLE" in str(value) else "partial"
    return _Html(f'<span class="status {css}">{text}</span>')


def _position_line(item: dict[str, Any]) -> str:
    """Keep CFTC components visible without treating spreading as outright net."""
    def number(value: Any) -> str:
        return "UNAVAILABLE" if value is None else f"{float(value):.1f}"

    return " / ".join(number(item.get(key)) for key in ("long", "short", "spreading", "net"))


def _event_html(event: dict[str, Any]) -> str:
    summary = event["summary"]
    availability = [("Dealer", summary["dealer_available"]), ("CFTC", summary["cftc_available"]),
                    ("Auction", summary["auction_available"]), ("Corporate", summary["corporate_available"]),
                    ("Market", summary["market_confirmation_available"])]
    badges = " ".join(f'<span class="mini {"yes" if present else "no"}">{label}: {"available" if present else "unavailable"}</span>' for label, present in availability)
    market = event["market_move"]
    dealer_rows = [[name, item.get("position") if item else None, item.get("unit") if item else None,
                    item.get("observation_date") if item else None, item.get("lag_days") if item else None,
                    item.get("temporal_relationship") if item else None] for name, item in event["dealer_positioning"].items()]
    cftc_rows = [[item.get("contract"), item.get("positioning_date"), item.get("days_before_event"),
                  item.get("temporal_relationship"), len(item.get("participant_categories", []))]
                 for item in event["cftc_positioning"]]
    auction_rows = [[item.get("security_type"), item.get("security_term"), item.get("auction_date"),
                     item.get("offering_amount"), item.get("bid_to_cover"), item.get("yield_or_rate")]
                    for item in event["treasury_supply"]]
    study = event["event_study"]
    study_rows = [[name, values.get("pre_event_move_bps"), values.get("event_day_move_bps"),
                   values.get("post_event_move_bps"), values.get("post_event_reversal_magnitude_bps"),
                   values.get("post_event_reversal_percent")]
                  for name, values in (study.get("summary") or {}).items()]
    raw = escape(json.dumps(event, indent=2, sort_keys=True, allow_nan=False))
    details = "".join(_table(["Category", "Long", "Short", "Spreading", "Net"], [[p.get("category"), p.get("long"), p.get("short"), p.get("spreading"), p.get("net_position")] for p in contract.get("participant_categories", [])]) for contract in event["cftc_positioning"])
    return f'''<details class="event"><summary><strong>{_value(summary.get("date"))}</strong> · 10Y {_value(summary.get("dgs10_change_bps"))} bp · 2Y {_value(summary.get("dgs2_change_bps"))} bp · curve {_value(summary.get("curve_change_bps"))} bp<br>{badges}</summary>
<div class="event-grid"><section><h3>MARKET MOVE</h3>{_table(["Date", "Prior date", "10Y move", "2Y move", "Curve move", "Evidence"], [[market.get("event_date"), market.get("prior_observation_date"), market.get("change_bps"), market.get("dgs2_change_bps"), market.get("spread_change_bps"), market.get("evidence_type")]])}</section>
<section><h3>DEALER POSITIONING</h3>{_table(["Series", "Position", "Unit", "Observation date", "Lag days", "Relation"], dealer_rows)}</section>
<section><h3>CFTC POSITIONING</h3>{_table(["Contract", "Report date", "Lag days", "Relation", "Categories"], cftc_rows)}<details><summary>Participant details</summary>{details}</details></section>
<section><h3>TREASURY SUPPLY</h3>{_table(["Security", "Term", "Auction date", "Offering", "Bid-to-cover", "Yield/rate"], auction_rows)}</section>
<section><h3>CORPORATE ISSUANCE</h3><p>{_status(event["corporate_issuance"].get("status"))}</p><p>No production corporate event data is implied by this status.</p></section>
<section><h3>MARKET CONFIRMATION</h3><p>{_status(event["market_confirmation"].get("status"))}</p><p>{_value(event["market_confirmation"].get("classification"))}</p></section>
<section><h3>EVENT STUDY</h3><p>{_value(study.get("alignment"))}</p>{_table(["Series", "Pre-event bp", "Event day bp", "Post-event bp", "Reversal bp", "Reversal %"], study_rows)}</section>
<section><h3>PROVENANCE AND LIMITATIONS</h3><ul>{''.join(f'<li>{_value(item)}</li>' for item in event["limitations"])}</ul></section></div>
<details><summary>Raw structured evidence</summary><pre>{raw}</pre></details></details>'''


def render_report(report: dict[str, Any], *, user_event: dict[str, Any] | None = None,
                  allow_event_input: bool = False, event_limit: int = 20) -> str:
    """Render a bounded snapshot; the full raw report is never embedded."""
    view = build_dashboard_view(report, event_limit=event_limit)
    happening = view["happening"]
    d10, d2, spread = happening["dgs10"], happening["dgs2"], happening["spread"]
    dealer, auction = happening["dealer"], happening["auction"]
    system_read = "".join(f'<article class="read"><h3>{_value(item["label"])}</h3><p>{_status(item["value"])}</p><details><summary>Supporting detail · {_value(item["evidence_type"])}</summary><pre>{escape(json.dumps(item["detail"], indent=2, sort_keys=True, allow_nan=False))}</pre></details></article>' for item in view["system_read"])
    cftc_rows = [[item["contract"], item["positioning_date"], item["summary_category"], item["net"], item["net_change"], item["long"], item["short"], item["spreading"], _position_line(item)] for item in happening["cftc"]]
    cftc_detail = "".join(f'<details><summary>{_value(item["contract"])} participant details</summary>{_table(["Category", "Long", "Short", "Spreading", "Net", "Net change", "Report date"], [[d.get("participant_category"), d.get("long"), d.get("short"), d.get("spreading"), d.get("net"), d.get("net_change"), d.get("positioning_date")] for d in item["details"]])}</details>' for item in happening["cftc"])
    evidence = view["evidence"]
    events = "".join(_event_html(event) for event in view["events"]) or "<p>No large DGS10 moves in this snapshot’s available range.</p>"
    rate_lock_rows = [[item["item"], _status(item["status"])] for item in view["rate_lock_status"]]
    provenance_rows = [[row["source"], row["series"], row["observation_date"], row["retrieved"], row["published"], row["evidence_type"], row["reference"]] for row in view["provenance"]]
    user_event_html = ""
    if user_event:
        user_event_html = f'<section><h2>UNVERIFIED USER EVENT</h2><p class=warning>USER-SUPPLIED — UNVERIFIED. It is not a production observation. No production event records are asserted.</p><details><summary>Event details</summary><pre>{escape(json.dumps(user_event, indent=2, sort_keys=True, allow_nan=False))}</pre></details></section>'
    if evidence["treasury_side_complete"]:
        coverage_note = "The database currently holds the Treasury-market side of the hypothesis (yields, curve, dealer and CFTC positioning, auctions) but no production corporate issuance event feed."
    else:
        coverage_note = "The database is also missing Treasury-market evidence listed under Evidence not available, so even the Treasury side of the hypothesis is incomplete."
    if allow_event_input:
        mode_line = "Live view of the local database"
        footer = "Live view: rendered on each page load from the local SQLite database, opened read-only. No API keys or environment values are included in the page."
    else:
        mode_line = "Static snapshot"
        footer = "This is a self-contained snapshot: no network, Python, SQLite, API keys, environment variables, or raw database dump are included."
    event_input = '<form method=get><label>Event date <input name=event_date type=date required></label><label>Event type <input name=event_type></label><button>Build unverified event view</button></form>' if allow_event_input else '<p class=muted>Static snapshot: interactive event input is available in live mode only.</p>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Treasury Flow Radar</title><style>
:root{{--navy:#10243a;--ink:#172334;--muted:#557085;--line:#d9e3eb;--paper:#fff;--bg:#f3f6fa;--green:#17683a;--amber:#875b08;--red:#982d2d}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,"Segoe UI",sans-serif}}header{{background:var(--navy);color:#fff;padding:28px max(18px,calc((100% - 1240px)/2))}}h1,h2,h3{{margin-top:0}}h1{{margin-bottom:4px;font-size:27px}}header p{{margin:5px 0;color:#d7e3ed}}main{{max-width:1240px;margin:20px auto;padding:0 16px}}section{{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:18px;margin:14px 0}}h2{{font-size:20px;color:#173e5c;margin-bottom:12px}}h3{{font-size:15px;margin-bottom:7px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:12px}}.read{{border:1px solid var(--line);border-radius:8px;padding:12px;background:#fbfdff}}.read p{{margin:0 0 7px}}.status{{display:inline-block;padding:3px 7px;border-radius:12px;font-size:12px;font-weight:700;background:#edf2f6}}.status.available{{color:var(--green);background:#e6f4eb}}.status.unavailable{{color:var(--red);background:#fbeaea}}.status.partial{{color:var(--amber);background:#fff4db}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e7edf2;text-align:left;vertical-align:top}}th{{background:#f5f8fb;white-space:nowrap}}details{{margin:8px 0}}summary{{cursor:pointer;font-weight:650}}.event{{border-top:1px solid var(--line);padding:13px 0}}.event-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:10px;margin-top:10px}}.event-grid section{{margin:0;padding:12px}}.mini{{display:inline-block;margin:5px 4px 0 0;padding:2px 5px;border-radius:8px;font-size:11px;background:#eef2f5}}.mini.no{{color:var(--red)}}.mini.yes{{color:var(--green)}}pre{{white-space:pre-wrap;overflow:auto;background:#f5f7f9;padding:12px;border-radius:5px;font-size:12px}}.warning{{border-left:4px solid var(--amber);background:#fff5e7;padding:10px}}.muted{{color:var(--muted)}}ul.compact{{margin:0;padding-left:20px}}footer{{padding:12px 0 28px;color:var(--muted)}}label{{margin-right:8px}}button{{padding:5px 9px}}@media(max-width:600px){{.event-grid{{grid-template-columns:1fr}}}}
</style></head><body><header><h1>TREASURY FLOW RADAR</h1><p>Descriptive Treasury market research · no trading signals</p><p>Generated at: {_value(view["generated_at"])} · {mode_line} · data range {_value(view["scope"].get("start_date"))} to {_value(view["scope"].get("end_date"))}</p></header><main>
<section><h2>SYSTEM READ</h2><p class=muted>Current measurable state, based only on stored observations and documented calculations.</p><div class=grid>{system_read}</div></section>
<section><h2>WHAT IS HAPPENING?</h2><div class=grid><article class=read><h3>10Y Treasury</h3><p>{_value(d10.get("yield_percent"))}% · {_value(d10.get("observation_date"))}</p><p>5 obs: {_value(d10.get("change_5_observations_bps"))} bp · 10 obs: {_value(d10.get("change_10_observations_bps"))} bp</p></article><article class=read><h3>2Y Treasury</h3><p>{_value(d2.get("yield_percent"))}% · {_value(d2.get("observation_date"))}</p><p>5 obs: {_value(d2.get("change_5_observations_bps"))} bp · 10 obs: {_value(d2.get("change_10_observations_bps"))} bp</p></article><article class=read><h3>10Y–2Y curve</h3><p>{_value(spread.get("spread_bps"))} bp · {_value(spread.get("observation_date"))}</p><p>Latest change: {_value(spread.get("daily_change_bps"))} bp</p></article><article class=read><h3>Primary dealers</h3><p>{_status(dealer.get("status"))}</p><p>{_value(dealer.get("position"))} {_value(dealer.get("unit"))}; change {_value(dealer.get("change"))}</p><p>Observation: {_value(dealer.get("observation_date"))} · Retrieved: {_value(dealer.get("retrieval_time"))}</p></article></div><h3>CFTC positioning — compact contract summary</h3>{_table(["Contract", "Report date", "Summary category", "Net", "Net change", "Long", "Short", "Spreading", "Long / short / spreading / net"], cftc_rows)}{cftc_detail}<h3>Most recent Treasury auction</h3>{_table(["Security", "Term", "Auction date", "Offering", "Accepted", "Bid-to-cover", "Yield/rate", "Evidence"], [[auction.get("security_type"), auction.get("security_term"), auction.get("auction_date"), auction.get("offering_amount"), auction.get("accepted_amount"), auction.get("bid_to_cover"), auction.get("yield_or_rate"), auction.get("evidence_type")]])}</section>
<section><h2>EVIDENCE AVAILABLE / NOT AVAILABLE</h2><div class=grid><article class=read><h3>Evidence available</h3><ul class=compact>{''.join(f'<li>{_value(item)}</li>' for item in evidence["available"]) or '<li>None — no Treasury-market observations stored</li>'}</ul></article><article class=read><h3>Evidence not available</h3><ul class=compact>{''.join(f'<li>{_value(item["item"])} — <strong>{_value(item["reason"])}</strong></li>' for item in evidence["not_available"]) or '<li>None</li>'}</ul></article><article class=read><h3>Causality limitation</h3><ul class=compact>{''.join(f'<li>{_value(item)}</li>' for item in evidence["causality_limitation"])}</ul></article></div><p class=muted>UNKNOWN values remain explicitly marked where observations are absent or insufficient.</p></section>
<section><h2>CORPORATE ISSUANCE / RATE-LOCK HYPOTHESIS</h2>{_table(["Research requirement", "Status"], rate_lock_rows)}<p>Production corporate issuance feed unavailable. Synthetic test fixtures are not loaded as production events. {coverage_note} Therefore the system cannot currently test whether large corporate deals systematically coincide with pre-settlement Treasury pressure or post-settlement reversal.</p></section>
<section><h2>RESEARCH EVENTS</h2><p class=muted>Showing the most recent {_value(view["event_limit"])} of {_value(view["event_count"])} detected events. Expand an event for readable evidence; raw structured evidence is collapsed for auditability.</p>{events}</section><section><h2>Event Study — live input</h2>{event_input}</section>{user_event_html}
<section><h2>PROVENANCE AND FRESHNESS</h2><p class=muted>Observation date, retrieval time, and publication time are distinct. Missing publication time remains unavailable.</p>{_table(["Source", "Series", "Observation date", "Retrieved", "Published", "Evidence type", "Reference"], provenance_rows)}</section><section><h2>Evidence taxonomy</h2><p>FACT · CALCULATION · OBSERVATION · MECHANISM · INFERENCE · HYPOTHESIS. Confidence is omitted unless a statistical basis is defined.</p><p>{_value(view["interpretation_limit"])}</p></section><footer>{footer}</footer></main><script>/* static snapshot: no external scripts or network requests */</script></body></html>'''

