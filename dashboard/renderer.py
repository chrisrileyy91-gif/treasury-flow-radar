"""Self-contained, offline HTML renderer for the shared research report."""
from __future__ import annotations

import json
from html import escape
from typing import Any


def _value(value: Any) -> str:
    if value is None:
        return "UNAVAILABLE"
    if isinstance(value, float):
        rounded = round(value, 2)
        return f"{rounded:.1f}" if rounded.is_integer() else f"{rounded:.2f}".rstrip("0")
    return escape(str(value))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{escape(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{_value(v)}</td>" for v in row) + "</tr>" for row in rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}">No production observations available.</td></tr>'
    return f"<div class=table-wrap><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def render_report(report: dict[str, Any], *, user_event: dict[str, Any] | None = None,
                  allow_event_input: bool = False) -> str:
    yields = report.get("yield_changes", {})
    d2 = yields.get("DGS2", [])
    d10 = yields.get("DGS10", [])
    latest_by_series = {"DGS2": d2[-1] if d2 else None, "DGS10": d10[-1] if d10 else None}
    spreads = report.get("spread", [])
    latest_spread = spreads[-1] if spreads else None
    market_rows = []
    for series, item in report.get("market_confirmation", {}).get("instruments", {}).items():
        latest = item.get("latest")
        market_rows.append([series, latest, item.get("unit"), item.get("source_observation_date"),
                            item.get("return_percent"), item.get("status"), item.get("retrieval_time")])
    yield_rows = []
    for series, item in latest_by_series.items():
        yield_rows.append([series, None if item is None else item.get("yield_percent"),
                           None if item is None else item.get("observation_date"),
                           None if item is None else item.get("daily_change_bps"),
                           None if item is None else item.get("change_5_observations_bps"),
                           None if item is None else item.get("change_10_observations_bps")])
    dealer_rows = [[d.get("series_name"), d.get("position"), d.get("previous_position"), d.get("change"),
                    d.get("unit"), d.get("observation_date"), d.get("reporting_frequency"),
                    (d.get("provenance") or {}).get("retrieval_time")] for d in report.get("dealer_positions", [])]
    cftc_rows = [[c.get("contract"), c.get("participant_category"), c.get("long"), c.get("short"),
                  c.get("spreading"), c.get("net"), c.get("net_change"), c.get("positioning_date"),
                  c.get("reporting_frequency"),
                  " / ".join(str(c.get(k)) if c.get(k) is not None else "UNAVAILABLE"
                             for k in ("long", "short", "spreading", "net"))]
                 for c in report.get("cftc_positions", [])]
    auction_rows = [[a.get("security_type"), a.get("security_term"), a.get("auction_date"),
                     a.get("offering_amount"), a.get("offering_amount_unit"), a.get("accepted_amount"),
                     a.get("bid_to_cover"), a.get("yield_or_rate"), a.get("yield_or_rate_unit")] 
                    for a in report.get("treasury_auctions", [])[-30:]]
    events = []
    for wrapped in report.get("events", []):
        event = wrapped["event"]
        details = json.dumps(wrapped, indent=2, sort_keys=True, allow_nan=False).replace("<", "\\u003c")
        events.append(
            "<details class=event><summary>"
            f"{_value(event.get('event_date'))} — DGS10 {_value(event.get('direction'))} "
            f"{_value(event.get('change_bps'))} bp <span>{_value(event.get('evidence_type'))}</span>"
            "</summary><h3>Event details</h3>"
            f"<p>DGS2 change: {_value(event.get('dgs2_change_bps'))} bp; "
            f"10Y–2Y spread change: {_value(event.get('spread_change_bps'))} bp.</p>"
            "<p>Dealer, CFTC, auction, issuance, and event-study context are included below as structured data.</p>"
            f"<pre>{escape(details)}</pre></details>"
        )
    event_html = "".join(events) or "<p>No large DGS10 moves in this snapshot's available date range.</p>"
    corporate = report.get("corporate_issuance", {})
    if corporate.get("status") != "AVAILABLE":
        corporate_html = (
            "<p><strong>Corporate issuance event feed not configured.</strong></p>"
            "<p>Production corporate issuance feed unavailable. Synthetic test fixtures are not loaded as production events.</p>"
        )
    else:
        corporate_html = _table(
            ["Issuer", "Deal", "Principal", "Currency", "Estimated duration", "Pricing", "Settlement"],
            [[e.get("issuer"), e.get("deal_identifier"), e.get("principal_amount"), e.get("currency"),
              e.get("estimated_duration_years"), e.get("fields", {}).get("pricing_date"),
              e.get("fields", {}).get("settlement_date")] for e in corporate.get("events", [])],
        )
    provenance_rows = [[p.get("source_identifier"), p.get("series_identifier"), p.get("source_url"),
                       report.get("data_freshness", {}).get(p.get("source_identifier"), {}).get("observation_date"),
                       report.get("data_freshness", {}).get(p.get("source_identifier"), {}).get("retrieval_time"),
                       report.get("data_freshness", {}).get(p.get("source_identifier"), {}).get("publication_time")]
                      for p in report.get("source_provenance", [])]
    evidence_html = "".join(
        f"<details><summary>{_value(item.get('evidence_type'))}: {_value(item.get('statement'))}</summary>"
        f"<pre>{escape(json.dumps(item, indent=2, sort_keys=True, allow_nan=False))}</pre></details>"
        for item in report.get("evidence", [])
    ) or "<p>No event conclusions generated.</p>"
    user_event_html = ""
    if user_event:
        user_event_html = (
            "<p><strong>USER-SUPPLIED — UNVERIFIED.</strong> No production event records are asserted.</p>"
            f"<pre>{escape(json.dumps(user_event, indent=2, sort_keys=True, allow_nan=False))}</pre>"
        )
    event_input_html = (
        "<form method=get><label>Event date <input name=event_date type=date required></label> "
        "<label>Event type <input name=event_type></label> <button>Build unverified event view</button></form>"
        if allow_event_input else
        "<p>This report is a static snapshot; its event inputs are included only when generated.</p>"
    )
    data = json.dumps(report, sort_keys=True, allow_nan=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TREASURY FLOW RADAR</title><style>
:root{{color-scheme:light;--navy:#10243a;--ink:#182433;--muted:#52677a;--line:#dbe4ec;--paper:#fff;--bg:#f3f6fa}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,"Segoe UI",sans-serif}}
header{{background:var(--navy);color:#fff;padding:24px max(18px,calc((100% - 1200px)/2))}}h1{{margin:0;font-size:25px}}header p{{margin:5px 0;color:#ccd9e4}}main{{max-width:1200px;margin:20px auto;padding:0 16px}}
section{{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:18px;margin:14px 0}}h2{{font-size:19px;color:#173e5c;margin:0 0 10px}}h3{{font-size:16px}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e7edf2;text-align:left;vertical-align:top}}th{{background:#f5f8fb;white-space:nowrap}}.warn{{padding:10px;border-left:4px solid #c27717;background:#fff5e7}}.event{{border-top:1px solid var(--line);padding:10px 0}}summary{{cursor:pointer;font-weight:650}}summary span{{font-size:11px;background:#edf2f6;padding:3px 6px;border-radius:12px}}pre{{white-space:pre-wrap;overflow:auto;background:#f5f7f9;padding:12px;border-radius:5px;font-size:12px}}.muted{{color:var(--muted)}}footer{{padding:18px 0 30px;color:var(--muted)}}
</style></head><body><header><h1>TREASURY FLOW RADAR</h1><p>Descriptive Treasury market research • no trading signals</p><p>Generated at: {_value(report.get('generated_at'))}</p></header>
<main><section><h2>Overview</h2><p>Scope: {_value(report.get('scope',{}).get('start_date'))} to {_value(report.get('scope',{}).get('end_date'))}</p>
<p class=muted>{_value(report.get('interpretation_limit'))}</p><div class=warn>Unavailable or UNKNOWN data are shown explicitly. Observation-date alignment does not establish publication-time availability or causality.</div></section>
<section><h2>Market — yields and curve</h2><p>10Y–2Y latest spread: {_value(None if latest_spread is None else latest_spread.get('spread_bps'))} bp on {_value(None if latest_spread is None else latest_spread.get('observation_date'))} <span>CALCULATION</span></p>
{_table(["Series","Yield (%)","Observation date","Daily change (bp)","5 observations (bp)","10 observations (bp)"],yield_rows)}</section>
<section><h2>Dealer positioning</h2>{_table(["Series","Latest position","Previous","Change","Unit","Observation date","Frequency","Retrieved"],dealer_rows)}</section>
<section><h2>CFTC positioning</h2>{_table(["Contract","Participant","Long","Short","Spreading","Net","Net change","Position date","Frequency","Long / short / spreading / net"],cftc_rows)}</section>
<section><h2>Treasury supply</h2>{_table(["Security","Term","Auction date","Offering","Amount unit","Accepted","Bid-to-cover","Yield/rate","Rate unit"],auction_rows)}</section>
<section><h2>Corporate issuance</h2>{corporate_html}</section>
<section><h2>Market confirmation</h2><p>Status: {_value(report.get('market_confirmation',{}).get('status'))}. Cross-market co-movement is an observation, not proof of causality.</p>{_table(["Instrument","Latest","Unit","Observation date","Return %","Status","Retrieved"],market_rows)}</section>
<section><h2>Research events</h2>{event_html}</section>
<section><h2>Event Study</h2>{event_input_html}{user_event_html}</section>
<section><h2>Evidence classifications</h2><p>FACT · CALCULATION · OBSERVATION · MECHANISM · INFERENCE · HYPOTHESIS. Confidence is omitted unless a statistical basis is defined.</p>{evidence_html}</section>
<section><h2>Data sources, provenance, and freshness</h2>{_table(["Source","Series","Source reference","Latest observation date","Retrieved at","Published at"],provenance_rows)}</section>
<footer>This self-contained snapshot includes its displayed data and styling; it makes no network requests and requires no Python or SQLite to view. Live dashboard and static export use the same report builder.</footer></main>
<script id=research-data type=application/json>{data}</script><script>document.querySelectorAll('details').forEach(d=>d.addEventListener('toggle',()=>d.dataset.open=d.open));</script></body></html>"""

