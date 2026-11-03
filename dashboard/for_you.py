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
              "not all present. Context for why markets may feel jumpier, not a reason to act on its own."),
    "strain": (f"For {YOUR_SECTORS}: all three conditions behind past forced selling are present. In past "
               "episodes selling spread to stocks quickly, software, AI and chips included; it does not "
               "say it will happen this time."),
    "partial": "Some inputs are missing, so this read can't say what it means for your holdings yet.",
}

DISCLAIMER = "Context about how the plumbing can reach your sectors, not a buy or sell signal."


def for_you(table: dict[str, Entry], verdict: str) -> Entry | None:
    """The entry for a gauge verdict, or None when the verdict has none (e.g. '—')."""
    return table.get(verdict)
