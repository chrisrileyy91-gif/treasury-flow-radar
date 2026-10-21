"""The overview tab: one screen that answers "is Treasury plumbing under strain?"

Every number here is read from the dashboard view model; nothing is computed that the
detail tabs do not already show. The plumbing instrument draws each basis-trade input
against its trip line (the display rule in analytics/basis_crowding.py), so distance to
the line is visible rather than only stated. Sparklines are inline SVG built here, so the
overview renders without the chart script.
"""
from __future__ import annotations

from datetime import date
from html import escape
from typing import Any

from treasury_flow_radar.analytics.plumbing_synthesis import funding_thinning

MINUS = "−"
FUNDING_SCALE_BPS = 10.0       # the funding track spans ±10 bp of SOFR minus IORB around the zero line
NEAR_PERCENTILE_POINTS = 5.0   # a percentile within 5 points of its line is "near"
NEAR_FUNDING_BPS = 1.0


def _signed(value: float | None, digits: int = 0, unit: str = "") -> str:
    if value is None:
        return "—"
    rounded = round(value, digits)
    sign = "+" if rounded > 0 else MINUS if rounded < 0 else ""
    return f"{sign}{abs(rounded):.{digits}f}{unit}"


def _tone(value: float | None) -> str:
    if value is None or round(value) == 0:
        return "neu"
    return "pos" if value > 0 else "neg"


def _date(value: Any, *, weekday: bool = False) -> str:
    if not value:
        return "—"
    day = date.fromisoformat(str(value)[:10])
    text = f"{day:%b} {day.day}"
    return f"{day:%a} {text}" if weekday else text


def _ordinal(value: float) -> str:
    n = round(value)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def sparkline(values: list[float], *, width: int = 300, height: int = 52, baseline: float | None = None,
              label: str = "") -> str:
    """Inline SVG line; an optional dashed baseline (e.g. zero or a threshold)."""
    points = [v for v in values if v is not None]
    if len(points) < 2:
        return ""
    lo, hi = min(points + ([baseline] if baseline is not None else [])), max(points + ([baseline] if baseline is not None else []))
    span = (hi - lo) or 1.0
    pad = 4

    def xy(i: int, v: float) -> tuple[float, float]:
        return (pad + i * (width - 2 * pad) / (len(points) - 1),
                pad + (hi - v) * (height - 2 * pad) / span)
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in
                    enumerate(xy(i, v) for i, v in enumerate(points)))
    end_x, end_y = xy(len(points) - 1, points[-1])
    base = ""
    if baseline is not None:
        by = xy(0, baseline)[1]
        base = f'<line class="spark-base" x1="{pad}" x2="{width - pad}" y1="{by:.1f}" y2="{by:.1f}"/>'
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
            f'aria-label="{escape(label)}">{base}<path class="spark-line" d="{path}"/>'
            f'<circle class="spark-end" cx="{end_x:.1f}" cy="{end_y:.1f}" r="3"/></svg>')


def _gauge(*, name: str, reading: str, verdict: str, position: float | None, line: float, state: str,
           left: str, right: str, line_label: str, points: list[str], so_what: str) -> str:
    """One trip-line track: reading as a dot, the rule's line as a brass mark, then what it means."""
    if position is None:
        track = '<div class="g-track g-empty"></div>'
    else:
        pos = min(max(position, 0.0), 1.0) * 100
        over = (f'<span class="g-over" style="left:{line * 100:.1f}%;width:{max(pos - line * 100, 0):.1f}%"></span>'
                if state == "over" else "")
        track = (f'<div class="g-track">{over}<span class="g-line" style="left:{line * 100:.1f}%"></span>'
                 f'<span class="g-dot g-{state}" style="left:{pos:.1f}%"></span></div>')
    facts = "".join(f"<li>{p}</li>" for p in points)
    return (f'<div class="gauge"><div class="g-head"><span class="g-name">{escape(name)}</span>'
            f'<span class="g-read g-{state}">{escape(reading)}<span class="g-verdict">{escape(verdict)}</span></span></div>{track}'
            f'<div class="g-scale"><span>{escape(left)}</span><span class="g-mid">{escape(line_label)}</span>'
            f'<span>{escape(right)}</span></div><ul class="g-facts">{facts}</ul>'
            + (f'<p class="g-so"><span class="g-so-l">So what</span>{so_what}</p>' if so_what else "") + '</div>')


def _m(value: float) -> str:
    """10-year-note equivalents in millions, e.g. 5.18M."""
    return f"{value / 1e6:.2f}M"


def crowding_context(agg: dict[str, Any], crowded: bool) -> tuple[str, list[str], str]:
    """Verdict, context lines and the practical reading for the leveraged-fund short."""
    pct = agg.get("short_percentile")
    size = abs(agg["net_10y_equivalents"])
    verdict = ("—" if pct is None else "crowded" if crowded else "large" if pct >= 70 else
               "typical" if pct >= 40 else "small")
    points = [(f'Short <b>{_m(size)}</b> 10-year-note equivalents, about <b>${size * 1e5 / 1e9:,.0f}B</b> of '
               f'10-year notes at face value.')]
    if pct is not None:
        line = agg.get("crowded_threshold_10y_equivalents")
        gap = "" if line is None else (
            f", {_m(size - line)} over the crowded line" if size >= line else
            f", {_m(line - size)} ({(line - size) / line * 100:.0f}%) under the crowded line")
        start = date.fromisoformat(agg["history_start"]).year
        points.append(f'By the long record: larger than {pct:.0f}% of the {agg["history_weeks"]} weeks since {start}{gap}.')
    history = [abs(h["value"]) for h in agg.get("history") or [] if h.get("value") is not None and h["value"] < 0]
    recent = history[-52:]
    if len(recent) >= 26:
        low, high = min(recent), max(recent)
        place = ("near the low end of" if size <= low + (high - low) / 3 else
                 "near the top of" if size >= high - (high - low) / 3 else "in the middle of")
        peak = agg.get("peak_short_10y_equivalents")
        peak_day = max(agg["history"], key=lambda h: -h["value"])["date"] if agg.get("history") else None
        below_peak = (f', {(1 - size / peak) * 100:.0f}% below the record {_m(peak)} ({_date(peak_day)}, '
                      f'{date.fromisoformat(peak_day).year})' if peak and peak_day and size < peak else
                      ", the largest on record" if peak and size >= peak else "")
        points.append(f'By the past year: {place} its {_m(low)} to {_m(high)} range{below_peak}.')
    dv01 = agg.get("net_dv01_usd_per_bp")
    exposure = ("" if dv01 is None else
                f"The futures leg carries about ${abs(dv01) / 1e6:,.0f}M per basis point of rate exposure, which "
                "the cash bonds held against it offset, so a yield move alone does not hurt the trade; "
                "funding costs and margin calls do. ")
    so_what = exposure + (
        "Crowded: if funding or volatility turns, an unusually large position would have to come out at once."
        if crowded else
        "Large enough that a forced exit would move the market, but size alone forces nothing."
        if pct is not None and pct >= 70 else
        "Not large enough on its own to make an unwind disorderly.")
    return verdict, points, so_what


def funding_context(funding: dict[str, Any], tight: bool) -> tuple[str, list[str], str]:
    median = funding["sofr_minus_iorb_median_bps"]
    pct = funding.get("sofr_minus_iorb_median_share_below")
    thinning = funding_thinning(funding)
    verdict = "tight" if tight else "loose" if median <= -5 else "thinning" if thinning else "normal"
    relation = f"{median:.0f} bp above" if median > 0 else f"{abs(median):.0f} bp below" if median < 0 else "level with"
    points = [(f'Overnight repo (SOFR) runs <b>{relation}</b> what the Fed pays banks on reserves (IORB), '
               f'{funding["median_sessions"]}-session median; latest {_signed(funding["sofr_minus_iorb_bps"], 0, " bp")} '
               f'on {_date(funding["date"])}.')]
    if pct is not None and funding.get("history_start"):
        start = date.fromisoformat(funding["history_start"])
        points.append(f'Tighter than in {pct:.0f}% of sessions since {start:%b} {start.year}.')
    tail = funding.get("sofr99_minus_iorb_bps")
    if tail is not None:
        points.append(f'The costliest 1% of repo trades pay <b>{_signed(tail, 0, " bp")}</b> versus IORB'
                      + (", already above it." if tail > 0 else "."))
    so_what = ("Financing the cash bonds now costs more than the Fed pays on reserves, so the trade's margin is "
               "being squeezed: the condition that forces deleveraging." if tight else
               "Cash is plentiful and the trade is cheap to finance." if median <= -5 else
               f"Still cheap to finance, {abs(median):.0f} bp from the line, but the cushion is thinner than usual"
               + (" and the expensive end of repo is already above it." if tail is not None and tail > 0 else ".")
               if thinning else
               f"Financing is cheap relative to the Fed's rate, {abs(median):.0f} bp from the line.")
    return verdict, points, so_what


def volatility_context(vol: dict[str, Any], elevated: bool) -> tuple[str, list[str], str]:
    pct = vol.get("realized_vol_percentile")
    daily = vol["realized_vol_bps_per_day"]
    line = vol.get("elevated_threshold_bps_per_day")
    verdict = "—" if pct is None else "elevated" if elevated else "ordinary" if pct >= 40 else "calm"
    points = [f'The 10-year moves about <b>{daily:.1f} bp on a typical day</b> (last {vol["sessions"]} sessions).']
    if pct is not None:
        years = round(vol.get("history_sessions", 0) / 252)
        points.append(f'Choppier than {pct:.0f}% of sessions over about {years} years'
                      + ("" if line is None else
                         f'; the line is {line:.1f} bp a day, {(line / daily - 1) * 100:.0f}% more movement.'
                         if daily < line else f'; above the {line:.1f} bp line.'))
    so_what = ("Exchanges raise futures margin when yields swing like this, so levered funds must post more "
               "cash: the usual trigger for cutting positions." if elevated else
               "Margin requirements rise with volatility; at this level they are not forcing anyone out. "
               "Measured from actual daily moves, not the MOVE index.")
    return verdict, points, so_what


def _percentile_state(pct: float | None, tripped: bool | None) -> str:
    if pct is None:
        return "unknown"
    if tripped:
        return "over"
    return "near" if pct >= 80 - NEAR_PERCENTILE_POINTS else "clear"


def _instrument(view: dict[str, Any]) -> str:
    basis = view.get("basis") or {}
    syn = view.get("synthesis") or {}
    state = basis.get("state") or {}
    agg = (basis.get("crowding") or {}).get("aggregate")
    funding = basis.get("funding")
    vol = basis.get("volatility")
    if not syn.get("headline") and agg is None and funding is None and vol is None:
        return ('<section class="ov-block"><p class="ov-empty">No positioning, funding or volatility '
                'observations are stored yet, so the plumbing read is not available.</p></section>')
    tripped = sum(bool(state.get(k)) for k in ("crowded", "funding_tight", "vol_elevated"))
    tone = str(syn.get("tone") or "unknown")
    word = {"quiet": "Quiet", "watch": "Watch", "strain": "Strain", "partial": "Partial"}.get(tone, "Unread")

    pct = None if agg is None else agg.get("short_percentile")
    c_verdict, c_points, c_so = (("—", ["Not enough stored weeks to rank the position."], "")
                                 if agg is None else crowding_context(agg, bool(state.get("crowded"))))
    crowd = _gauge(
        name="Crowding", reading="—" if pct is None else f"{_ordinal(pct)} pct", verdict=c_verdict,
        position=None if pct is None else pct / 100, line=0.8,
        state=_percentile_state(pct, state.get("crowded")), left="smaller short", right="record",
        line_label="crowded at 80th", points=c_points, so_what=c_so)
    median = None if funding is None else funding.get("sofr_minus_iorb_median_bps")
    f_state = ("unknown" if median is None else "over" if state.get("funding_tight") else
               "near" if median >= -NEAR_FUNDING_BPS else "clear")
    f_verdict, f_points, f_so = (("—", ["No SOFR or IORB observations stored."], "")
                                 if median is None else funding_context(funding, bool(state.get("funding_tight"))))
    fund = _gauge(
        name="Funding", reading="—" if median is None else _signed(median, 0, " bp"), verdict=f_verdict,
        position=None if median is None else (median + FUNDING_SCALE_BPS) / (2 * FUNDING_SCALE_BPS), line=0.5,
        state=f_state, left=f"{MINUS}{FUNDING_SCALE_BPS:.0f} bp", right=f"+{FUNDING_SCALE_BPS:.0f} bp",
        line_label="tight above 0", points=f_points, so_what=f_so)
    vpct = None if vol is None else vol.get("realized_vol_percentile")
    v_verdict, v_points, v_so = (("—", ["Not enough 10-year history to measure."], "")
                                 if vol is None else volatility_context(vol, bool(state.get("vol_elevated"))))
    vola = _gauge(
        name="Volatility", reading="—" if vpct is None else f"{_ordinal(vpct)} pct", verdict=v_verdict,
        position=None if vpct is None else vpct / 100, line=0.8,
        state=_percentile_state(vpct, state.get("vol_elevated")), left="calm", right="extreme",
        line_label="elevated at 80th", points=v_points, so_what=v_so)
    lines = syn.get("lines") or []
    conclusion = escape(lines[-1]["text"]) if lines else ""
    as_of = syn.get("as_of") or {}
    stamp = ", ".join(f"{label} {_date(as_of[key])}" for key, label in
                      (("positioning", "positioning"), ("funding", "funding")) if as_of.get(key))
    return f'''<section class="ov-block ov-hero" aria-labelledby="ov-state">
<p class="ov-kicker">Treasury plumbing</p>
<p class="ov-headline"><span id="ov-state" class="ov-state t-{escape(tone)}">{word}</span>
<span class="ov-count">{tripped} of 3 trip lines crossed</span></p>
<p class="ov-read">{escape(str(syn.get("headline") or ""))}</p>
<div class="card instrument">
<div class="cap-row"><span class="cap brass">Basis-trade setup</span><span class="cap-meta">{escape(stamp)}</span></div>
{crowd}{fund}{vola}
<p class="ov-conclusion">{conclusion}</p>
<a class="go" href="#plumbing" data-tab="plumbing">Full read and charts</a>
</div>
</section>'''


def _ten_year(view: dict[str, Any]) -> str:
    level = view.get("level")
    rows = {r["series"]: r for r in (view.get("curve") or {}).get("rows") or []}
    ten = rows.get("DGS10") or {}
    if not level or ten.get("yield") is None:
        return ""
    points = [p["yield"] for p in (view.get("ten_year_history") or {}).get("points") or []]
    cells = "".join(
        f'<div class="mat"><span class="mat-l">{escape(r["label"].replace("-year", "y"))}</span>'
        f'<span class="mat-y">{r["yield"]:.2f}</span><span class="mat-d {_tone(r.get("d5"))}">{_signed(r.get("d5"))}</span></div>'
        for r in rows.values() if r.get("yield") is not None)
    stale = [r["label"] for r in rows.values() if r.get("date") and r["date"] != ten.get("date")]
    stale_note = (f'<p class="small">{escape(", ".join(stale))} last published {_date(next(r["date"] for r in rows.values() if r["label"] == stale[0]))}.</p>'
                  if stale else "")
    return f'''<section class="ov-block card">
<div class="cap-row"><span class="cap cyan">10-year yield</span><span class="cap-meta">{_ordinal(level["percentile"])} pct of {level["sessions"]} sessions</span></div>
<p class="big-row"><span class="big">{ten["yield"]:.2f}%</span><span class="big-sub {_tone(ten.get("d5"))}">{_signed(ten.get("d5"))} bp in 5 sessions</span></p>
{sparkline(points, label=f"10-year yield, last {len(points)} sessions")}
<div class="mats" aria-label="Yields and 5-session change, basis points">{cells}</div>
<p class="small">{escape(str(view.get("curve_read") or ""))}</p>{stale_note}
<a class="go" href="#moves" data-tab="moves">Curve and large moves</a>
</section>'''


def _drivers(view: dict[str, Any]) -> str:
    cand = view.get("candidates") or {}
    bars = cand.get("bars") or []
    window = cand.get("window") or {}
    if not bars:
        return ""
    rows = "".join(
        f'<div class="drv{" unexpl" if b.get("unexplained") else ""}"><span class="drv-n">{escape(b["name"])}</span>'
        f'<span class="drv-t"><span style="width:{max(b["share"] * 100, 0.8):.1f}%"></span></span>'
        f'<span class="drv-v">{b["share"] * 100:.0f}%</span></div>'
        for b in bars[:4])
    meta = f'{_date(window.get("start"))} to {_date(window.get("end"))}' if window else ""
    return f'''<section class="ov-block card">
<div class="cap-row"><span class="cap cyan">What moved it</span><span class="cap-meta">{escape(meta)}</span></div>
<p class="ov-body">Share of the 10-year's {cand.get("total_bps") or 0:.0f} bp of daily moves that each candidate's pattern fits.</p>
<div class="drvs">{rows}</div>
<a class="go" href="#moves" data-tab="moves">Why, checks and calibration</a>
</section>'''


def _positioning(view: dict[str, Any]) -> str:
    dealer = view.get("dealer")
    agg = ((view.get("basis") or {}).get("crowding") or {}).get("aggregate")
    parts = []
    if dealer and dealer.get("position_bn") is not None:
        parts.append(f'''<div class="pos-col"><span class="cap dim">Primary dealers</span>
<p class="mid"><span class="mid-n">${dealer["position_bn"]:,.0f}B</span>
<span class="{_tone(dealer.get("change_bn"))}">{_signed(dealer.get("change_bn"), 1, "B").replace("+", "+$").replace("−", "−$")} wk</span></p>
{sparkline([h["value"] for h in dealer.get("history") or []], height=40, label="Dealer net position, weekly")}
<span class="small">net long, {_date(dealer["date"])}</span></div>''')
    if agg:
        hist = [h["value"] / 1e6 for h in agg.get("history") or []][-156:]
        parts.append(f'''<div class="pos-col"><span class="cap dim">Leveraged funds</span>
<p class="mid"><span class="mid-n">{agg["net_10y_equivalents"] / 1e6:+.2f}M</span>
<span class="{_tone(agg.get("weekly_change_10y_equivalents"))}">{_signed((agg.get("weekly_change_10y_equivalents") or 0) / 1e3, 0, "k")} wk</span></p>
{sparkline(hist, height=40, baseline=0.0, label="Leveraged-fund net futures, 10-year equivalents, last 3 years")}
<span class="small">futures, 10y equiv., {_date(agg["report_date"])}</span></div>''')
    if not parts:
        return ""
    return f'''<section class="ov-block card">
<div class="cap-row"><span class="cap cyan">Positioning</span><span class="cap-meta">weekly</span></div>
<div class="pos-grid">{"".join(parts)}</div>
<a class="go" href="#positioning" data-tab="positioning">Dealers and futures by group</a>
</section>'''


def _supply(view: dict[str, Any]) -> str:
    auctions = view.get("auctions") or []
    if not auctions:
        return ""
    items = []
    for a in auctions[:4]:
        size = "—" if a.get("offering_usd") is None else f'${a["offering_usd"] / 1e9:.0f}B'
        if a.get("upcoming"):
            result = '<span class="tagline">upcoming</span>'
        else:
            cover = "—" if a.get("bid_to_cover") is None else f'{a["bid_to_cover"]:.2f}×'
            yld = "—" if a.get("yield") is None else f'{a["yield"]:.3f}%'
            result = f'<span>{yld}</span><span class="dim">{cover}</span>'
        kind = ' <span class="dim">reopening</span>' if a.get("reopening") else ""
        items.append(f'<div class="auc"><span class="auc-d">{_date(a["date"])}</span>'
                     f'<span class="auc-s">{escape(a.get("security") or "—")}{kind}</span>'
                     f'<span class="auc-z">{size}</span><span class="auc-r">{result}</span></div>')
    return f'''<section class="ov-block card">
<div class="cap-row"><span class="cap cyan">Treasury supply</span><span class="cap-meta">coupon auctions</span></div>
<div class="aucs">{"".join(items)}</div>
<a class="go" href="#supply" data-tab="supply">All recent auctions</a>
</section>'''


def overview(view: dict[str, Any]) -> str:
    return "".join((_instrument(view), _ten_year(view), _drivers(view), _positioning(view), _supply(view)))
