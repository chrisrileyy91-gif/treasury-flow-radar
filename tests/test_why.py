from dashboard.presentation import _why


def test_why_explains_the_score_mechanism_and_unknowns():
    attribution = {"total_abs_bps": 19.0, "candidates": [
        {"type": "macro_release", "kind": "labor", "name": "Jobs report", "attributed_bps": 4.0, "sessions": [
            {"date": "2026-10-02", "role": "release", "fit": 1.0, "prior": 1.0, "change_bps": 4.0,
             "attributed_bps": 4.0, "led_by": "short end", "short_change_bps": 5.0, "long_change_bps": 2.0,
             "competitors": 0}]},
        {"type": "corporate_deal", "name": "Deal", "attributed_bps": 1.0, "sessions": []},
        {"type": "treasury_auction", "name": "Auction", "attributed_bps": 0.0, "sessions": []}]}
    why = _why(attribution)
    assert [w["name"] for w in why] == ["Jobs report", "Deal"]          # top two credited only
    first = why[0]
    assert "4.0 of 19 bp" in first["rank_text"]
    assert first["lines"] == [("2026-10-02: the 10-year moved +4 bp (2-year +5 bp, 30-year +2 bp, so the short end led). "
                              "Weight = prior 1 × fit 1: a move led by the short end is what news about the Fed's "
                              "path usually produces. No other candidate fit that day; it received 4.0 bp, all of "
                              "the move.")]
    assert "2-year" in first["mechanism"] and "cannot say whether the report was strong" in first["unknown"]
    assert "never confirmed" in why[1]["unknown"] and why[1]["price_note"] == ""
