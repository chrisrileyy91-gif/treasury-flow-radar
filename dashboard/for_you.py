"""Plain-English "What this means for you" lines for the overview gauges.

Written for a reader who holds energy, AI, chips/semis and software stocks. Each entry is a
plain-words sentence on what the reading is, plus, when it is flashing, one short line per
sector on how that condition can reach it. These are MECHANISM (the usual transmission path)
and INFERENCE (what the current reading implies about that path); none is a forecast or a
buy/sell signal, and none claims the reading has moved any stock. Edit the texts here, not in
the gauge code.
"""
from __future__ import annotations

YOUR_SECTORS = "energy, AI, chips/semis and software"

Entry = tuple[str, dict[str, str]]

FUNDING: dict[str, Entry] = {
    "tight": (("Borrowing cash against Treasuries now costs more than the Fed's own rate: spare cash in "
              "the system is getting scarce. On its own this doesn't move your stocks; it matters if it "
              "stays above 0 for weeks."),
              {"AI & software": "Most exposed if money keeps getting costlier: their value rests on profits years away.",
               "Chips/semis": "Big, crowded and easy to sell, so often sold first when funds need cash.",
               "Energy": "Least tied; moves more with oil and the dollar."}),
    "thinning": (("Cash is still cheap to borrow, but the cushion is thinner than usual. Not a pressure on "
                 "your stocks yet; it's the gauge to keep an eye on."), {}),
    "normal": ((f"Cash is cheap and easy to borrow. Not a pressure on {YOUR_SECTORS}."), {}),
    "loose": (("Cash is plentiful. Easy money is usually a tailwind for growth stocks like software and AI."), {}),
}

CROWDING: dict[str, Entry] = {
    "crowded": (("Hedge funds have an unusually big borrowed bet on Treasuries. If they were forced out at "
                "once, bond yields could spike, and fast-rising yields hit growth stocks."),
                {"AI & software": "Hit hardest by a yield spike: their value rests on profits years away.",
                 "Chips/semis": "Crowded and easy to sell, so often caught early in forced selling.",
                 "Energy": "Less tied, though in a scramble for cash almost everything gets sold."}),
    "large": (("Hedge funds hold a big borrowed bet on Treasuries, but nothing is forcing them out. It only "
              "reaches your stocks if funding or volatility turns too."), {}),
    "typical": ((f"The hedge-fund bet is a normal size. Not a concern for {YOUR_SECTORS}."), {}),
    "small": ((f"The hedge-fund bet is small. Not a concern for {YOUR_SECTORS}."), {}),
}

VOLATILITY: dict[str, Entry] = {
    "elevated": (("The 10-year yield is swinging a lot day to day. That forces leveraged funds to post more "
                 "cash and makes long-term profits harder to price."),
                 {"AI & software": "Valuations tend to shrink when rates jump around.",
                  "Chips/semis": "Usually swing harder than the market, both ways.",
                  "Energy": "Less sensitive; oil and the dollar matter more."}),
    "ordinary": ((f"Rate swings are normal. They aren't what's driving {YOUR_SECTORS} right now."), {}),
    "calm": (("Rates are calm, which lets software, AI and chip stocks trade on their own news."), {}),
}

OVERALL = {
    "quiet": f"For {YOUR_SECTORS}: the Treasury plumbing is not a pressure right now.",
    "watch": (f"For {YOUR_SECTORS}: one warning light is on, but the conditions that force selling are "
              "not all present, so it explains why markets may feel jumpier rather than calling for action."),
    "strain": (f"For {YOUR_SECTORS}: all three conditions behind past forced selling are present. In past "
               "episodes selling spread to stocks quickly, software, AI and chips included; it does not "
               "say it will happen this time."),
    "partial": "Some inputs are missing, so this read can't say what it means for your holdings yet.",
}

DISCLAIMER = "Context about how the plumbing can reach your sectors, not a buy or sell signal."


def for_you(table: dict[str, Entry], verdict: str) -> Entry | None:
    """The entry for a gauge verdict, or None when the verdict has none (e.g. '—')."""
    return table.get(verdict)


# ---------------------------------------------------------------- shared rendering

def you_html(entry: Entry | None) -> str:
    """The "What this means for you" box: one plain sentence, then a line per sector if any."""
    from html import escape
    if not entry:
        return ""
    plain, sectors = entry
    rows = "".join(f'<li><span class="g-you-s">{escape(k)}</span>{escape(v)}</li>' for k, v in sectors.items())
    return (f'<div class="g-you"><span class="g-you-l">What this means for you</span><p>{escape(plain)}</p>'
            + (f'<ul>{rows}</ul>' if rows else "") + '</div>')


# ---------------------------------------------------------------- per-reading lines
# Display rules (not calibrated): a level/position at or above the 80th percentile of its
# stored history is "high", at or below the 20th "low"; a 10-year move of 10 bp or more over
# 5 sessions is "a real move"; a 20 bp change in high-yield spreads over the window is "a
# real move" (a typical day is roughly 7 bp).
HIGH_PCT, LOW_PCT = 80.0, 20.0
MOVE_BPS = 10.0
HY_MOVE_BPS = 20.0


def _rank(value: float, history: list[float]) -> float | None:
    if len(history) < 20:
        return None
    return sum(h <= value for h in history) / len(history) * 100


def _ord(n: float) -> str:
    n = round(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _md(day: object) -> str:
    from datetime import date
    d = date.fromisoformat(str(day)[:10])
    return f"{d:%b} {d.day}"


def level_you(level: dict | None, d5: float | None) -> Entry | None:
    """The 10-year yield's level against its stored history, plus the week's direction."""
    if not level or level.get("percentile") is None:
        return None
    pct = level["percentile"]
    years = round((level.get("sessions") or 0) / 252)
    span = f"the past {years} years'" if years >= 2 else "the stored history's"
    move = ""
    if d5 is not None and d5 <= -MOVE_BPS:
        move = f" It fell {abs(d5):.0f} bp this week, which eases that a little."
    elif d5 is not None and d5 >= MOVE_BPS:
        move = f" It rose {d5:.0f} bp this week, adding to it."
    base = ("The 10-year yield is the rate the market uses to price long-term money. ")
    if pct >= HIGH_PCT:
        return (base + f"It's higher than {pct:.0f}% of {span} closes, a headwind for stocks priced on "
                "future profits." + move,
                {"AI & software": "Most exposed: high long-term rates shrink what profits years away are worth today.",
                 "Chips/semis": "Also priced on growth, so they feel it, though they swing more on demand news.",
                 "Energy": "Least sensitive to the level of rates; oil and the dollar matter more."})
    if pct <= LOW_PCT:
        return (base + f"It's lower than {100 - pct:.0f}% of {span} closes, a tailwind for stocks "
                "priced on future profits." + move,
                {"AI & software": "Biggest beneficiaries: low long-term rates raise what future profits are worth today.",
                 "Chips/semis": "Helped too, though they swing more on demand news.",
                 "Energy": "Least sensitive to the level of rates; oil and the dollar matter more."})
    return (base + f"It sits in the middle of its stored range ({_ord(pct)} percentile), so it isn't a big "
            f"factor for {YOUR_SECTORS} right now." + move, {})


_DRIVER_MEANING = {
    "macro_release": ("Economic data moves rates by changing what the market expects the Fed to do, and that "
                      "expected path is the main way rates reach software and AI valuations."),
    "fomc": ("Fed decisions reset the expected path of rates, the main way rates reach software and AI "
             "valuations."),
    "auction": ("Heavy Treasury supply can lift yields without any better growth news, the least friendly kind "
                "of rise for software and AI."),
    "corporate_deal": ("Big corporate bond deals can nudge yields as issuers hedge; the effect is small and "
                       "usually fades."),
}


def drivers_you(cand: dict | None) -> Entry | None:
    """Which scheduled events fit the window's moves, and how much is unexplained."""
    bars = (cand or {}).get("bars") or []
    if not bars:
        return None
    top = next((b for b in bars if not b.get("unexplained")), None)
    unexplained = sum(b["share"] for b in bars if b.get("unexplained"))
    types = {i.get("name"): i.get("type") for i in (cand or {}).get("items") or []}
    if top is None:
        plain = "Nothing on the calendar lines up with this week's moves."
    else:
        plain = (f"Scheduled events explain only part of this week's moves: the best fit is "
                 f"{top['name']} (about {top['share'] * 100:.0f}%), and {unexplained * 100:.0f}% isn't tied to "
                 "anything on the calendar.")
        meaning = _DRIVER_MEANING.get(types.get(top["name"]) or "", "")
        if meaning:
            plain += " " + meaning
    if unexplained >= 0.6:
        plain += " With most of the move unexplained, be wary of headlines that claim to know why rates moved."
    return (plain, {})


def dealer_you(dealer: dict | None) -> Entry | None:
    """Primary dealers' Treasury inventory against the past year."""
    if not dealer or dealer.get("position_bn") is None:
        return None
    pct = _rank(dealer["position_bn"], [h["value"] for h in dealer.get("history") or [] if h.get("value") is not None])
    who = "Dealers, the big banks that make markets in Treasuries,"
    if pct is not None and pct >= HIGH_PCT:
        return ((f"{who} are holding more bonds than in {pct:.0f}% of the past year's weeks. With their shelves full, "
                "new Treasury supply may need higher yields to find buyers, which would be a headwind for software "
                "and AI if it happens."), {})
    if pct is not None and pct <= LOW_PCT:
        return ((f"{who} are holding fewer bonds than in most of the past year's weeks, so they have room to absorb "
                "new supply. That helps keep yields steady."), {})
    return ((f"{who} hold a normal amount of bonds for the past year. Not a pressure on {YOUR_SECTORS}."), {})


def futures_you(crowding_verdict: str) -> Entry | None:
    """The trader-group futures table, tied back to the basis trade."""
    entry = CROWDING.get(crowding_verdict)
    lead = ("This table is where the basis trade lives: asset managers are mostly long futures and hedge funds "
            "short against cash bonds they hold. ")
    if not entry:
        return (lead + "Positions by themselves don't say where yields go.", {})
    return (lead + entry[0], entry[1])


def supply_you(auctions: list | None, today: str | None = None) -> Entry | None:
    """Treasury coupon auctions: what they are and how they can reach stocks."""
    if not auctions:
        return None
    upcoming = [a for a in auctions if a.get("upcoming") and (today is None or str(a["date"])[:10] >= today[:10])]
    nxt = ""
    if upcoming:
        a = min(upcoming, key=lambda x: x["date"])
        size = "" if a.get("offering_usd") is None else f" ${a['offering_usd'] / 1e9:.0f}B"
        nxt = f" Next up: {a['security']}{size} on {_md(a['date'])}."
    return (("The Treasury borrows by auctioning new notes and bonds. The cover ratio is how many dollars were bid "
            "for each dollar sold; a weak auction means buyers wanted a higher yield." + nxt +
            " Weak demand at long-term auctions can push yields up, the same headwind for software and AI as any "
            "rise in rates; energy is the least exposed."), {})


def market_you(ctx: dict | None) -> Entry | None:
    """Stocks and credit over the same sessions as the 10-year's moves."""
    sessions = (ctx or {}).get("sessions") or []
    if (ctx or {}).get("status") != "AVAILABLE" or not sessions:
        return None
    spx, hy, ten = 1.0, 0.0, 0.0
    for s in sessions:
        cells = s.get("cells") or {}
        if (cells.get("SP500") or {}).get("change") is not None:
            spx *= 1 + cells["SP500"]["change"] / 100
        if (cells.get("BAMLH0A0HYM2") or {}).get("change") is not None:
            hy += cells["BAMLH0A0HYM2"]["change"]
        ten += s.get("ten_year_bps") or 0.0
    spx = (spx - 1) * 100
    def signed(v: float, digits: int) -> str:
        return f"{v:+.{digits}f}".replace("-", "−")
    plain = (f"Over these sessions the S&P 500 moved {signed(spx, 1)}% and high-yield credit spreads "
             f"{signed(hy, 0)} bp, while the 10-year moved {signed(ten, 0)} bp.")
    last = sessions[-1]
    last_hy = (last.get("cells") or {}).get("BAMLH0A0HYM2") or {}
    if last_hy.get("unusual") and (last_hy.get("change") or 0) > 0 and hy < HY_MOVE_BPS:
        plain += (f" Across the week credit was roughly flat, but the latest session ({_md(last['date'])}) broke "
                  "that: credit spreads widened more than usual, a flight-to-safety pattern. If it continues, it "
                  "tends to weigh most on unprofitable software and smaller AI names.")
    elif hy >= HY_MOVE_BPS:
        plain += (" Wider credit spreads mean lenders are getting nervous about riskier companies; that tends to "
                  "weigh most on unprofitable software and smaller AI names.")
    elif hy <= -HY_MOVE_BPS:
        plain += " Tighter credit spreads mean lenders are relaxed, a supportive backdrop for growth stocks."
    else:
        plain += " Credit markets are steady, so stress isn't spreading from bonds into stocks."
    return (plain, {})


def channel_you(channel: dict | None) -> Entry | None:
    """Whether the 10-year's move came through real yields or inflation expectations."""
    five = (channel or {}).get("five") or {}
    nominal, real, be = five.get("nominal_bps"), five.get("real_bps"), five.get("breakeven_bps")
    if nominal is None or real is None or be is None:
        return None
    if abs(nominal) < 3:
        return (("The 10-year barely moved this week, so neither channel is doing much to your stocks."), {})
    if abs(real) >= abs(be):
        word = "drop" if real < 0 else "rise"
        effect = "a mild tailwind" if real < 0 else "a headwind"
        return ((f"This week's {word} came mostly from real yields (rates after inflation), the part growth-stock "
                f"valuations track most closely: {effect} for software and AI."), {})
    return (("This week's move came mostly from inflation expectations, which often travel with oil prices. That's "
            "the channel most linked to energy, and it shapes how much room the Fed has to cut rates."), {})


def events_you(threshold: float | None) -> Entry:
    """The list of large 10-year moves."""
    size = f"{threshold:g} bp or more" if isinstance(threshold, int | float) else "a lot"
    return ((f"Each of these is a day the 10-year moved {size}. Days like these are when stocks priced on future "
            "profits, software and AI especially, tend to reprice most, so they're worth comparing against your "
            "own stocks' moves on the same dates."), {})


def read_you(tone: str | None) -> Entry | None:
    """The plumbing tab's overall read."""
    text = OVERALL.get(str(tone or ""))
    return None if text is None else (text + " " + DISCLAIMER, {})


# ---------------------------------------------------------------- Treasury cash (TGA)
# Market-side language: TGA up = cash leaving banks (a liquidity drain); TGA down = Treasury
# spending it back out (a liquidity injection). The plan lines restate Treasury's own published
# cash plan (INFERENCE from Treasury's assumptions), never a forecast of markets.

CASH_WHAT = ("This is the government's checking account at the Fed. When it grows, cash leaves the banks "
             "(a liquidity drain); when Treasury spends it down, cash flows back in (an injection).")

CASH_DIRECTION: dict[str, Entry] = {
    "draining": (("Right now it is growing, so spare cash in the banking system is shrinking. A drain like "
                  "this rarely moves stocks on its own; it matters when it lines up with rising repo costs."),
                 {"AI & software": "Most sensitive to tighter money: their value rests on profits years away.",
                  "Chips/semis": "Big, crowded and easy to sell, so often sold first when cash gets scarce.",
                  "Energy": "Least tied; moves more with oil and the dollar."}),
    "injecting": (("Right now Treasury is spending it down, so cash is flowing back into the banks. More spare "
                   "cash is usually a mild tailwind for growth stocks like software and AI."), {}),
    "flat": ((f"Right now it is roughly steady, so it isn't adding or removing cash. Not a pressure on "
              f"{YOUR_SECTORS}."), {}),
}

CASH_OUTLOOK = {
    "drain_then_injection": ("Treasury's own plan has the balance still climbing to its peak, so the drain may "
                             "run a little longer before the spend-down that hands cash back."),
    "injection_ahead": ("Treasury's own plan has the balance falling from here: a spend-down that hands cash "
                        "back to the banks."),
    "drain_ahead": ("Treasury's own plan has the balance rising from here: more cash pulled out of the banks."),
    "neutral": "The balance is close to Treasury's own target, so its plan implies little change from here.",
}


def cash_you(cash: dict | None) -> Entry | None:
    """The TGA read: what the account is, which way cash is moving, and what Treasury's plan implies."""
    if not cash or cash.get("balance_bn") is None:
        return None
    direction = cash.get("direction")
    plain, sectors = CASH_DIRECTION.get(str(direction), ("", {}))
    outlook = CASH_OUTLOOK.get(str((cash.get("plan") or {}).get("outlook")))
    text = " ".join(t for t in (CASH_WHAT, plain, outlook) if t)
    return (text, sectors)
