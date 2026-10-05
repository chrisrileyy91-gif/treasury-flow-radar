"""Dependency-free local HTTP dashboard for the normalized Treasury Flow Radar DB."""

from __future__ import annotations

import argparse
from datetime import date
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dashboard.research import Event, build_event_window, evidence_summary, load_observations


def _fmt(value: object) -> str:
    if value is None:
        return "UNKNOWN"
    return escape(str(value))


def _table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_fmt(x)}</td>" for x in row) + "</tr>" for row in rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="empty">No matching observations. Values remain UNKNOWN.</td></tr>'
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def render_dashboard(database_path: str | Path, query: dict[str, list[str]] | None = None) -> str:
    observations = load_observations(database_path)
    query = query or {}
    event_rows = []
    event_date = (query.get("event_date") or [""])[0]
    if event_date:
        try:
            day = date.fromisoformat(event_date)
            event = Event((query.get("event_id") or ["user-event"])[0], day,
                          (query.get("event_type") or ["Research event"])[0],
                          (query.get("issuer") or [None])[0],
                          date.fromisoformat(query["pricing_date"][0]) if query.get("pricing_date", [""])[0] else None,
                          date.fromisoformat(query["settlement_date"][0]) if query.get("settlement_date", [""])[0] else None,
                          float(query["size"][0]) if query.get("size", [""])[0] else None,
                          (query.get("size_unit") or [None])[0], (query.get("notes") or [None])[0],
                          source="USER-SUPPLIED — UNVERIFIED")
            event_rows = build_event_window(event, observations)
        except ValueError:
            event_date = ""
    treasury = [r for r in observations if r["series_identifier"].upper() in {"DGS2", "DGS10"}]
    yield_rows = []
    for ident in ("DGS2", "DGS10"):
        series = sorted((r for r in treasury if r["series_identifier"].upper() == ident), key=lambda r: r["observation_time"])
        last = [r for r in series if r["value_numeric"] is not None]
        latest = last[-1] if last else None
        prior = last[-2] if len(last) > 1 else None
        five = last[-6] if len(last) > 5 else None
        delta = None if latest is None or prior is None else f"{(latest['value_numeric']-prior['value_numeric'])*100:+.2f} bp"
        delta5 = None if latest is None or five is None else f"{(latest['value_numeric']-five['value_numeric'])*100:+.2f} bp"
        yield_rows.append([ident, None if latest is None else latest["value_numeric"],
                           None if latest is None else latest["observation_time"][:10], delta, delta5,
                           None if latest is None else latest["source_name"],
                           None if latest is None else latest["retrieval_time"],
                           "UNKNOWN" if latest is None else latest["freshness"]])
    curve = None
    d2 = {r["observation_time"][:10]: r["value_numeric"] for r in treasury if r["series_identifier"].upper() == "DGS2" and r["value_numeric"] is not None}
    d10 = {r["observation_time"][:10]: r["value_numeric"] for r in treasury if r["series_identifier"].upper() == "DGS10" and r["value_numeric"] is not None}
    common = sorted(d2.keys() & d10.keys())
    if common:
        curve = f"{(d10[common[-1]] - d2[common[-1]]) * 100:+.2f} bp on {common[-1]}"
    dealer = [r for r in observations if "nyfed" in r["source_identifier"].lower() or "dealer" in r["source_name"].lower()]
    futures = [r for r in observations if "cftc" in r["source_identifier"].lower()]
    supply = [r for r in observations if "auction" in r["source_identifier"].lower()]
    if not supply:
        supply = [r for r in observations if "treasury" in r["source_identifier"].lower()
                  and "security_type" in r["metadata"]]
    confirm = [r for r in observations if r["series_identifier"].upper() in {"HYG", "IWM", "DXY", "ZN", "UB", "ZB"}]
    confirm_rows = [[r["series_identifier"], r["value_numeric"], r["unit"], r["observation_time"][:10], r["freshness"]] for r in confirm[-20:]]
    dealer_groups: dict[str, list[dict[str, object]]] = {}
    for row in dealer:
        dealer_groups.setdefault(str(row["series_identifier"]), []).append(row)
    dealer_rows = []
    for ident, entries in sorted(dealer_groups.items()):
        entries.sort(key=lambda item: str(item["observation_time"]))
        latest = entries[-1]
        previous = entries[-2] if len(entries) > 1 else None
        current_value, prior_value = latest["value_numeric"], None if previous is None else previous["value_numeric"]
        change = None if current_value is None or prior_value is None else float(current_value)-float(prior_value)
        pct = None if change is None or prior_value in (None, 0) else 100*change/float(prior_value)
        dealer_rows.append([latest["series_name"], current_value, prior_value, change, pct,
                            latest["unit"] or latest["default_unit"], latest["observation_time"][:10],
                            latest["source_name"], latest["freshness"]])
    report_groups: dict[tuple[str, str, str], dict[str, dict[str, object]]] = {}
    for r in futures:
        meta = r["metadata"]
        participant, metric = meta.get("participant_category"), meta.get("metric")
        if participant and metric:
            report_date = str(meta.get("report_date") or r["observation_time"][:10])
            key = (r["series_name"], str(participant), report_date)
            report_groups.setdefault(key, {})[str(metric)] = r
    grouped: dict[tuple[str, str], list[tuple[str, dict[str, dict[str, object]]]]] = {}
    for (contract, participant, report_date), measures in report_groups.items():
        grouped.setdefault((contract, participant), []).append((report_date, measures))
    futures_rows = []
    for (contract, participant), entries in sorted(grouped.items()):
        entries.sort(key=lambda item: item[0])
        report_date, current = entries[-1]
        previous = entries[-2][1] if len(entries) > 1 else {}
        long = current.get("long", {}).get("value_numeric")
        short = current.get("short", {}).get("value_numeric")
        net = None if long is None or short is None else float(long) - float(short)
        old_long = previous.get("long", {}).get("value_numeric")
        old_short = previous.get("short", {}).get("value_numeric")
        old_net = None if old_long is None or old_short is None else float(old_long) - float(old_short)
        spread = current.get("spreading", {}).get("value_numeric")
        template = next(iter(current.values()))
        futures_rows.append([contract, participant, "long / short / spreading / net",
                             f"{long} / {short} / {spread} / {net}", template.get("unit"), report_date,
                             None if net is None or old_net is None else net - old_net,
                             template.get("retrieval_time"), template.get("frequency") or "weekly"])
    supply_rows = []
    for r in supply[-30:]:
        meta = r["metadata"]
        dates = meta.get("dates", {})
        supply_rows.append([meta.get("security_type"), meta.get("security_term"), dates.get("announcement_date"),
                            dates.get("auction_date") or r["observation_time"][:10], dates.get("issue_date"), dates.get("maturity_date"),
                            f"{meta.get('source_field')}: {r['value_numeric']}", r["unit"],
                            "REOPENING" if meta.get("new_issue_or_reopening") == "reopening" else "NEW ISSUE" if meta.get("new_issue_or_reopening") == "new_issue" else "UNKNOWN"])
    event_table = []
    for r in event_rows:
        event_table.append([f"T{r['offset']:+d}", r["date"], r["yield_2y"], r["yield_2y_change_bps"], r["yield_10y"], r["yield_10y_change_bps"], r["curve_bps"],
                            "available" if r["nyfed"] else "UNKNOWN", "available" if r["cftc"] else "UNKNOWN",
                            "available" if r["auctions"] else "UNKNOWN", "available" if r["prices"] else "UNKNOWN"])
    evidence = "".join(f"<li><b>{escape(item['type'])}:</b> {escape(item['text'])}</li>" for item in evidence_summary(observations))
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TREASURY FLOW RADAR</title><style>
body{{margin:0;background:#f3f6fa;color:#182433;font:15px/1.5 system-ui,Segoe UI,sans-serif}}header{{background:#10243a;color:white;padding:26px max(22px,calc((100% - 1200px)/2))}}main{{max-width:1200px;margin:24px auto;padding:0 18px}}h1{{margin:0;font-size:25px;letter-spacing:.04em}}header p{{margin:6px 0 0;color:#c9d6e2}}section{{background:#fff;border:1px solid #dce4ec;border-radius:10px;padding:18px;margin:16px 0;box-shadow:0 2px 8px #132a3d0b}}h2{{margin:0 0 12px;font-size:19px;color:#163957}}h3{{margin-bottom:5px}}.note{{color:#536779}}.pill{{display:inline-block;background:#e7edf3;border-radius:99px;padding:2px 9px;font-size:12px;font-weight:700}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e6ebf0;text-align:left;vertical-align:top}}th{{background:#f5f8fb;white-space:nowrap}}.table-wrap{{overflow:auto}}.empty{{color:#64788b}}ul{{padding-left:22px}}input,button{{font:inherit;padding:8px;border:1px solid #aebdca;border-radius:5px}}button{{background:#153c5d;color:white;cursor:pointer}}form{{display:flex;gap:8px;flex-wrap:wrap;align-items:end}}label{{display:grid;font-size:12px;color:#536779}}details summary{{cursor:pointer;color:#15537b}}footer{{color:#687b8c;padding:12px 0 30px;font-size:13px}}
</style></head><body><header><h1>TREASURY FLOW RADAR</h1><p>Descriptive Treasury market plumbing research • no trading signals</p></header><main>
<section><h2>1. Treasury Market</h2><p><b>10Y − 2Y curve spread:</b> {_fmt(curve)} <span class="pill">CALCULATION</span></p>{_table(["Series","Latest value","Observation date","Latest change","5-observation change","Source","Retrieved","Freshness"],yield_rows)}</section>
<section><h2>2. Dealer Positioning</h2><p class="note">Aggregate primary-dealer net position; reported values retain source-native units and do not imply a directional bet. Percentage change is omitted when the prior value is zero or missing.</p>{_table(["Series","Latest","Previous","Change","Change %","Native unit","Observation date","Source","Freshness"],dealer_rows)}</section>
<section><h2>3. Futures Positioning</h2><p class="note">CFTC observations are report-date observations (typically weekly). Spreading is displayed separately from long and short; net = long − short and change is versus the prior report.</p>{_table(["Contract","Participant","Components","Long / short / spreading / net","Unit","Report date","Net change","Retrieved","Frequency"],futures_rows)}</section>
<section><h2>4. Treasury Supply</h2><p class="note">Treasury auction facts only; no auction prediction. Source-native offering units are retained.</p>{_table(["Security type","Term","Announcement","Auction","Issue","Maturity","Reported value","Unit","Issue type"],supply_rows)}</section>
<section><h2>5. Corporate Issuance</h2><p><b>Production corporate issuance feed unavailable.</b> Synthetic test fixtures are not loaded into the dashboard as production observations.</p></section>
<section><h2>6. Market Confirmation</h2><p class="note">HYG, IWM, DXY and Treasury futures appear only if normalized price observations exist.</p>{_table(["Instrument","Value","Unit","Observation date","Freshness"],confirm_rows)}<p>Status: {'STALE' if confirm and any(r["freshness"] == "STALE" for r in confirm) else 'AVAILABLE' if confirm else 'NOT AVAILABLE'}</p></section>
<section><h2>7. Evidence / System Read</h2><p><b>Evidence status: INSUFFICIENT EVIDENCE</b></p><p>Production corporate event data are unavailable; the dashboard does not infer causality or assign confidence scores.</p><ul>{evidence}</ul><details><summary>Evidence taxonomy</summary><p>FACT: directly reported source value. CALCULATION: reproducible arithmetic. OBSERVATION: measured co-movement without cause. MECHANISM: a process that could operate. INFERENCE: interpretation beyond direct measurement. HYPOTHESIS: an explanation requiring tests. No inference is generated without a stated evidential rule.</p></details></section>
<section><h2>8. Data Freshness / Provenance</h2><p class="note">Freshness limits: daily 5 days, weekly 14 days, monthly 45 days since retrieval. Unsupported frequency or missing retrieval time is UNKNOWN.</p>
{_table(["Source","Series","Observation","Publication","Retrieval","Frequency","Freshness","Raw record"],[[r["source_name"],r["series_identifier"],r["observation_time"],r["publication_time"],r["retrieval_time"],r["frequency"],r["freshness"],r["raw_record_id"]] for r in observations[-100:]])}</section>
<section><h2>9. Event Study</h2><p class="note">Descriptive alignment on actual Treasury yield observation dates. T offsets count supplied yield observations around the event; weekly sources stay weekly. Missing values remain UNKNOWN. User-entered events are unverified and are not added to production data.</p>
<form method="get"><label>Event ID<input name="event_id" value="{escape((query.get('event_id') or ['user-event'])[0])}"></label><label>Event date<input name="event_date" type="date" value="{escape(event_date)}" required></label><label>Event type<input name="event_type" value="{escape((query.get('event_type') or [''])[0])}" placeholder="Research event"></label><label>Issuer<input name="issuer" value="{escape((query.get('issuer') or [''])[0])}" placeholder="Optional"></label><label>Pricing date<input name="pricing_date" type="date" value="{escape((query.get('pricing_date') or [''])[0])}"></label><label>Settlement date<input name="settlement_date" type="date" value="{escape((query.get('settlement_date') or [''])[0])}"></label><label>Size<input name="size" type="number" step="any" value="{escape((query.get('size') or [''])[0])}"></label><label>Size unit<input name="size_unit" value="{escape((query.get('size_unit') or [''])[0])}"></label><label>Notes<input name="notes" value="{escape((query.get('notes') or [''])[0])}"></label><button type="submit">View window</button></form>
{'<p><b>USER-SUPPLIED — UNVERIFIED</b> Event date '+escape(event_date)+'. No production event records are asserted.</p>'+_table(["Offset","Actual observation date","2Y (%)","2Y Δ since T−1 (bp)","10Y (%)","10Y Δ since T−1 (bp)","10Y−2Y (bp)","NY Fed","CFTC","Auction","Price confirmation"],event_table) if event_rows else '<p>No production event records are available. Enter a research event date to inspect an observation-aligned window.</p>'}</section>
<footer>Source facts remain distinct from calculations and hypotheses. Co-movement does not establish causality; event comparisons are descriptive only.</footer></main></body></html>"""
    return html


def make_server(database_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            body = render_dashboard(database_path, query).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Treasury Flow Radar dashboard")
    parser.add_argument("--database", default="data/treasury_flow_radar.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.database, args.host, args.port)
    print(f"Treasury Flow Radar: http://{args.host}:{server.server_port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

