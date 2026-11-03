from __future__ import annotations

import re
from datetime import date

from dashboard.overview import _percentile_state, overview, sparkline
from dashboard.renderer import TABS, font_faces, render_report
from treasury_flow_radar.analytics.research import build_research_report


def _view(**basis_overrides):
    basis = {
        "state": {"crowded": False, "funding_tight": False, "vol_elevated": False},
        "crowding": {"aggregate": {"short_percentile": 79.0, "net_10y_equivalents": -5.18e6,
                                   "history_weeks": 552, "history_start": "2016-03-08",
                                   "weekly_change_10y_equivalents": -6.9e4, "report_date": "2026-10-13",
                                   "history": [{"date": "2026-10-06", "value": -5.1e6},
                                               {"date": "2026-10-13", "value": -5.18e6}]}},
        "funding": {"sofr_minus_iorb_median_bps": -2.0, "median_sessions": 10, "sofr_minus_iorb_bps": -3.0,
                    "date": "2026-10-19"},
        "volatility": {"realized_vol_percentile": 65.0, "realized_vol_bps_per_day": 5.4, "sessions": 20},
    }
    basis.update(basis_overrides)
    return {"basis": basis,
            "synthesis": {"tone": "quiet", "headline": "Treasury plumbing is quiet, but the trade is large.",
                          "lines": [{"text": "Nothing is forcing it to unwind."}],
                          "as_of": {"positioning": "2026-10-13", "funding": "2026-10-19"}}}


def test_percentile_state_marks_near_and_over_the_line():
    assert _percentile_state(None, None) == "unknown"
    assert _percentile_state(60, False) == "clear"
    assert _percentile_state(79, False) == "near"          # within 5 points of the 80th-percentile line
    assert _percentile_state(85, True) == "over"


def test_instrument_draws_each_input_against_its_trip_line():
    html = overview(_view())
    assert "0 of 3 trip lines crossed" in html
    assert html.count('class="gauge"') == 3
    assert "79th pct" in html and "g-near" in html            # crowding sits just under its line
    assert "−2 bp" in html                                     # funding median, true minus sign
    assert "Nothing is forcing it to unwind." in html


def test_crossed_line_is_counted_and_shaded():
    view = _view()
    view["basis"]["state"]["crowded"] = True
    view["basis"]["crowding"]["aggregate"]["short_percentile"] = 91.0
    html = overview(view)
    assert "1 of 3 trip lines crossed" in html
    assert 'class="g-band"' in html
    assert 'class="gauge g-tripped" id="g-crowding"' in html
    # crossed chip leads the strip and links to its gauge
    assert html.index('trip trip-over" href="#g-crowding"') < html.index('href="#g-funding"')


def test_crossed_reading_is_not_styled_as_the_band():
    # g-over colours a crossed reading; only g-band may be absolutely positioned (else it
    # pulls the reading out of its row and stretches it down the page).
    view = _view()
    view["basis"]["state"]["funding_tight"] = True
    view["basis"]["funding"]["sofr_minus_iorb_median_bps"] = 3.0
    html = overview(view)
    assert '<span class="g-read g-over">+3 bp' in html
    page = render_report(build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1)))
    assert not re.search(r"\.g-over\{[^}]*position", page)
    assert re.search(r"\.g-band\{position:absolute", page)


def test_missing_inputs_render_as_unknown_not_zero():
    html = overview(_view(funding=None, volatility=None))
    assert "No SOFR or IORB observations stored." in html
    assert "g-track g-empty" in html


def test_no_plumbing_data_says_so():
    html = overview({"basis": {}, "synthesis": {}})
    assert "plumbing read is not available" in html


def test_sparkline_needs_two_points_and_marks_the_end():
    assert sparkline([1.0]) == ""
    svg = sparkline([1.0, 2.0, 1.5], baseline=0.0, label="x")
    assert svg.startswith("<svg") and "spark-end" in svg and "spark-base" in svg


def test_page_has_tabs_panels_and_stays_offline():
    html = render_report(build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1)))
    for key, _label in TABS:
        assert f'id="panel-{key}"' in html and f'data-tab="{key}"' in html
    assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=['\"]https?://", html, re.IGNORECASE)
    assert "url(http" not in html
    assert font_faces() in html and "font/woff2;base64," in font_faces()


def test_crowding_context_compares_to_long_record_and_past_year():
    from dashboard.overview import crowding_context

    history = [{"date": f"2025-{1 + i // 5:02d}-{1 + i % 5 * 5:02d}", "value": -v}
               for i, v in enumerate([7.0e6] + [6.0e6] * 50 + [5.18e6])]
    agg = {"short_percentile": 79.0, "net_10y_equivalents": -5.18e6, "history_weeks": 552,
           "history_start": "2016-03-08", "crowded_threshold_10y_equivalents": 5.37e6,
           "peak_short_10y_equivalents": 7.26e6, "net_dv01_usd_per_bp": -2.98e8, "history": history}
    verdict, points, so_what = crowding_context(agg, crowded=False)
    assert verdict == "large"
    assert "$518B" in points[0]                                  # 10-year notes have $100,000 face
    assert "larger than 79% of the 552 weeks since 2016" in points[1]
    assert "(4%) under the crowded line" in points[1]
    assert "near the low end of its 5.18M to 7.00M range" in points[2]
    assert "$298M per basis point" in so_what and "size alone forces nothing" in so_what


def test_funding_context_flags_a_thin_cushion_and_a_hot_tail():
    from dashboard.overview import funding_context

    funding = {"sofr_minus_iorb_median_bps": -2.0, "sofr_minus_iorb_median_share_below": 83.0,
               "median_sessions": 10, "sofr_minus_iorb_bps": -3.0, "date": "2026-10-19",
               "history_start": "2021-07-29", "sofr99_minus_iorb_bps": 5.0}
    verdict, points, so_what = funding_context(funding, tight=False)
    assert verdict == "thinning"
    assert "2 bp below" in points[0] and "Tighter than in 83% of sessions since Jul 2021." in points[1]
    assert "already above it" in points[2] and "cushion is thinner" in so_what
    loose = dict(funding, sofr_minus_iorb_median_bps=-8.0, sofr_minus_iorb_median_share_below=10.0)
    assert funding_context(loose, tight=False)[0] == "loose"


def test_flat_funding_history_is_not_called_thinning():
    from treasury_flow_radar.analytics.plumbing_synthesis import funding_thinning

    # Every session ties at the current median: nothing is strictly looser, so the cushion has not thinned.
    assert not funding_thinning({"sofr_minus_iorb_median_bps": -2.0, "sofr_minus_iorb_median_percentile": 100.0,
                                 "sofr_minus_iorb_median_share_below": 0.0})
    assert not funding_thinning({"sofr_minus_iorb_median_bps": 1.0, "sofr_minus_iorb_median_share_below": 99.0})


def test_volatility_context_states_distance_to_the_line():
    from dashboard.overview import volatility_context

    verdict, points, _ = volatility_context({"realized_vol_percentile": 65.0, "realized_vol_bps_per_day": 5.4,
                                             "elevated_threshold_bps_per_day": 6.7, "sessions": 20,
                                             "history_sessions": 5080}, elevated=False)
    assert verdict == "ordinary"
    assert "5.4 bp on a typical day" in points[0] and "about 20 years" in points[1] and "24% more movement" in points[1]


def test_each_gauge_says_what_it_means_for_the_readers_sectors():
    view = _view()
    view["basis"]["state"]["funding_tight"] = True
    view["basis"]["funding"]["sofr_minus_iorb_median_bps"] = 3.0
    view["synthesis"]["tone"] = "watch"
    html = overview(view)
    assert html.count('class="g-you"') == 3
    assert "What this means for you" in html
    assert "Borrowing cash against Treasuries now costs more" in html      # funding: tight
    assert html.count('class="g-you-s"') == 3                              # sector lines only when flashing
    assert "nothing is forcing" in html                                    # crowding: large (79th)
    assert "not a buy or sell signal" in html


def test_for_you_lines_never_give_trade_instructions():
    from dashboard import for_you as fy
    entries = [*fy.FUNDING.values(), *fy.CROWDING.values(), *fy.VOLATILITY.values()]
    texts = [*fy.OVERALL.values(), *(e[0] for e in entries), *(v for e in entries for v in e[1].values())]
    banned = re.compile(r"\b(buy|sell now|should sell|should buy|reduce|trim|exit|hedge your|price target)\b", re.IGNORECASE)
    assert not [t for t in texts if banned.search(t)]
    assert fy.for_you(fy.FUNDING, "—") is None


def test_page_links_home_screen_icon_and_export_ships_it(tmp_path):
    from dashboard.export import ICONS, copy_icons
    html = render_report(build_research_report([], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1)))
    assert '<link rel="apple-touch-icon" href="apple-touch-icon.png">' in html
    assert '<meta name="apple-mobile-web-app-title" content="Flow Radar">' in html
    assert '<link rel="manifest" href="manifest.webmanifest">' in html
    copy_icons(tmp_path)
    for name in (n for n in ICONS if n.endswith(".png")):
        data = (tmp_path / name).read_bytes()
        assert data.startswith(b"\x89PNG") and len(data) > 200
    import json
    manifest = json.loads((tmp_path / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["short_name"] == "Flow Radar"
    assert {"maskable"} <= {i["purpose"] for i in manifest["icons"]}
    assert all((tmp_path / i["src"]).exists() for i in manifest["icons"])
