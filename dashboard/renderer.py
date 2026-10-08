"""Self-contained, phone-first HTML renderer for the shared research report.

The page is one HTML file with inline CSS and JavaScript and no network requests.
Charts are drawn client-side from a small embedded JSON payload; every chart also
has a plain table, so the content is readable without JavaScript.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from html import escape
from typing import Any

from dashboard.presentation import CFTC_GROUPS, MATURITIES, build_dashboard_view

MINUS = "−"


class _Html(str):
    """Markup this renderer built itself; inserted without re-escaping."""


# ---------------------------------------------------------------- formatting

def _text(value: Any) -> str:
    if isinstance(value, _Html):
        return value
    return escape(str(value))


def _unknown() -> _Html:
    return _Html('<span class="unknown">Unknown</span>')


def _num(value: Any, digits: int = 2) -> _Html | str:
    if value is None:
        return _unknown()
    return f"{float(value):.{digits}f}"


def _pct(value: Any) -> _Html | str:
    return _unknown() if value is None else f"{float(value):.2f}%"


def _bp(value: Any, *, unit: bool = True) -> _Html | str:
    if value is None:
        return _unknown()
    rounded = round(float(value))
    sign = "+" if rounded > 0 else MINUS if rounded < 0 else ""
    return f"{sign}{abs(rounded)}{' bp' if unit else ''}"


def _usd_bn(value: Any, *, signed: bool = False) -> _Html | str:
    if value is None:
        return _unknown()
    value = float(value)
    sign = ("+" if value > 0 else MINUS if value < 0 else "") if signed else (MINUS if value < 0 else "")
    return f"{sign}${abs(value):,.1f}B"


def _contracts(value: Any, *, signed: bool = True) -> _Html | str:
    if value is None:
        return _unknown()
    value = float(value)
    sign = ("+" if value > 0 else MINUS if value < 0 else "") if signed else (MINUS if value < 0 else "")
    magnitude = abs(value)
    if magnitude >= 1e6:
        body = f"{magnitude / 1e6:.2f}M"
    elif magnitude >= 1e3:
        body = f"{magnitude / 1e3:.1f}K"
    else:
        body = f"{magnitude:.0f}"
    return sign + body


def _day(value: Any, *, year: bool = True) -> _Html | str:
    if not value:
        return _unknown()
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return _text(value)
    return f"{parsed:%b} {parsed.day}, {parsed.year}" if year else f"{parsed:%b} {parsed.day}"


def _instant(value: Any) -> str:
    if not value:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        return str(value)
    parsed = parsed.astimezone(UTC)
    return f"{parsed:%b} {parsed.day}, {parsed.year} {parsed:%H:%M} UTC"


def _nowrap_terms(text: str) -> str:
    """Keep terms like "10-year" on one line in running text."""
    return re.sub(r"(\d+-year)", r'<span class="nw">\1</span>', text)


def _friendly_dates(text: str) -> str:
    """Show ISO dates inside sentences as "Sep 30"."""
    return re.sub(r"\b(\d{4}-\d{2}-\d{2})\b", lambda m: str(_day(m.group(1), year=False)), text)


def _tag(kind: str) -> _Html:
    return _Html(f'<span class="tag">{escape(kind)}</span>')


def _table(headers: list[tuple[str, str]], rows: list[list[Any]], *, caption: str | None = None,
           empty: str = "No observations stored yet.") -> str:
    """headers: (label, 'l'|'r'). Cells pass through _text; _Html cells are inserted as-is."""
    head = "".join(f'<th scope="col" class="{align}">{escape(label)}</th>' for label, align in headers)
    if rows:
        body = "".join(
            "<tr>" + "".join(f'<td class="{headers[i][1]}">{_text(cell)}</td>' for i, cell in enumerate(row)) + "</tr>"
            for row in rows)
    else:
        body = f'<tr><td colspan="{len(headers)}" class="l"><span class="unknown">{escape(empty)}</span></td></tr>'
    cap = f"<caption>{escape(caption)}</caption>" if caption else ""
    return f'<div class="table-wrap"><table>{cap}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


# ---------------------------------------------------------------- sections

def _header(view: dict[str, Any], live: bool) -> str:
    mode = "Live view of the local database" if live else "Static snapshot"
    return f'''<header class="site">
<div class="bar"><a class="brand" href="./">Treasury Flow Radar</a>
<nav aria-label="Pages"><a href="./" aria-current="page">Treasury market</a></nav></div>
<p class="meta">Data through <strong>{_day(view["data_through"])}</strong>. {mode}. Generated at: {escape(_instant(view["generated_at"]))}.</p>
</header>'''


def _happening(view: dict[str, Any]) -> str:
    curve = view["curve"]
    read = view["curve_read"]
    lead = (f'<p class="lead">{_nowrap_terms(escape(read))} {_tag("Calculation")}</p>' if read else
            '<p class="lead"><span class="unknown">Not enough yield observations to describe the curve.</span></p>')
    rows = [[r["label"], _pct(r["yield"]), _bp(r["d1"], unit=False), _bp(r["d5"], unit=False),
             _bp(r["d20"], unit=False)] for r in curve["rows"]]
    have_rows = any(r["yield"] is not None for r in curve["rows"])
    spread = view["spread"]
    spread_line = ""
    if spread and spread.get("spread_bps") is not None:
        spread_line = (f'<p class="note">10-year minus 2-year: <strong>{_bp(spread["spread_bps"])}</strong>'
                       f' on {_day(spread["date"])}, {_bp(spread["daily_change_bps"])} on the day.</p>')
    history = view["ten_year_history"]
    threshold = view.get("threshold_bps")
    marks = ""
    if history["events"]:
        marks = (f' Ticks along the bottom mark sessions where the 10-year moved at least {_bp(threshold).replace("+", "")}'
                 if threshold is not None else " Ticks along the bottom mark large moves")
        marks += "; see the list further down."
    return f'''<section id="happening" aria-labelledby="h-happening">
<h1 id="h-happening">What is happening?</h1>
{_level(view.get("level"))}
{lead}
{_candidate_block(view)}
{_market_block(view.get("market_context") or {})}
{_x_block(view.get("x_take"))}
{_channel(view["channel"])}
<figure class="chart" data-chart="curve" aria-label="Treasury yield curve: latest versus 5 and 20 sessions earlier">
<figcaption>Yield curve: latest versus 5 and 20 sessions earlier</figcaption>
<div class="legend" data-legend="curve"></div><div class="plot"></div></figure>
{_table([("Maturity", "l"), ("Yield", "r"), ("1 day", "r"), ("5 days", "r"), ("20 days", "r")],
        rows if have_rows else [], caption="Changes in basis points, counted in trading sessions")}
{spread_line}
<figure class="chart" data-chart="tenyear" aria-label="10-year Treasury yield, last six months">
<figcaption>10-year yield, last {len(history["points"])} sessions.{escape(marks)}</figcaption>
<div class="plot"></div></figure>
<p class="source">Source: FRED constant-maturity yields, end of day. Market-closed dates are skipped, never filled. {_tag("Fact")}</p>
</section>'''


def _level(level: dict[str, Any] | None) -> str:
    if not level:
        return '<p class="level"><span class="unknown">No 10-year yield stored yet.</span></p>'
    if level["highest_in_history"]:
        context = f"the highest close in the stored history (since {_day(level['history_start'])})"
    else:
        context = f"highest since {_day(level['last_at_or_above'])}"
    return (f'<p class="level"><span class="level-figure">10-year {float(level["percent"]):.2f}%</span> '
            f'<span class="level-tag">{escape(level["label"])}</span> '
            f'<span class="level-context">{_ordinal(level["percentile"])} percentile of {level["sessions"]} sessions; '
            f'{context}. As of {_day(level["date"])}. {_tag("Calculation")}</span></p>')


def _ordinal(value: float) -> str:
    n = round(value)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


MARKS = {"pass": ("✓", "Passed"), "fail": ("✗", "Failed"), "n/a": ("–", "Not applicable")}


def _x_block(take: dict[str, Any] | None) -> str:
    if not take:
        return ""
    from urllib.parse import quote
    link = "https://x.com/intent/post?text=" + quote(take["text"], safe="")
    return f'''<h2 class="sub-q" id="h-xtake">X take</h2>
<blockquote class="xtake" id="xtake-text">{escape(take["text"])}</blockquote>
<p class="xtake-actions"><button type="button" onclick="navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('xtake-text').textContent).then(()=>{{this.textContent='Copied'}})">Copy</button>
<a href="{escape(link)}" target="_blank" rel="noopener">Post on X</a>
<span class="small">{take["length"]} of {take["limit"]} characters</span></p>
<p class="small">Written automatically from the numbers on this page, every run. Every figure is the page's own; the driver is the
formula's best fit (an inference), never a confirmed cause. Nothing here is a trading signal. {_tag("Inference")}</p>'''


def _market_day(md: dict[str, Any] | None) -> str:
    if not md:
        return '<p class="note">Other markets that day: <span class="unknown">not stored</span>.</p>'
    cells = md["session"]["cells"]
    parts = []
    for s in md["series"]:
        c = cells.get(s["id"]) or {}
        if c.get("change") is None:
            continue
        mark = "*" if c.get("unusual") else ""
        unit = " bp" if s["kind"] == "bp" else ""
        parts.append(f'{escape(s["short"])} {_signed(c["change"], s["kind"])}{unit}{mark}')
    read = md["session"].get("read")
    return (f'<p class="note">Other markets that day: {", ".join(parts)} (* = bigger than a usual day).'
            f'{" " + escape(read) if read else ""} {_tag("Observation")}</p>')


def _signed(value: float | None, unit: str) -> str:
    if value is None:
        return '<span class="unknown">pending</span>'
    text = f"{value:+.1f}%" if unit == "percent" else f"{value:+.0f}"
    if text.lstrip("+-") in ("0.0%", "0"):
        return text.lstrip("+-")
    return text.replace("-", "\u2212")


def _market_block(ctx: dict[str, Any]) -> str:
    if ctx.get("status") != "AVAILABLE":
        return ""
    series = ctx["series"]
    head = [("Date", "l"), ("10Y", "r")] + [(s["short"], "r") for s in series]
    head_html = "".join(f'<th scope="col" class="{a}">{escape(h)}</th>' for h, a in head)
    rows, reads = [], []
    for r in ctx["sessions"]:
        cells = [f'<td class="l">{_day(r["date"], year=False)}</td>', f'<td class="r">{_signed(r["ten_year_bps"], "bp")}</td>']
        for s in series:
            c = r["cells"].get(s["id"]) or {}
            text = _signed(c.get("change"), s["kind"])
            cells.append(f'<td class="r">{"<strong>" + text + "*</strong>" if c.get("unusual") else text}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
        if r.get("read"):
            reads.append(f'<li><strong>{_day(r["date"], year=False)}</strong>: {escape(r["read"])}</li>')
    typical = "; ".join(f'{escape(s["label"])} ±{s["typical"]:.1f}{"%" if s["kind"] == "percent" else " bp"}'
                        for s in series if s.get("typical"))
    lag = [s for s in series if s["id"] == "DTWEXBGS"]
    lag_note = (f' The dollar index is published weekly; it currently runs through {_day(lag[0]["latest"], year=False)}.'
                if lag else "")
    return f'''<h2 class="sub-q" id="h-markets">What did other markets do?</h2>
<p class="small">Same sessions. 10Y = 10-year yield, bp. S&amp;P = S&amp;P 500, %. HY and IG = high-yield and investment-grade
credit spreads, bp (wider = more worried). USD = Fed broad dollar index, %. An asterisk
marks a move bigger than that market's usual day ({typical}).{lag_note} {_tag("Observation")}</p>
<div class="table-wrap"><table><thead><tr>{head_html}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<ul class="limits">{"".join(reads)}</ul>
<p class="small">A pattern here is consistent with a story (for example a flight to safety), not evidence of who traded or why.
Source: FRED (S&amp;P Dow Jones Indices, ICE Data Indices, Federal Reserve H.10).</p>'''


def _share_pct(value: float) -> str:
    """A 0..1 share as a whole percent with a true minus sign."""
    return f"{value:.0%}".replace("-", "\u2212")


def _calibration_table(cand: dict[str, Any]) -> str:
    cal = cand.get("calibration") or {}
    rows = cand.get("calibration_rows") or []
    if not rows:
        return ""
    body = []
    for r in rows:
        if r.get("measured"):
            clear = "Yes" if r["distinguishable"] else "No"
            body.append(f'<tr><td>{escape(r["type"])}</td><td class="r">{r["days"]}</td>'
                        f'<td class="r">{r["event_rms_bps"]:.1f}</td><td class="r">{r["prior"]:.2f}</td>'
                        f'<td class="r">{_share_pct(r["low"])} to {_share_pct(r["high"])}</td><td>{clear}</td></tr>')
        else:
            prior = "" if r.get("prior") is None else f'{r["prior"]:.2f}'
            body.append(f'<tr><td>{escape(r["type"])}</td><td class="r">{r["days"]}</td><td>–</td>'
                        f'<td class="r">{prior}</td><td colspan="2" class="wrap"><span class="unknown">Not measured</span>: '
                        f'{escape(r["basis"])}</td></tr>')
    body.append(f'<tr><td>Corporate deal</td><td>–</td><td>–</td><td class="r">≤ {cal.get("deal_prior_cap", 1):.2f}</td>'
                f'<td colspan="2" class="wrap"><span class="unknown">Not measured</span>: size rule, capped at the strongest measured event</td></tr>')
    span = ""
    if cal.get("start") and cal.get("end"):
        span = f"{_day(cal['start'])} to {_day(cal['end'])}, {cal['sessions']} sessions"
    return f'''<details class="more"><summary>Where the weights come from (measured)</summary>
<p class="small">Each event type's prior is measured from {escape(span)}, before the window being explained. Ordinary days
(no scheduled event, {cal.get("quiet_days", 0)} of them) moved the 10-year about {float(next((r["quiet_rms_bps"] for r in rows if r.get("quiet_rms_bps")), 0)):.1f} bp on a typical day.
The prior is the share of an event day's movement that ordinary days do not have. {_tag("Calculation")}</p>
<div class="table-wrap"><table><thead><tr><th>Event</th><th class="r">Days</th><th class="r">Move, bp</th>
<th class="r">Prior</th><th class="r">90% range</th><th>Clear?</th></tr></thead>
<tbody>{"".join(body)}</tbody></table></div>
<p class="small">"Move" is the typical (root-mean-square) daily change on those days. "Clear?" is Yes only when the whole 90% range is above zero, meaning those days were clearly more volatile than ordinary days. A negative estimate means those days were calmer than
ordinary days; the prior is then 0. Two years is a short sample: most ranges are wide, and the numbers will update as history grows.</p></details>'''


def _candidate_block(view: dict[str, Any]) -> str:
    cand = view.get("candidates") or {}
    window = cand.get("window")
    span = f"{_day(window['start'], year=False)} to {_day(window['end'], year=False)}" if window else "the latest sessions"
    if cand.get("verdict"):
        verdict = f'<p class="verdict">{_nowrap_terms(escape(_friendly_dates(cand["verdict"])))} {_tag("Inference")}</p>'
    elif cand.get("status") == "INSUFFICIENT DATA":
        verdict = '<p class="verdict"><span class="unknown">Not enough yield history to test candidates.</span></p>'
    else:
        verdict = ('<p class="verdict">No candidate in the calendar for this window. '
                   '<span class="unknown">Unknown</span>: other news and deals not in the ledger are not covered.</p>')
    gap = cand.get("unexplained_biggest_day")
    if gap:
        verdict += (f'<p class="note">The window\'s largest 10-year session, {_day(gap["date"], year=False)} '
                    f'({_bp(gap["change_bps"])}), had no release or auction in the calendar that day. '
                    f'<span class="unknown">Unknown</span> driver. {_tag("Observation")}</p>')
    bars = cand.get("bars") or []
    if bars:
        rows = "".join(
            f'<div class="bar-row{" unexpl" if b.get("unexplained") else ""}"><span class="bar-name">{escape(b["name"])}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{max(b["share"], 0.01) * 100:.1f}%"></span></span>'
            f'<span class="bar-val">{b["share"]:.0%}</span></div>' for b in bars)
        verdict += (f'<div class="bars" role="img" aria-label="Share of the window\'s 10-year movement by candidate">{rows}</div>'
                    f'<p class="small">Share of the 10-year\'s total movement ({float(cand.get("total_bps") or 0):.0f} bp of '
                    f'session-by-session moves, {escape(span)}) assigned to each candidate by the formula below.</p>')
    for n, w in enumerate(cand.get("why") or []):
        steps = "".join(f"<li>{escape(_friendly_dates(x))}</li>" for x in w["lines"])
        price = f'<p class="small">{escape(w["price_note"])}</p>' if w.get("price_note") else ""
        verdict += f'''<details class="why"{" open" if n == 0 else ""}><summary>Why {escape(w["name"])}?</summary>
<p>{escape(w["rank_text"])} {_tag("Calculation")}</p><ul class="why-steps">{steps}</ul>
<p>{escape(_friendly_dates(w["history"]))}</p>
<p><strong>How it moves yields.</strong> {escape(w["mechanism"])} {_tag("Mechanism")}</p>
<p><strong>What we don't know.</strong> <span class="unknown">Unknown</span>: {escape(w["unknown"])}</p>{price}</details>'''
    items = []
    labels = cand.get("type_labels") or {}
    for c in cand.get("items") or []:
        checks = "".join(
            f'<li class="chk {c2["status"].replace("/", "")}"><span class="mark" aria-label="{MARKS[c2["status"]][1]}">'
            f'{MARKS[c2["status"]][0]}</span> <strong>{escape(c2["check"])}</strong>: {escape(_friendly_dates(c2["detail"]))}</li>'
            for c2 in c["checks"])
        dates = ""
        if c["type"] == "corporate_deal":
            d = c.get("dates") or {}
            parts = [f"launched {_day(d['launch_date'], year=False)}" if d.get("launch_date") else "",
                     f"priced {_day(d['pricing_date'], year=False)}",
                     (f"notes expected to settle {_day(d['settlement_date_expected'], year=False)} (not confirmed)"
                      if d.get("settlement_date_expected") else ""),
                     f"acquisition closed {_day(d['transaction_close_date'], year=False)}" if d.get("transaction_close_date") else ""]
            dates = "; ".join(p for p in parts if p)
            links = ", ".join(f'<a href="{escape(src["url"])}">{escape(src.get("title") or "source")}</a>'
                              for src in c.get("sources") or [])
            dates = f'<p class="small">{escape(dates)}. Sources: {links}</p>'
        else:
            dates = f'<p class="small">{escape(labels.get(c["type"], ""))} on {_day(c["date"])}.</p>'
        share_text = "" if c.get("share") is None else f"{c['share']:.0%} · "
        items.append(f'''<details class="cand"{" open" if not items else ""}><summary><span class="cand-name">{escape(c["name"])}</span>
<span class="score">{share_text}{c["passed"]} of {c["applicable"]} checks</span></summary>
{dates}<ul class="checks">{checks}</ul>
<p class="small">{_tag("Mechanism")} {escape(c.get("mechanism") or "")}</p></details>''')
    if cand.get("hidden_count"):
        items.append(f'<p class="small">{cand["hidden_count"]} more candidates in the window were credited with no movement.</p>')
    limits = "".join(f"<li>{escape(x)}</li>" for x in cand.get("limitations") or [])
    return f'''<h2 class="sub-q" id="h-candidates">What could explain it?</h2>
<p class="small">Sessions {escape(span)}. Each candidate is credited with the part of each day's 10-year move that its mechanism fits; the rest is unexplained. Corporate deals are checked against the rate-lock pattern: yields rise before pricing, led by the long end, and the long end reverses after.</p>
{verdict}
{"".join(items)}
{_calibration_table(cand)}
<details class="more"><summary>The formula and its assumptions</summary><ul class="limits">{"".join(f"<li>{escape(x)}</li>" for x in cand.get("assumptions") or [])}<li>Each session's absolute 10-year move is split across the candidates active that day in proportion to prior × fit; whatever their weights do not cover is unexplained.</li></ul></details>
<details class="more"><summary>What this ranking cannot tell you</summary><ul class="limits">{limits}<li>Hedge trades are not observable in public data, so a deal can only ever be a consistent candidate, never a confirmed cause.</li></ul></details>'''


def _channel(channel: dict[str, Any]) -> str:
    one, five = channel.get("one") or {}, channel.get("five") or {}
    def cell(window: dict[str, Any], key: str) -> Any:
        return _bp(window.get(key), unit=False) if window else _unknown()
    rows = [["10-year (nominal)", cell(one, "nominal_bps"), cell(five, "nominal_bps")],
            ["Real yield (TIPS)", cell(one, "real_bps"), cell(five, "real_bps")],
            ["Inflation breakeven", cell(one, "breakeven_bps"), cell(five, "breakeven_bps")]]
    gaps = [w.get("gap_bps") for w in (one, five) if w and w.get("gap_bps") is not None]
    if any(abs(g) >= 1 for g in gaps):
        rows.append(["Gap (nominal − real − breakeven)", cell(one, "gap_bps"), cell(five, "gap_bps")])
    def shape(window: dict[str, Any]) -> Any:
        curve = (window or {}).get("curve")
        return _unknown() if not curve else curve["name"]
    curve_line = (f'<p class="note">Curve shape (2-year vs 30-year): {_text(shape(one))} over 1 day, '
                  f'{_text(shape(five))} over 5 days.</p>')
    read = channel.get("read")
    lead = (f'<p class="lead-sm">{_nowrap_terms(escape(read))} {_tag("Calculation")}</p>' if read
            else '<p class="lead-sm"><span class="unknown">Not enough data to split the move yet.</span></p>')
    tp = channel.get("term_premium")
    if tp:
        change = "" if tp.get("change_bps") is None else f", {_bp(tp['change_bps'])} over its last {tp['sessions']} published sessions"
        tp_line = (f'<p class="note">Term premium (Federal Reserve model estimate, 10-year zero-coupon): '
                   f'<strong>{float(tp["percent"]):.2f}%</strong> on {_day(tp["date"])}{change}. '
                   f'It is published about a week late, so it cannot yet speak to the latest sessions. {_tag("Calculation")}</p>')
    else:
        tp_line = '<p class="note">Term premium estimate: <span class="unknown">not stored yet</span>.</p>'
    end = channel.get("date")
    caption = f"Basis points, through {_day(end, year=False) if end else 'latest session'}. Real yield and breakeven are FRED market measures; any gap between them and the nominal change is shown, not assumed away."
    return f'''<h2 class="sub-q" id="h-channel">Through which channel?</h2>
{lead}
{_table([("Component", "l"), ("1 day", "r"), ("5 days", "r")], rows, caption=caption)}
{curve_line}
{tp_line}
<details class="more"><summary>What these components can and cannot tell you</summary>
<p class="note">{_tag("Mechanism")} A rise in the <strong>real yield</strong> reflects higher expected real policy rates or a higher term premium. A rise in <strong>breakevens</strong> reflects more inflation compensation, which also carries an inflation-risk premium and TIPS liquidity effects. Pressure from Treasury supply or hedging flows would be expected to show up mainly in the real yield and the term premium, concentrated at the long end; a move led by the 2-year points more toward policy expectations.</p>
<p class="note">{_tag("Hypothesis")} These are possible explanations, not findings. A split of the move says which channel moved, not why it moved.</p></details>'''


def _dealers(view: dict[str, Any]) -> str:
    dealer = view["dealer"]
    if dealer is None:
        body = '<p class="lead-sm"><span class="unknown">No primary dealer observations stored yet.</span></p>'
    else:
        if dealer["position_bn"] is None:
            figure = f'{_text(dealer["position_raw"])} {_text(dealer["unit"])}'
        else:
            figure = _usd_bn(dealer["position_bn"])
        body = f'''<p class="lead-sm"><strong class="figure">{figure}</strong> net position in nominal Treasuries, as of {_day(dealer["date"])}.
{_usd_bn(dealer["change_bn"], signed=True)} from {_usd_bn(dealer["previous_bn"])} the week before. {_tag("Fact")}</p>
<figure class="chart" data-chart="dealer" aria-label="Primary dealer net Treasury position, weekly">
<figcaption>Weekly net position, last {len(dealer["history"])} reports</figcaption><div class="plot"></div></figure>'''
    return f'''<section id="dealers" aria-labelledby="h-dealers">
<h2 id="h-dealers">Dealer positioning</h2>
{body}
<p class="note">This is the aggregate of all primary dealers (long minus short, excluding TIPS), reported weekly by the New York Fed. Dealers hold inventory to make markets for customers, so a large position is a measure of balance-sheet use, not a directional bet.</p>
<p class="source">Source: NY Fed Primary Dealer Statistics, series PDPOSGST-TOT. Exact publication time is not supplied.</p>
</section>'''


def _moves(moves: dict[str, Any]) -> str:
    rows = moves.get("rows") or []
    if not rows:
        return ""
    flagged = moves.get("flagged") or []
    if not flagged:
        return (f'<p class="note">No unusual dealer move this week: every contract\'s weekly change was within '
                f'{moves["threshold_sd"]:.0f} standard deviations of its history. {_tag("Calculation")}</p>')
    items = []
    for r in flagged:
        rank = ("the largest weekly change" if r["larger_weeks"] == 0 else
                f"larger than all but {r['larger_weeks']} weekly change{'s' if r['larger_weeks'] > 1 else ''}")
        items.append(f'<li><strong>{escape(r["contract"])}</strong>: {rank} in {r["weeks"]} weeks of history '
                     f'(about {abs(r["z"]):.1f}× a typical week). {escape(r["read"] or "")}</li>')
    return f'''<div class="flag-box"><h3>Unusual dealer moves this week</h3><ul class="limits">{"".join(items)}</ul>
<p class="small">Flagged when a weekly change is at least {moves["threshold_sd"]:.0f} standard deviations of that contract's past weekly changes. Each flag is checked against two ordinary explanations from the same report: undoing the prior week, and taking the other side of asset managers. {_tag("Calculation")} {_tag("Inference")}</p></div>'''


def _futures(view: dict[str, Any]) -> str:
    cftc = view["cftc"]
    short = {"dealer": "Dealers", "asset_manager": "Asset mgrs", "leveraged_fund": "Lev. funds"}
    headers = [("Contract", "l")] + [(short.get(key, label), "r") for key, label in CFTC_GROUPS]
    rows = []
    if cftc:
        for row in cftc["rows"]:
            cells: list[Any] = [row["contract"]]
            for key, _ in CFTC_GROUPS:
                group = row["groups"].get(key)
                if group is None or group.get("net") is None:
                    cells.append(_unknown())
                else:
                    cells.append(_Html(f'<span class="cell-main">{_text(_contracts(group["net"]))}</span>'
                                       f'<span class="cell-sub">{_text(_contracts(group["change"]))} wk</span>'))
            rows.append(cells)
    date_line = f" Report date {_day(cftc['date'])}." if cftc else ""
    return f'''<section id="futures" aria-labelledby="h-futures">
<h2 id="h-futures">Futures positioning</h2>
<p class="lead-sm">Net futures contracts by trader group.{date_line} {_tag("Fact")} {_tag("Calculation")}</p>
<p class="small">In each cell the large number is the group's net position (contracts long minus short; a minus sign means net short). The small "wk" number underneath is how much that net position changed from the prior week. Contract counts are not comparable across rows: a 2-year contract carries far less rate risk than a Bond contract.</p>
{_moves(view.get("positioning_moves") or {})}
{_table(headers, rows, caption="Net contracts, with weekly change underneath. Asset mgrs = asset managers; Lev. funds = leveraged funds. Spreading positions are kept separate and not included in net.")}
{_cftc_details(cftc)}
<p class="note">These are positioning observations, not predictions. Dealers are often net short futures while holding cash Treasuries, and asset managers' long futures commonly reflect duration exposure. A group being long or short does not by itself say where yields will go.</p>
<p class="source">Source: CFTC Traders in Financial Futures, futures only. Positions as of Tuesday, usually released Friday.</p>
</section>'''


def _cftc_details(cftc: dict[str, Any] | None) -> str:
    if not cftc or not cftc.get("details"):
        return ""
    rows = [[d["contract"], d["group"], _contracts(d["long"], signed=False), _contracts(d["short"], signed=False),
             _contracts(d["spreading"], signed=False), _contracts(d["net"]), _contracts(d["change"])]
            for d in cftc["details"]]
    table = _table([("Contract", "l"), ("Group", "l"), ("Long", "r"), ("Short", "r"), ("Spreading", "r"),
                    ("Net", "r"), ("Week", "r")], rows)
    return f'<details class="more"><summary>Long, short, and spreading for every trader group</summary>{table}</details>'


def _supply(view: dict[str, Any]) -> str:
    rows = [[_day(a["date"], year=False), a["security"] or _unknown(),
             _usd_bn(None if a["offering_usd"] is None else a["offering_usd"] / 1e9),
             _num(a["bid_to_cover"]), _pct(a["yield"])] for a in view["auctions"]]
    return f'''<section id="supply" aria-labelledby="h-supply">
<h2 id="h-supply">Treasury supply</h2>
<p class="lead-sm">Most recent coupon auctions (2- to 30-year notes and bonds). {_tag("Fact")}</p>
{_table([("Date", "l"), ("Security", "l"), ("Size", "r"), ("Cover", "r"), ("Yield", "r")], rows,
        caption="Size is the amount offered. Cover is the bid-to-cover ratio. Yield is the auction high yield.")}
<p class="source">Source: U.S. Treasury Fiscal Data auctions dataset. Bills, TIPS, and floating-rate notes are not included.</p>
</section>'''


def _missing(view: dict[str, Any]) -> str:
    evidence = view["evidence"]
    feed_items = [m["item"] for m in evidence["not_available"] if m["reason"].endswith("FEED CONFIGURED")]
    data_items = [m for m in evidence["not_available"] if not m["reason"].endswith("FEED CONFIGURED")]
    panels = []
    if data_items:
        panels.append('<div class="void"><h3>Treasury-market data not stored</h3><ul>' + "".join(
            f'<li>{escape(m["item"])}: <strong>{escape(m["reason"])}</strong></li>' for m in data_items)
            + '</ul><p>Run ingestion to populate these. Missing data is not negative evidence.</p></div>')
    if "Corporate issuance event feed" in feed_items:
        panels.append(f'''<div class="void"><h3>Corporate issuance</h3>
<p><strong>Production corporate issuance feed unavailable.</strong> No event-level source is configured, so deal size, pricing date, settlement date, and duration are unknown for every deal. Synthetic test fixtures are not loaded as production events.</p>
<p class="small">Status: {escape(view["corporate_status"])}.</p>
</div>''')
    market_items = [i for i in feed_items if i not in ("Corporate issuance event feed",
                    "Corporate deal size, maturity/duration, pricing date, and settlement date")]
    if market_items:
        panels.append('<div class="void"><h3>Market confirmation</h3><ul>' + "".join(
            f'<li>{escape(i)}: <strong>Unavailable</strong></li>' for i in market_items)
            + '</ul><p>These FRED series are not stored yet, so cross-market moves cannot be checked.</p>'
            + f'<p class="small">Status: {escape(view["market_status"])}.</p></div>')
    panels.append('<div class="void"><h3>Not needed: Treasury futures prices</h3><p>Futures are priced off the same '
                  'Treasuries whose official daily yields are already stored, so they would add little at a daily '
                  'frequency. Their intraday prices and volume would add timing detail, but they are licensed exchange '
                  'data. Futures positioning is covered by the CFTC panel.</p></div>')
    have = [r["item"] for r in view["rate_lock_status"] if r["status"] == "AVAILABLE"]
    partial = [r["item"] for r in view["rate_lock_status"] if r["status"] == "PARTIALLY AVAILABLE"]
    lacking = [r["item"] for r in view["rate_lock_status"] if r["status"] == "UNAVAILABLE"]
    if evidence["treasury_side_complete"] and not lacking:
        coverage = ("Every input the rate-lock checks use is now stored. What stays invisible in public data is the "
                    "hedging itself: the Treasury sales and swaps that issuers and their banks do are not reported.")
    elif evidence["treasury_side_complete"]:
        coverage = "The database currently holds the Treasury-market side of the hypothesis (yields, curve, dealer and CFTC positioning, auctions) but not every corporate or cross-market input listed above."
    else:
        coverage = "The database is also missing Treasury-market evidence listed above, so even the Treasury side of the hypothesis is incomplete."
    test_note = ("Each deal can now be checked one at a time. Testing whether large deals systematically coincide with "
                 "pre-pricing Treasury pressure and a later reversal needs many more dated deals than the few weeks "
                 "of SEC filings stored so far." if not lacking else
                 "Therefore the system cannot currently test whether large corporate deals systematically coincide "
                 "with pre-settlement Treasury pressure or post-settlement reversal.")
    checklist = f'''<div class="checklist">
<div><h3>Have</h3><ul>{"".join(f"<li>{escape(i)}</li>" for i in have) or "<li>None</li>"}{"".join(f"<li>{escape(i)} (partial)</li>" for i in partial)}</ul></div>
<div><h3>Missing</h3><ul>{"".join(f"<li>{escape(i)}</li>" for i in lacking) or "<li>None</li>"}</ul></div></div>'''
    return f'''<section id="missing" aria-labelledby="h-missing">
<h2 id="h-missing">What evidence is missing?</h2>
<p class="lead-sm">Gaps are shown on purpose. An empty panel here means the system cannot see that part of the market, not that nothing happened there.</p>
{"".join(panels)}
<h3 class="sub">Rate-lock hypothesis: what can be tested today</h3>
{checklist}
<p class="note">{escape(coverage)} {escape(test_note)} {_tag("Hypothesis")}</p>
</section>'''


def _event_detail(event: dict[str, Any]) -> str:
    headers = [("T", "l"), ("Date", "l")] + [(label.replace("-year", "y"), "r") for _, label, _ in MATURITIES]
    rows = []
    def name(offset: int) -> str:
        return "T0" if offset == 0 else f"T{'+' if offset > 0 else MINUS}{abs(offset)}"
    observed = [row for row in event["window"] if row["date"]]
    pending = [row["offset"] for row in event["window"] if not row["date"] and row["offset"] > 0]
    for row in observed:
        rows.append([name(row["offset"]), _day(row["date"], year=False)] +
                    [_bp(row["changes"].get(series), unit=False) for series, _, _ in MATURITIES])
    summary = event.get("summary_10y") or {}
    context = []
    if event["dealer_date"]:
        context.append(f"dealer positions as of {_day(event['dealer_date'])}")
    if event["cftc_date"]:
        context.append(f"CFTC report of {_day(event['cftc_date'])}")
    auctions = ", ".join(f"{a['security']} on {_day(a['date'], year=False)}" for a in event["auctions"])
    caption = "Change versus T−1, basis points. T counts trading sessions."
    if pending:
        span = name(min(pending)) + ("" if len(pending) == 1 else f" to {name(max(pending))}")
        caption += f" {span}: not yet observed."
    return f'''<div class="event-body">
{_table(headers, rows, caption=caption)}
<p class="note">10-year: {_bp(summary.get("pre_event_move_bps"))} from T−5 to T−1, {_bp(summary.get("event_day_move_bps"))} on T0, {_bp(summary.get("post_event_move_bps"))} from T0 to T+5. {_tag("Calculation")}</p>
{_event_channel(event.get("channel"))}
<p class="note">Nearby context: {escape("; ".join(context) or "no dealer or CFTC report on or before this date")}.{(" Auctions within 3 days: " + escape(auctions) + ".") if auctions else ""}</p>{_market_day(event.get("market_day"))}
</div>'''


def _event_channel(window: dict[str, Any] | None) -> str:
    if not window:
        return ""
    curve = window.get("curve")
    tp = window.get("term_premium_bps")
    parts = [f"real yield {_text(_bp(window.get('real_bps')))}", f"breakeven {_text(_bp(window.get('breakeven_bps')))}",
             "term premium " + (_text(_bp(tp)) if tp is not None else "not published for this date")]
    shape = "" if not curve else f" Curve: {escape(curve['name'])}."
    return f'<p class="note">On the day: {", ".join(parts)}.{shape} {_tag("Calculation")}</p>'


def _events(view: dict[str, Any]) -> str:
    items = []
    for event in view["events"]:
        gap = (f'<span class="gap">after closure {", ".join(str(_day(d, year=False)) for d in event["skipped"])}</span>'
               if event["skipped"] else "")
        items.append(f'''<details class="event"><summary>
<span class="ev-date">{_day(event["date"])}</span><span class="ev-move">10y <strong>{_bp(event["d10"])}</strong></span><span class="ev-move">2y {_bp(event["d2"])}</span>{gap}</summary>
{_event_detail(event)}</details>''')
    shown = len(view["events"])
    threshold = view.get("threshold_bps")
    rule = f"at least {threshold:g} bp" if isinstance(threshold, int | float) else "above the threshold"
    body = "".join(items) or '<p><span class="unknown">No large 10-year moves in the stored range.</span></p>'
    return f'''<section id="moves" aria-labelledby="h-moves">
<h2 id="h-moves">Large 10-year moves</h2>
<p class="lead-sm">Sessions where the 10-year yield moved {rule} from the previous session. Showing the most recent {shown} of {view["event_count"]} detected events. Open one to see the curve around it. {_tag("Observation")}</p>
{body}
<p class="note">Timing alone does not establish cause. Weekly dealer and CFTC data are matched to the latest report on or before each date and are never filled forward.</p>
</section>'''


def _sources(view: dict[str, Any]) -> str:
    rows = []
    for item in view["provenance"]:
        age = item["observation_age_days"]
        age_text = _unknown() if age is None else str(age)
        label = (f'<a href="{escape(item["reference"])}">{escape(item["name"])}</a>'
                 if item["reference"] else escape(item["name"]))
        published = f'; published {escape(_instant(item["published"]))}' if item["published"] else ""
        name = _Html(f'<span class="cell-main src">{label}</span>'
                     f'<span class="cell-sub">Retrieved {escape(_instant(item["retrieved"]))}{published}</span>')
        rows.append([name, _day(item["observation_date"]), age_text])
    return f'''<section id="sources" aria-labelledby="h-sources">
<h2 id="h-sources">Sources and freshness</h2>
{_table([("Source", "l"), ("Latest data", "l"), ("Days old", "r")], rows,
        caption="Days old counts from the latest observation to when this page was generated.")}
<p class="note">Observation date is when the market event happened; retrieval is when this system fetched it. The sources do not supply exact publication times, so none are claimed.</p>
</section>'''


def _reading(view: dict[str, Any]) -> str:
    limits = "".join(f"<li>{escape(x)}</li>" for x in view["evidence"]["causality_limitation"])
    return f'''<section id="reading" aria-labelledby="h-reading">
<h2 id="h-reading">How to read this page</h2>
<dl class="taxonomy">
<dt>Fact</dt><dd>A value reported by an identified source.</dd>
<dt>Calculation</dt><dd>Reproducible arithmetic on stated inputs.</dd>
<dt>Observation</dt><dd>A measured pattern, with no claim about its cause.</dd>
<dt>Mechanism</dt><dd>A documented way a market process could work.</dd>
<dt>Inference</dt><dd>An interpretation the evidence supports but does not establish.</dd>
<dt>Hypothesis</dt><dd>A proposed explanation that still has to be tested.</dd>
</dl>
<ul class="limits">{limits}<li>{escape(view["interpretation_limit"] or "")}</li><li>This is a research and measurement tool. It produces no trading signals.</li></ul>
</section>'''


def _live_tools(user_event: dict[str, Any] | None, allow_event_input: bool) -> str:
    if not allow_event_input:
        return ""
    form = '''<form method="get" class="event-form"><label>Event date <input name="event_date" type="date" required></label>
<label>Label <input name="event_type" placeholder="e.g. Deal pricing"></label><button type="submit">Show event window</button></form>'''
    result = ""
    if user_event:
        rows = [[row.get("offset"), _day(row.get("date"), year=False), _bp(row.get("yield_2y_change_bps"), unit=False),
                 _bp(row.get("yield_10y_change_bps"), unit=False)] for row in user_event.get("window") or []]
        result = (f'<p class="warning">USER-SUPPLIED — UNVERIFIED. {escape(str(user_event.get("event_type") or ""))} on '
                  f'{_day(user_event.get("event_date"))}. Not a production observation. No production event records are asserted.</p>'
                  + _table([("T", "l"), ("Date", "l"), ("2y", "r"), ("10y", "r")], rows))
    return f'''<section id="event-input" aria-labelledby="h-event-input">
<h2 id="h-event-input">Look at a date</h2>
<p class="lead-sm">Enter any date to see the yield curve around it. Available in live mode only.</p>
{form}{result}</section>'''


# ---------------------------------------------------------------- payload & page

def _chart_payload(view: dict[str, Any]) -> dict[str, Any]:
    dealer = view["dealer"]
    return {
        "curve": {"maturities": [{"label": label.replace("-year", "y"), "years": years} for _, label, years in MATURITIES],
                  "snapshots": [{"key": s["key"], "name": s["name"], "date": s["date"],
                                 "points": [[p["years"], p["yield"]] for p in s["points"]]}
                                for s in view["curve"]["snapshots"]]},
        "tenyear": {"points": [[p["date"], p["yield"]] for p in view["ten_year_history"]["points"]],
                    "events": [[e["date"], e["change_bps"]] for e in view["ten_year_history"]["events"]]},
        "dealer": {"points": [] if dealer is None else
                   [[h["date"], h["value"]] for h in dealer["history"] if h["value"] is not None]},
    }


def render_report(report: dict[str, Any], *, user_event: dict[str, Any] | None = None,
                  allow_event_input: bool = False, event_limit: int = 20) -> str:
    """Render a bounded page; the full raw report is never embedded."""
    view = build_dashboard_view(report, event_limit=event_limit)
    payload = json.dumps(_chart_payload(view), separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    footer = ("Live view: rendered on each page load from the local SQLite database, opened read-only. "
              "No API keys or environment values are included in the page." if allow_event_input else
              "This is a self-contained snapshot: no network, Python, SQLite, API keys, environment variables, "
              "or raw database dump are included.")
    body = "".join((
        _header(view, allow_event_input), '<main>',
        _happening(view), _dealers(view), _futures(view), _supply(view), _missing(view),
        _events(view), _live_tools(user_event, allow_event_input), _sources(view), _reading(view),
        f'<footer>{escape(footer)}</footer></main>',
    ))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            '<title>Treasury Flow Radar</title>'
            f'<style>{CSS}</style></head><body>{body}'
            f'<script type="application/json" id="chart-data">{payload}</script>'
            f'<script>{JS}</script></body></html>')


CSS = r"""
:root{--bg:#f5f7f9;--ink:#14212c;--ink-2:#465663;--ink-3:#5f6e7a;--rule:#d5dce2;--rule-2:#e6ebef;
--hatch:rgba(20,33,44,.16);--s1:#184f95;--s2:#3987e5;--s3:#86b6ef;--line:#2a78d6;--tip:#14212c;--tip-ink:#f5f7f9;
--serif:Charter,"Bitstream Charter","Sitka Text",Cambria,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#11181e;--ink:#e4eaef;--ink-2:#a9b7c2;
--ink-3:#8b9aa6;--rule:#2b3741;--rule-2:#1e2830;--hatch:rgba(228,234,239,.14);--s1:#cde2fb;--s2:#5598e7;--s3:#1c5cab;
--line:#3987e5;--tip:#e4eaef;--tip-ink:#11181e;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#11181e;--ink:#e4eaef;--ink-2:#a9b7c2;--ink-3:#8b9aa6;--rule:#2b3741;--rule-2:#1e2830;
--hatch:rgba(228,234,239,.14);--s1:#cde2fb;--s2:#5598e7;--s3:#1c5cab;--line:#3987e5;--tip:#e4eaef;--tip-ink:#11181e;color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 var(--sans)}
td.r,th.r,.figure,.plot,.tip,.ev-move{font-variant-numeric:tabular-nums lining-nums}
a{color:inherit;text-underline-offset:3px}
a:focus-visible,summary:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--line);outline-offset:2px}
.site,main{max-width:46rem;margin:0 auto;padding:0 16px}
.site{padding-top:14px}
.bar{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 16px;
border-bottom:2px solid var(--ink);padding-bottom:8px}
.brand{font-weight:700;letter-spacing:.01em;text-decoration:none;font-size:15px}
nav a{font-size:14px;color:var(--ink-2);text-decoration:none;margin-left:14px}
nav a[aria-current="page"]{color:var(--ink);text-decoration:underline;text-decoration-thickness:2px}
nav a:first-child{margin-left:0}
.meta{margin:8px 0 0;font-size:14px;color:var(--ink-2)}
section{padding:28px 0 8px;border-bottom:1px solid var(--rule)}
section:last-of-type{border-bottom:0}
h1,h2{font-family:var(--serif);font-weight:600;letter-spacing:-.005em;margin:0 0 10px;line-height:1.2}
h1{font-size:30px}
h2{font-size:24px}
h3{font-size:16px;margin:0 0 6px}
h3.sub{margin-top:22px}
h2.sub-q{font-size:20px;margin-top:22px}
.level{margin:0 0 6px;line-height:1.35}
.level-figure{font-family:var(--serif);font-size:34px;font-weight:600;font-variant-numeric:tabular-nums lining-nums;white-space:nowrap}
.level-tag{display:inline-block;font-size:13px;font-weight:700;letter-spacing:.02em;border:1.5px solid var(--ink);border-radius:3px;padding:0 6px;margin:0 4px;vertical-align:6px}
.level-context{display:block;font-size:14px;color:var(--ink-2);margin-top:2px}
.verdict{font-family:var(--serif);font-size:19px;line-height:1.45;margin:4px 0 12px;max-width:38em}
.xtake{margin:6px 0 8px;padding:12px 14px;border:1px solid var(--rule);border-radius:6px;font-size:16px;line-height:1.45;max-width:36em}
.xtake-actions{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:0 0 6px}
.xtake-actions button{font:inherit;font-size:14px;padding:5px 12px;border:1px solid var(--rule);border-radius:4px;background:transparent;color:var(--ink);cursor:pointer}
.xtake-actions a{font-size:14px}
.flag-box{border:1px solid var(--rule);border-left:3px solid var(--line);border-radius:4px;padding:8px 12px;margin:8px 0 14px;max-width:42em}
.flag-box h3{margin:4px 0 6px;font-size:16px}
details.why{border-left:3px solid var(--line);padding:2px 0 2px 12px;margin:10px 0 14px;max-width:42em;font-size:15px;line-height:1.5}
details.why summary{cursor:pointer;font-weight:600;padding:4px 0}
details.why p{margin:6px 0}
.why-steps{margin:4px 0 8px;padding-left:18px;font-variant-numeric:tabular-nums}
.why-steps li{margin:3px 0}
details.cand{border:1px solid var(--rule);border-radius:4px;padding:0 12px;margin:0 0 10px;max-width:42em}
details.cand summary{cursor:pointer;display:flex;justify-content:space-between;gap:12px;padding:10px 0;font-weight:600}
.bars{margin:6px 0 4px;max-width:42em}
.bar-row{display:grid;grid-template-columns:minmax(0,11em) 1fr 3em;gap:10px;align-items:center;font-size:14px;padding:3px 0}
.bar-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:10px;background:var(--rule-2);border-radius:2px;overflow:hidden}
.bar-fill{display:block;height:100%;background:var(--line);border-radius:2px}
.bar-row.unexpl .bar-fill{background:repeating-linear-gradient(135deg,var(--ink-3) 0 2px,transparent 2px 6px)}
.bar-row.unexpl .bar-name{color:var(--ink-2);font-style:italic}
.bar-val{text-align:right;font-variant-numeric:tabular-nums}
.score{white-space:nowrap;color:var(--ink-2);font-weight:500;font-variant-numeric:tabular-nums}
ul.checks{list-style:none;padding:0;margin:4px 0 8px;font-size:14px}
ul.checks li{padding:4px 0 4px 24px;position:relative;color:var(--ink-2)}
ul.checks li strong{color:var(--ink);font-weight:600}
ul.checks .mark{position:absolute;left:0;top:3px;width:18px;text-align:center;font-weight:700;color:var(--ink)}
ul.checks li.fail .mark{color:var(--ink-3)}
.lead{font-family:var(--serif);font-size:21px;line-height:1.45;margin:0 0 18px;max-width:38em}
.lead-sm{margin:0 0 14px;max-width:40em}
.figure{font-family:var(--serif);font-size:26px;font-weight:600}
.note,.source{color:var(--ink-2);font-size:14px;max-width:42em}
.source{color:var(--ink-3);font-size:13px}
.tag{display:inline-block;font-family:var(--sans);font-size:12px;line-height:1.5;font-weight:500;color:var(--ink-2);
border:1px solid var(--rule);border-radius:3px;padding:0 5px;vertical-align:2px;white-space:nowrap}
.lead .tag{vertical-align:4px}
.unknown{color:var(--ink-3);font-style:italic}
.nw{white-space:nowrap}
.table-wrap{overflow-x:auto;margin:6px 0 12px;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:14px}
caption{caption-side:bottom;text-align:left;font-size:12px;color:var(--ink-3);padding-top:6px}
th,td{padding:7px 8px;border-bottom:1px solid var(--rule-2);white-space:nowrap}
th{font-weight:600;color:var(--ink-2);font-size:13px;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{padding-left:0}
th:last-child,td:last-child{padding-right:0}
.l{text-align:left}.r{text-align:right}
td.wrap{white-space:normal;min-width:12em}
.cell-main{display:block;font-weight:600}
.cell-main.src{font-weight:500;white-space:normal}
.cell-sub{display:block;font-size:12px;color:var(--ink-3)}
.chart{margin:4px 0 10px}
.chart figcaption{font-size:14px;color:var(--ink-2);margin-bottom:6px}
.plot{position:relative;width:100%;min-height:8px}
.plot svg{display:block;width:100%;overflow:visible}
.plot text{fill:var(--ink-3);font:12px var(--sans)}
.plot .axis{stroke:var(--rule);stroke-width:1}
.plot .grid{stroke:var(--rule-2);stroke-width:1}
.plot .series{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.plot .dot{stroke:var(--bg);stroke-width:2}
.plot .rug{stroke:var(--ink-2);stroke-width:1.5}
.plot .cross{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:2 3}
.plot .end-label{font-weight:600}
.tip{position:absolute;pointer-events:none;background:var(--tip);color:var(--tip-ink);font-size:12px;line-height:1.4;
padding:6px 8px;border-radius:4px;white-space:nowrap;display:none;z-index:2}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px;color:var(--ink-2);margin-bottom:4px}
.legend i{display:inline-block;width:16px;height:2px;vertical-align:4px;margin-right:6px;border-radius:1px}
.void{position:relative;border:1px solid var(--rule);border-radius:4px;padding:12px 14px 12px 30px;margin:0 0 12px;max-width:42em}
.void::before{content:"";position:absolute;left:0;top:0;bottom:0;width:16px;border-radius:3px 0 0 3px;
background:repeating-linear-gradient(135deg,var(--hatch) 0 2px,transparent 2px 7px);border-right:1px solid var(--rule)}
.void p,.void ul{margin:4px 0;font-size:15px}
.void ul{padding-left:18px}
.checklist{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px;font-size:14px;max-width:42em}
.checklist ul{margin:0;padding-left:18px}
.checklist div:last-child li{color:var(--ink-2)}
details.event{border-bottom:1px solid var(--rule-2)}
details.more{margin:0 0 12px;font-size:14px}
details.more summary{cursor:pointer;color:var(--ink-2);padding:4px 0}
.small{font-size:13px!important;color:var(--ink-3)}
details.event summary{list-style:none;cursor:pointer;display:flex;flex-wrap:wrap;gap:2px 16px;padding:10px 0;align-items:baseline}
details.event summary::-webkit-details-marker{display:none}
.ev-date{min-width:8.5em;font-weight:600}
.ev-date::before{content:"";display:inline-block;width:0;height:0;border:5px solid transparent;border-left:6px solid var(--ink-3);
margin-right:6px;transition:transform .15s;transform-origin:3px 5px}
details.event[open] .ev-date::before{transform:rotate(90deg)}
@media (prefers-reduced-motion:reduce){.ev-date::before{transition:none}}
.ev-move{color:var(--ink-2)}
.ev-move strong{color:var(--ink)}
.gap{font-size:13px;color:var(--ink-3)}
.event-body{padding:0 0 12px 14px}
.taxonomy{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0 0 12px;font-size:15px}
.taxonomy dt{font-weight:600}.taxonomy dd{margin:0;color:var(--ink-2)}
.limits{font-size:14px;color:var(--ink-2);padding-left:18px}
.warning{border-left:3px solid var(--ink);padding:6px 10px;font-size:14px}
.event-form{display:flex;flex-wrap:wrap;gap:10px;align-items:end;font-size:14px}
.event-form input{display:block;font:inherit;padding:6px;border:1px solid var(--rule);border-radius:3px;background:var(--bg);color:var(--ink)}
.event-form button{font:inherit;padding:7px 12px;border:1px solid var(--ink);border-radius:3px;background:var(--ink);color:var(--bg);cursor:pointer}
footer{font-size:12px;color:var(--ink-3);padding:20px 0 40px}
@media (max-width:520px){h1{font-size:26px}h2{font-size:21px}.lead{font-size:19px}.checklist{grid-template-columns:1fr}
.ev-date{min-width:7.5em}.event-body{padding-left:0}}
"""

JS = r"""
(function(){
var el=document.getElementById('chart-data');if(!el)return;var D=JSON.parse(el.textContent);
var NS='http://www.w3.org/2000/svg';
function css(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
function mk(t,a,p){var e=document.createElementNS(NS,t);for(var k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e;}
function nice(lo,hi,n){var span=hi-lo||Math.abs(hi)||1,raw=span/n,p=Math.pow(10,Math.floor(Math.log10(raw))),s=[1,2,2.5,5,10].map(function(m){return m*p;}).find(function(m){return m>=raw;});
var a=Math.floor(lo/s)*s,b=Math.ceil(hi/s)*s,t=[];for(var v=a;v<=b+s/2;v+=s)t.push(+v.toFixed(10));return{lo:a,hi:b,ticks:t,step:s};}
function fmtDate(s,y){var d=new Date(s+'T00:00:00Z');var m=d.toLocaleString('en-US',{month:'short',timeZone:'UTC'});return y?m+' '+d.getUTCDate()+', '+d.getUTCFullYear():m+' '+d.getUTCDate();}
function bp(v){var r=Math.round(v);return(r>0?'+':r<0?'−':'')+Math.abs(r)+' bp';}
// Generic line chart: series [{name,color,points:[[x,y,label]]}], x numeric.
function draw(box,o){
  box.innerHTML='';var W=box.clientWidth||320,H=o.height||220,m={t:10,r:o.right||12,b:26,l:46};
  var xs=[],ys=[];o.series.forEach(function(s){s.points.forEach(function(p){xs.push(p[0]);ys.push(p[1]);});});
  if(!xs.length){box.innerHTML='<p class="unknown" style="margin:0">No observations to chart yet.</p>';return;}
  var x0=o.xDomain?o.xDomain[0]:Math.min.apply(null,xs),x1=o.xDomain?o.xDomain[1]:Math.max.apply(null,xs);
  var Y=nice(Math.min.apply(null,ys),Math.max.apply(null,ys),4);
  var X=function(v){return m.l+(x1===x0?0.5:(v-x0)/(x1-x0))*(W-m.l-m.r);},Yp=function(v){return m.t+(1-(v-Y.lo)/(Y.hi-Y.lo||1))*(H-m.t-m.b);};
  var svg=mk('svg',{viewBox:'0 0 '+W+' '+H,height:H,role:'img','aria-label':o.label},box);
  Y.ticks.forEach(function(v){mk('line',{x1:m.l,x2:W-m.r,y1:Yp(v),y2:Yp(v),'class':'grid'},svg);var t=mk('text',{x:m.l-8,y:Yp(v)+4,'text-anchor':'end'},svg);t.textContent=o.yFmt(v,Y.step);});
  mk('line',{x1:m.l,x2:W-m.r,y1:H-m.b,y2:H-m.b,'class':'axis'},svg);
  (o.xTicks||[]).forEach(function(tk){var t=mk('text',{x:X(tk[0]),y:H-8,'text-anchor':'middle'},svg);t.textContent=tk[1];});
  o.series.forEach(function(s){var d=s.points.map(function(p,i){return(i?'L':'M')+X(p[0]).toFixed(1)+' '+Yp(p[1]).toFixed(1);}).join(' ');
    mk('path',{d:d,'class':'series',stroke:s.color},svg);
    if(o.dots)s.points.forEach(function(p){mk('circle',{cx:X(p[0]),cy:Yp(p[1]),r:4,fill:s.color,'class':'dot'},svg);});
    if(o.endLabel){var p=s.points[s.points.length-1];var t=mk('text',{x:X(p[0])+8,y:Yp(p[1])+4,'class':'end-label'},svg);t.textContent=o.endLabel(p);}});
  (o.rug||[]).forEach(function(r){mk('line',{x1:X(r),x2:X(r),y1:H-m.b-7,y2:H-m.b,'class':'rug'},svg);});
  // Hover / touch: crosshair on the nearest x with a tooltip listing each series.
  var allX=[];o.series.forEach(function(s){s.points.forEach(function(p){if(allX.indexOf(p[0])<0)allX.push(p[0]);});});allX.sort(function(a,b){return a-b;});
  var cross=mk('line',{y1:m.t,y2:H-m.b,'class':'cross',visibility:'hidden'},svg),tip=document.createElement('div');tip.className='tip';box.appendChild(tip);
  function show(ev){var r=svg.getBoundingClientRect(),cx=(ev.touches?ev.touches[0].clientX:ev.clientX)-r.left;var best=allX[0];
    allX.forEach(function(v){if(Math.abs(X(v)-cx)<Math.abs(X(best)-cx))best=v;});
    cross.setAttribute('x1',X(best));cross.setAttribute('x2',X(best));cross.setAttribute('visibility','visible');
    var lines=o.series.map(function(s){var p=s.points.find(function(q){return q[0]===best;});return p?o.tipLine(s,p):null;}).filter(Boolean);
    if(o.tipReverse)lines.reverse();
    tip.innerHTML=o.tipHead(best)+'<br>'+lines.join('<br>');tip.style.display='block';
    // Open the tooltip on the side of the crosshair that has room, inside the plot.
    var right=X(best)>W/2;tip.style.left=right?'auto':(X(best)+10)+'px';tip.style.right=right?(W-X(best)+10)+'px':'auto';
    tip.style.top=(m.t+2)+'px';}
  function hide(){cross.setAttribute('visibility','hidden');tip.style.display='none';}
  svg.addEventListener('mousemove',show);svg.addEventListener('touchstart',show,{passive:true});svg.addEventListener('touchmove',show,{passive:true});
  svg.addEventListener('mouseleave',hide);svg.addEventListener('touchend',function(){setTimeout(hide,1500);});
}
var day=function(s){return Date.parse(s+'T00:00:00Z')/864e5;},undo=function(n){return new Date(n*864e5).toISOString().slice(0,10);};
function monthTicks(pts,W){var out=[],seen={},every=W<420?2:1,i=0;pts.forEach(function(p){var k=p[0].slice(0,7);if(!seen[k]){seen[k]=1;if(i++%every===0)out.push([day(p[0]),fmtDate(p[0]).split(' ')[0]]);}});return out.slice(1);}
function render(){
  var c=document.querySelector('[data-chart="curve"] .plot');
  if(c){var cols=[css('--s1'),css('--s2'),css('--s3')];var mats=D.curve.maturities;var pos={};mats.forEach(function(mm,i){pos[mm.years]=i;});
    var series=D.curve.snapshots.map(function(s,i){return{name:s.name+(s.date?' ('+fmtDate(s.date)+')':''),color:cols[i],points:s.points.map(function(p){return[pos[p[0]],p[1],mats[pos[p[0]]].label];})};}).reverse();
    var lg=document.querySelector('[data-legend="curve"]');lg.innerHTML=series.slice().reverse().map(function(s){return'<span><i style="background:'+s.color+'"></i>'+s.name+'</span>';}).join('');
    draw(c,{label:'Yield curve',series:series,dots:true,tipReverse:true,height:230,right:16,xDomain:[-0.25,mats.length-0.75],xTicks:mats.map(function(mm,i){return[i,mm.label];}),
      yFmt:function(v,s){return v.toFixed(s<0.1?2:s<1?1:0)+'%';},tipHead:function(x){return'<b>'+mats[x].label+'</b>';},
      tipLine:function(s,p){return s.name.split(' (')[0]+': '+p[1].toFixed(2)+'%';}});}
  var t=document.querySelector('[data-chart="tenyear"] .plot');
  if(t){var pts=D.tenyear.points;var byDate={};pts.forEach(function(p){byDate[p[0]]=p[1];});var ev={};D.tenyear.events.forEach(function(e){ev[e[0]]=e[1];});
    draw(t,{label:'10-year yield',series:[{name:'10-year',color:css('--line'),points:pts.map(function(p){return[day(p[0]),p[1]];})}],height:210,
      rug:D.tenyear.events.filter(function(e){return byDate[e[0]]!=null;}).map(function(e){return day(e[0]);}),
      xTicks:monthTicks(pts,t.clientWidth),yFmt:function(v,s){return v.toFixed(s<0.1?2:1)+'%';},
      tipHead:function(x){return'<b>'+fmtDate(undo(x),true)+'</b>';},
      tipLine:function(s,p){var d=undo(p[0]);return'10-year '+p[1].toFixed(2)+'%'+(ev[d]!=null?' &middot; large move '+bp(ev[d]):'');}});}
  var d=document.querySelector('[data-chart="dealer"] .plot');
  if(d){var dp=D.dealer.points;draw(d,{label:'Primary dealer net position',series:[{name:'Net position',color:css('--line'),points:dp.map(function(p){return[day(p[0]),p[1]];})}],height:170,
      xTicks:monthTicks(dp,d.clientWidth).filter(function(_,i){return i%2===0;}),yFmt:function(v){return'$'+v.toFixed(0)+'B';},
      tipHead:function(x){return'<b>Week of '+fmtDate(undo(x),true)+'</b>';},tipLine:function(s,p){return'$'+p[1].toFixed(1)+'B';}});}
}
render();var w=0;window.addEventListener('resize',function(){clearTimeout(w);w=setTimeout(render,150);});
if(window.matchMedia)window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',render);
})();
"""
