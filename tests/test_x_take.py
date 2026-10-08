from dashboard.presentation import PAGE_URL, x_take

LEVEL = {"percent": 5.27, "date": "2026-10-06", "percentile": 99.4, "sessions": 499, "highest_in_history": False,
         "history_start": "2024-10-07"}
LATEST = {"windows": {"5": {"nominal_bps": 1.0, "real_bps": 0.0, "breakeven_bps": 1.0}}}


def _attr(best_bps: float, runner_bps: float) -> dict:
    cands = [{"type": "corporate_deal", "name": "Paramount Skydance financing for Warner Bros. Discovery",
              "attributed_bps": best_bps, "share": best_bps / 19},
             {"type": "macro_release", "name": "Jobs report", "attributed_bps": runner_bps, "share": runner_bps / 19}]
    return {"best": cands[0], "candidates": cands, "total_abs_bps": 19.0, "unexplained_share": 0.70}


def test_x_take_fits_the_limit_uses_page_numbers_and_says_fit_not_proof():
    take = x_take(LEVEL, LATEST, _attr(2.4, 2.2))
    assert take["length"] <= 280
    text = take["text"]
    assert text.startswith("10Y Treasury 5.27% (Oct 6), above 99% of closes in 2 yrs.")
    assert "+1bp net on 19bp of back-and-forth" in text
    assert "Paramount Skydance's bond deal & jobs report, ~13% each, too close to call" in text
    assert "70% unexplained" in text and "Fit, not proof." in text and PAGE_URL in text
    for word in ("buy", "sell", "long", "short", "caused"):
        assert word not in text.lower().split()


def test_clear_leader_is_named_alone():
    assert "Top fit: Paramount Skydance's bond deal, ~21% of the movement." in x_take(LEVEL, LATEST, _attr(4.0, 1.0))["text"]
