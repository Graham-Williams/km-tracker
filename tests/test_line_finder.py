"""Line Finder — the pure math (line_finder.compute and friends), the DB loader
(line_finder.load_pair) and the /line-finder routes.

Fixtures are small and synthetic on purpose: the numbers are hand-checkable.
The favourite is "a", the receiver of the line is "b"; margin = a - b.
"""

import math
from datetime import date

import pytest

import app as app_module
import line_finder as lf
from db import get_connection
from helpers import create_player

TODAY = date(2026, 9, 18)


def cup(
    id,
    date_str,
    edition,
    a,
    b,
    n_players=2,
    first_edition=None,
    a_blocks=None,
    b_blocks=None,
    b_line=0,
    a_line=0,
):
    return {
        "id": id,
        "date": date_str,
        "game_edition": edition,
        "first_edition": first_edition,
        "n_players": n_players,
        "a_score": a,
        "b_score": b,
        "a_block1": None if a_blocks is None else a_blocks[0],
        "a_block2": None if a_blocks is None else a_blocks[1],
        "b_block1": None if b_blocks is None else b_blocks[0],
        "b_block2": None if b_blocks is None else b_blocks[1],
        "a_line": a_line,
        "b_line": b_line,
    }


# =============================================================================
# Constants stay in step with the app
# =============================================================================


def test_race_constants_mirror_the_app():
    assert lf.RACES_PER_CUP == app_module.MAX_RACES
    assert lf.RACES_PER_BLOCK == app_module.RACES_PER_BLOCK


def test_lines_grid_covers_the_integer_range_in_half_steps():
    assert lf.LINES[0] == -10 and lf.LINES[-1] == 35
    assert all(x * 2 == int(x * 2) for x in lf.LINES)
    assert len(lf.LINES) == (35 - (-10)) * 2 + 1


# =============================================================================
# Weights
# =============================================================================


def test_weight_halves_every_half_life():
    assert lf.weight(0, 90) == 1.0
    assert lf.weight(90, 90) == pytest.approx(0.5)
    assert lf.weight(180, 90) == pytest.approx(0.25)


def test_weight_unweighted_when_half_life_is_none():
    assert lf.weight(0, None) == 1.0
    assert lf.weight(1000, None) == 1.0


def test_weight_clamps_future_dated_cups_to_one():
    assert lf.weight(-30, 90) == 1.0


# =============================================================================
# Per-edition pooling (pure cups + mixed blocks)
# =============================================================================


def test_edition_samples_pure_cups_and_mixed_blocks():
    cups = [
        cup(1, "2026-09-18", "wii", 50, 30),  # +20 over 4 races
        cup(2, "2026-09-18", "mk8dx", 40, 44),  # -4 over 4 races
        cup(
            3,
            "2026-09-18",
            "mixed",
            60,
            50,
            first_edition="mk8dx",
            a_blocks=(30, 30),
            b_blocks=(31, 19),  # switch block -1, wii block +11
        ),
    ]
    samples = lf.edition_samples(cups, None, TODAY)
    assert samples["wii"] == [(20, 4, 1.0), (11, 2, 1.0)]
    assert samples["mk8dx"] == [(-4, 4, 1.0), (-1, 2, 1.0)]


def test_mixed_cup_without_blocks_contributes_no_edition_samples():
    cups = [cup(1, "2026-09-18", "mixed", 60, 50, first_edition="wii")]
    samples = lf.edition_samples(cups, None, TODAY)
    assert samples == {"wii": [], "mk8dx": []}


def test_mixed_cup_with_garbage_first_edition_falls_back_to_default():
    cups = [
        cup(1, "2026-09-18", "mixed", 60, 50, first_edition=None, a_blocks=(30, 30), b_blocks=(20, 30))
    ]
    samples = lf.edition_samples(cups, None, TODAY)
    # block 1 -> DEFAULT_EDITION (wii), block 2 -> the other console
    assert samples["wii"] == [(10, 2, 1.0)]
    assert samples["mk8dx"] == [(0, 2, 1.0)]


def test_edition_stats_weighted_edge_and_unweighted_sd():
    # Two pure cups: +20 (weight 1) and +4 (weight 0.5).
    samples = [(20, 4, 1.0), (4, 4, 0.5)]
    st = lf.edition_stats(samples)
    # mu = (20*1 + 4*0.5) / (4*1 + 4*0.5) = 22 / 6
    assert st["mu"] == pytest.approx(22 / 6)
    # sd = sqrt(((20 - 4mu)^2 + (4 - 4mu)^2) / 8), unweighted
    mu = 22 / 6
    assert st["sd"] == pytest.approx(math.sqrt(((20 - 4 * mu) ** 2 + (4 - 4 * mu) ** 2) / 8))
    assert st["n_samples"] == 2 and st["races"] == 8


def test_edition_stats_zero_weight_sum_is_no_usable_samples():
    # Every weight underflowed to 0.0 (cups far older than the half-life
    # allows) -> no evidence, never a ZeroDivisionError.
    st = lf.edition_stats([(20, 4, 0.0), (10, 4, 0.0)])
    assert st == {"n_samples": 2, "races": 8, "mu": None, "sd": None}


def test_future_dated_cup_never_breaks_the_math():
    """A cup dated in the year 2400 alongside normal cups: it weighs 1 today
    (clamped, no OverflowError), and when the backtest weights FROM its date
    every prior weight underflows to 0 -> that row is 'not enough history',
    not a ZeroDivisionError."""
    cups = _history() + [cup(99, "2400-01-01", "wii", 60, 40, b_line=9)]
    for hl in (14, 90):
        out = lf.compute(cups, half_life=hl, today=TODAY, players=("A", "B"), stored_line=9)
        assert out["n_cups"] == 8
        assert out["formats"]["wii"]["rec"] is not None
        future = out["backtest"]["rows"][-1]
        assert future["date"] == "2400-01-01"
        assert future["rec_line"] is None and future["rec_outcome"] is None
        assert future["used_outcome"] == "a"
        # The running mean at the future cup is that cup alone (weight 1).
        assert out["trend"]["running"]["wii"][-1]["value"] == pytest.approx(20.0)
        assert all(isinstance(n, str) for n in out["formats"]["wii"]["notes"])


def test_all_ancient_cups_are_not_enough_evidence():
    cups = [cup(1, "1900-01-01", "wii", 60, 40), cup(2, "1900-02-01", "wii", 50, 40)]
    out = lf.compute(cups, half_life=14, today=TODAY)
    wii = out["formats"]["wii"]
    assert wii["n"] == 2 and wii["actual"] is not None
    assert wii["rec"] is None and wii["fitted"] is None
    assert any("Not enough" in n for n in wii["notes"])
    assert any("recency-weighted n/a" in n for n in wii["notes"])


def test_edition_stats_needs_two_samples_for_sd():
    st = lf.edition_stats([(20, 4, 1.0)])
    assert st["mu"] == 5.0
    assert st["sd"] is None
    assert lf.edition_stats([]) == {"n_samples": 0, "races": 0, "mu": None, "sd": None}


def test_format_models_combine_editions():
    stats = {
        "wii": {"n_samples": 5, "races": 20, "mu": 4.0, "sd": 5.0},
        "mk8dx": {"n_samples": 4, "races": 16, "mu": -0.5, "sd": 4.0},
    }
    wii = lf.format_model("wii", stats)
    assert wii["mean"] == pytest.approx(16.0)
    assert wii["sd"] == pytest.approx(10.0)  # 2 * sd_race
    assert wii["se"] == pytest.approx(10.0 / math.sqrt(5))  # sd_cup / sqrt(n cups)
    mixed = lf.format_model("mixed", stats)
    assert mixed["mean"] == pytest.approx(2 * 4.0 + 2 * -0.5)
    assert mixed["sd"] == pytest.approx(math.sqrt(2 * 25 + 2 * 16))
    assert mixed["se"] == pytest.approx(math.sqrt(100 / 20 + 64 / 16))


def test_format_model_none_when_an_edition_has_no_sd():
    stats = {
        "wii": {"n_samples": 5, "races": 20, "mu": 4.0, "sd": 5.0},
        "mk8dx": {"n_samples": 1, "races": 4, "mu": -0.5, "sd": None},
    }
    assert lf.format_model("wii", stats) is not None
    assert lf.format_model("mk8dx", stats) is None
    assert lf.format_model("mixed", stats) is None


# =============================================================================
# Curves
# =============================================================================


def test_actual_curve_counts_ties_as_half():
    pcts, wins, ties = lf.actual_curve([10, 10, 20, 0], lines=[5, 10, 15, 25])
    assert pcts == [75.0, 50.0, 25.0, 0.0]
    assert wins == [3, 1, 1, 0]
    assert ties == [0, 2, 0, 0]


def test_actual_curve_empty():
    assert lf.actual_curve([]) == (None, None, None)


def test_fitted_pct_is_the_normal_tail():
    assert lf.fitted_pct(10, 5, 10) == pytest.approx(50.0)
    assert lf.fitted_pct(10, 5, 0) == pytest.approx((1 - lf.phi(-2)) * 100)
    assert lf.fitted_pct(10, 5, 0) > 97


def test_fitted_pct_zero_spread_is_a_step():
    assert lf.fitted_pct(10, 0, 5) == 100.0
    assert lf.fitted_pct(10, 0, 10) == 50.0
    assert lf.fitted_pct(10, 0, 15) == 0.0


def test_pairs_proxy_is_every_wii_by_every_switch():
    margins, capped = lf.pairs_proxy_margins([20, 10], [0])
    assert sorted(margins) == [5.0, 10.0] and capped is False
    assert lf.pairs_proxy_margins([], [0]) == ([], False)


def test_pairs_proxy_caps_at_the_most_recent_100_cups_per_console():
    wii = list(range(101))  # chronological; the oldest (0) must drop
    margins, capped = lf.pairs_proxy_margins(wii, [0])
    assert capped is True and len(margins) == 100
    assert 0.0 not in margins and 50.0 in margins  # 100/2 + 0
    margins, capped = lf.pairs_proxy_margins(wii[:100], [0])
    assert capped is False and len(margins) == 100


def test_compute_reports_the_pairs_cap():
    cups = [cup(i, f"2020-01-{1 + i % 28:02d}", "wii", 40 + (i % 5), 40) for i in range(101)]
    cups += [cup(500, "2026-09-01", "mk8dx", 40, 40)]
    out = lf.compute(cups, half_life=None, today=TODAY)
    mixed = out["formats"]["mixed"]
    assert mixed["actual_source"] == "pairs"
    assert mixed["actual_n"] == 100 and mixed["pairs_capped"] is True
    assert any("most recent 100 cups per console" in n for n in mixed["notes"])
    assert out["formats"]["wii"]["pairs_capped"] is False
    small = lf.compute(_history(), half_life=None, today=TODAY)["formats"]["mixed"]
    assert small["pairs_capped"] is False


# =============================================================================
# Recommendation
# =============================================================================


@pytest.mark.parametrize(
    "mean,expected",
    [(18.4, 18), (18.5, 19), (-0.6, -1), (-0.4, 0), (0.0, 0), (-2.5, -3), (0.49, 0)],
)
def test_recommend_rounds_half_away_from_zero(mean, expected):
    assert lf.recommend(mean) == expected


def test_fmt_line_renders_even_and_halves():
    assert lf.fmt_line(0) == "even"
    assert lf.fmt_line(18) == "+18"
    assert lf.fmt_line(-4) == "-4"
    assert lf.fmt_line(8.5) == "+8½"
    assert lf.fmt_line(-0.5) == "-½"
    assert lf.fmt_line(0.5) == "+½"


@pytest.mark.parametrize(
    "mean,se,expected",
    [
        (-0.6, 3.5, (0, True)),  # inside the noise -> even
        (2.9, 3.0, (0, True)),
        (18.4, 2.2, (18, False)),  # clear edge -> round(mean)
        (-4.0, 3.5, (-4, False)),
        (3.0, 3.0, (3, False)),  # boundary: |mean| == se is NOT inside
        (0.0, 0.0, (0, False)),
    ],
)
def test_recommend_line_is_even_inside_the_noise(mean, se, expected):
    assert lf.recommend_line({"mean": mean, "sd": 1.0, "se": se}) == expected


def test_half_line_is_the_half_step_nearest_the_mean():
    assert lf.half_line(8.9, 9) == 8.5
    assert lf.half_line(9.2, 9) == 9.5


# =============================================================================
# compute(): end to end on a tiny synthetic history
# =============================================================================


def _history():
    """4 Wii cups, 3 Switch cups, no mixed. Dates are all 'today' so weights
    are 1 and the numbers are hand-checkable."""
    return [
        cup(1, "2026-09-01", "wii", 60, 40, n_players=3, b_line=9),  # +20
        cup(2, "2026-09-02", "wii", 55, 40, n_players=3, b_line=6),  # +15
        cup(3, "2026-09-03", "wii", 50, 40, n_players=2, b_line=9),  # +10
        cup(4, "2026-09-04", "wii", 45, 40, n_players=2, b_line=9),  # +5
        cup(5, "2026-09-05", "mk8dx", 40, 40),  # 0
        cup(6, "2026-09-06", "mk8dx", 44, 40),  # +4
        cup(7, "2026-09-07", "mk8dx", 36, 40),  # -4
    ]


def test_compute_recommendations_and_shape():
    out = lf.compute(_history(), half_life=None, today=TODAY, players=("A", "B"), stored_line=9)
    assert out["available"] is True
    assert out["players"] == {"a": "A", "b": "B"}
    assert out["n_cups"] == 7
    assert out["settings"] == {"half_life": None, "two_player": False, "today": "2026-09-18"}
    wii, sw, mixed = out["formats"]["wii"], out["formats"]["mk8dx"], out["formats"]["mixed"]
    # Wii: mean margin 12.5 -> rec 13 (half away from zero on 12.5); the edge
    # is well outside its standard error, so the rounding rule applies.
    assert wii["model"]["mean"] == pytest.approx(12.5)
    assert wii["rec"] == 13
    assert wii["rec_within_noise"] is False
    assert wii["se"] < 12.5
    assert not any("distinguishable" in n for n in wii["notes"])
    assert wii["n"] == 4
    assert wii["actual_source"] == "real"
    # Switch: mean 0 -> "even"
    assert sw["model"]["mean"] == pytest.approx(0.0)
    assert sw["rec"] == 0
    # Mixed: 2mu_w + 2mu_s = 12.5/2 + 0 = 6.25 -> 6; no real mixed -> pairs proxy
    assert mixed["model"]["mean"] == pytest.approx(6.25)
    assert mixed["rec"] == 6
    assert mixed["n"] == 0
    assert mixed["actual_source"] == "pairs"
    assert mixed["actual_n"] == 12
    # Curves are on the shared grid and are percentages.
    for fm in (wii, sw, mixed):
        assert len(fm["actual"]) == len(out["lines"]) == len(fm["fitted"])
        assert all(0 <= v <= 100 for v in fm["actual"])
        assert all(0 <= v <= 100 for v in fm["fitted"])
    # Actual Wii at line 10: margins 20,15 win, 10 ties, 5 loses -> 2.5/4
    i10 = out["lines"].index(10.0)
    assert wii["actual"][i10] == pytest.approx(62.5)
    assert wii["actual_wins"][i10] == 2 and wii["actual_ties"][i10] == 1
    # Fitted at the recommended line is ~50% (rounded mean).
    assert abs(wii["fitted_at_rec"] - 50) < 5
    # Notes mention the stored line on the Wii tile only.
    assert any("stored line for B is +9" in n for n in wii["notes"])
    assert not any("stored line" in n for n in sw["notes"])
    # The 2-player read (from the unfiltered cups) is in the notes.
    assert any("Just the two of them: 2 Wii cups, average +7.5" in n for n in wii["notes"])
    assert any("pairs" in n for n in mixed["notes"])
    # Cups list is newest first with the line used; ids stay (the page links
    # each row to the cup) — nothing else in the payload carries a cup id.
    assert [c["id"] for c in out["cups"]] == [7, 6, 5, 4, 3, 2, 1]
    assert out["cups"][-1]["line_used"] == 9
    assert out["cups"][-1]["format_label"] == "Wii"
    for key in ("editions", "stored_line"):
        assert key not in out
    assert "dates" not in wii and "half_line" not in wii
    assert "cup_id" not in out["trend"]["points"][0]
    assert "cup_id" not in out["backtest"]["rows"][0]
    assert "cup_id" not in out["line_history"]["rows"][0]
    assert "changes" not in out["line_history"]


def test_compute_recommends_even_when_the_edge_is_inside_the_noise():
    """Switch margins +1, +3, -2: mean +0.67 but SE ~1.2, so the fitted edge is
    not distinguishable from zero -> recommend even (not +1), say why, and
    keep the raw mean/SE in the JSON."""
    cups = [
        cup(1, "2026-09-01", "mk8dx", 41, 40),
        cup(2, "2026-09-02", "mk8dx", 43, 40),
        cup(3, "2026-09-03", "mk8dx", 38, 40),
    ]
    out = lf.compute(cups, half_life=None, today=TODAY, players=("A", "B"))
    sw = out["formats"]["mk8dx"]
    assert sw["model"]["mean"] == pytest.approx(2 / 3, abs=1e-3)
    assert sw["se"] > abs(sw["model"]["mean"])
    assert sw["rec"] == 0
    assert sw["rec_within_noise"] is True
    assert sw["fitted_at_rec"] == pytest.approx(lf.fitted_pct(2 / 3, sw["model"]["sd"], 0), abs=0.05)
    assert any("not distinguishable from even" in n for n in sw["notes"])
    assert any("Fitted +0.7 ± " in n for n in sw["notes"])
    assert any("at even (fitted" in n for n in sw["notes"])
    assert not any("rule out ties" in n for n in sw["notes"])
    # The same edge with far less noise rounds normally.
    tight = lf.compute(cups * 40, half_life=None, today=TODAY)["formats"]["mk8dx"]
    assert tight["se"] < abs(tight["model"]["mean"])
    assert tight["rec"] == 1 and tight["rec_within_noise"] is False
    assert any("Use +½ to rule out ties" in n for n in tight["notes"])


def test_compute_uses_real_mixed_cups_over_the_pairs_proxy():
    cups = _history() + [
        cup(8, "2026-09-08", "mixed", 60, 50, first_edition="wii", a_blocks=(30, 30), b_blocks=(20, 30)),
    ]
    out = lf.compute(cups, half_life=None, today=TODAY)
    mixed = out["formats"]["mixed"]
    assert mixed["n"] == 1
    assert mixed["actual_source"] == "real"
    assert mixed["actual_n"] == 1
    # Real margin +10: 100% below it, 50% at it, 0% above.
    lines = out["lines"]
    assert mixed["actual"][lines.index(5.0)] == 100
    assert mixed["actual"][lines.index(10.0)] == 50
    assert mixed["actual"][lines.index(15.0)] == 0
    assert any("1 real mixed cup drive" in n for n in mixed["notes"])
    # The blocks fed the per-edition pools: wii gained (10, 2), switch (0, 2).
    samples = lf.edition_samples(cups, None, TODAY)
    assert len(samples["wii"]) == 5 and len(samples["mk8dx"]) == 4
    assert out["cups"][0]["format_label"] == "Wii → Switch"


def test_compute_recency_weighting_leans_toward_recent_cups():
    cups = [
        cup(1, "2026-01-01", "wii", 60, 40),  # +20, old
        cup(2, "2026-09-17", "wii", 40, 40),  # 0, yesterday
    ]
    unweighted = lf.compute(cups, half_life=None, today=TODAY)["formats"]["wii"]["model"]["mean"]
    weighted = lf.compute(cups, half_life=30, today=TODAY)["formats"]["wii"]["model"]["mean"]
    assert unweighted == pytest.approx(10.0)
    assert weighted < 1.0


def test_compute_not_enough_cups_paths():
    # One Wii cup: no sd -> no Wii model, no mixed model; Switch has nothing.
    out = lf.compute([cup(1, "2026-09-01", "wii", 60, 40)], half_life=None, today=TODAY)
    wii = out["formats"]["wii"]
    assert wii["n"] == 1 and wii["actual"] is not None
    assert wii["fitted"] is None and wii["rec"] is None and wii["se"] is None
    assert any("Not enough Wii cups" in n for n in wii["notes"])
    sw = out["formats"]["mk8dx"]
    assert sw["n"] == 0 and sw["actual"] is None and sw["rec"] is None
    mixed = out["formats"]["mixed"]
    assert mixed["actual"] is None and mixed["actual_source"] is None and mixed["rec"] is None
    assert any("no Wii×Switch pairs" in n for n in mixed["notes"])


def test_compute_with_no_cups_at_all():
    out = lf.compute([], half_life=90, today=TODAY)
    assert out["n_cups"] == 0
    assert out["cups"] == []
    assert out["backtest"]["rows"] == []
    assert all(fm["rec"] is None for fm in out["formats"].values())


def test_compute_two_player_filter():
    out = lf.compute(_history(), half_life=None, two_player=True, today=TODAY)
    assert out["n_cups"] == 5
    assert out["n_all_cups"] == 7
    assert out["settings"]["two_player"] is True
    wii = out["formats"]["wii"]
    assert wii["n"] == 2
    assert wii["margins"] == [10, 5]
    assert wii["model"]["mean"] == pytest.approx(7.5)
    assert all(c["n_players"] == 2 for c in out["cups"])
    assert len(out["cups"]) == 5
    # Every downstream section is built from the same filtered set.
    assert len(out["backtest"]["rows"]) == 5
    assert len(out["trend"]["points"]) == 5
    assert all(p["n_players"] == 2 for p in out["trend"]["points"])
    assert len(out["trend"]["running"]["wii"]) == 2
    assert len(out["line_history"]["rows"]) == 5
    # Pairs proxy: 2 Wii x 3 Switch, not 4 x 3.
    mixed = out["formats"]["mixed"]
    assert mixed["actual_source"] == "pairs" and mixed["actual_n"] == 6


def test_compute_ignores_unknown_editions():
    cups = _history() + [cup(9, "2026-09-09", "gamecube", 99, 0)]
    out = lf.compute(cups, half_life=None, today=TODAY)
    assert out["n_cups"] == 7


def test_compute_is_json_serialisable():
    import json

    out = lf.compute(_history(), half_life=90, today=TODAY, line_changes=[
        {"cup_id": 1, "date": "2026-09-01 19:00:00", "line_before": 9, "line_after": 6}
    ], stored_line=6)
    json.dumps(out)


# =============================================================================
# Backtest
# =============================================================================


def test_backtest_walks_chronologically_with_min_history():
    out = lf.backtest(_history(), half_life=None)
    rows = out["rows"]
    assert [r["date"] for r in rows] == [f"2026-09-0{i}" for i in range(1, 8)]
    # First three Wii cups have < 3 prior Wii samples -> not enough history.
    assert [r["rec_line"] for r in rows[:3]] == [None, None, None]
    assert all(r["rec_outcome"] is None for r in rows[:3])
    # 4th Wii cup: prior margins 20, 15, 10 -> mean 15 -> rec 15; margin 5 -> b wins.
    assert rows[3]["rec_line"] == 15
    assert rows[3]["rec_outcome"] == "b"
    # The line actually used (9) also has the receiver winning that cup (5 < 9).
    assert rows[3]["line_used"] == 9 and rows[3]["used_outcome"] == "b"
    # Cup 1 under the used line 9: margin 20 > 9 -> a.
    assert rows[0]["used_outcome"] == "a"
    # Switch cups never reach 3 prior samples.
    assert all(r["rec_line"] is None for r in rows[4:])
    summary = out["summary"]
    assert summary["wii"] == {
        "n": 1,
        "skipped": 3,
        "rec": {"a": 0, "b": 1, "tie": 0},
        "used": {"a": 0, "b": 1, "tie": 0},
    }
    assert summary["mk8dx"]["n"] == 0 and summary["mk8dx"]["skipped"] == 3
    assert summary["mixed"]["n"] == 0 and summary["mixed"]["skipped"] == 0


def test_backtest_recommends_even_inside_the_noise():
    """Prior Switch cups +1, +3, -2 -> mean +0.67 inside SE ~1.2 -> the walk
    hands the 4th cup an 'even' line, not +1 (same rule as the tiles)."""
    cups = [
        cup(1, "2026-09-01", "mk8dx", 41, 40),
        cup(2, "2026-09-02", "mk8dx", 43, 40),
        cup(3, "2026-09-03", "mk8dx", 38, 40),
        cup(4, "2026-09-04", "mk8dx", 40, 40),  # margin 0 -> a tie at "even"
    ]
    out = lf.backtest(cups, half_life=None)
    row = out["rows"][3]
    assert row["rec_line"] == 0
    assert row["rec_outcome"] == "tie"
    assert out["summary"]["mk8dx"]["rec"] == {"a": 0, "b": 0, "tie": 1}


def test_backtest_line_used_is_net_of_the_favourites_line():
    """The app decides a cup on line_score, so 'the line actually used' is
    b_line - a_line. Margin +3 with B +9 / A +7: net +2 -> A still wins;
    comparing against B's +9 alone would wrongly hand it to B."""
    cups = [
        cup(1, "2026-09-01", "wii", 60, 40, b_line=9),
        cup(2, "2026-09-02", "wii", 43, 40, b_line=9, a_line=7),
        cup(3, "2026-09-03", "wii", 45, 40, b_line=9, a_line=4),  # net 5 -> tie
    ]
    rows = lf.backtest(cups, half_life=None)["rows"]
    assert rows[0]["line_used"] == 9 and rows[0]["used_outcome"] == "a"
    assert rows[1]["line_used"] == 2 and rows[1]["used_outcome"] == "a"
    assert rows[2]["line_used"] == 5 and rows[2]["used_outcome"] == "tie"


def test_backtest_counts_ties_and_mixed_needs_both_editions():
    cups = [
        cup(1, "2026-01-01", "wii", 50, 40),
        cup(2, "2026-01-02", "wii", 50, 40),
        cup(3, "2026-01-03", "wii", 50, 40),
        cup(4, "2026-01-04", "wii", 50, 40),  # rec 10, margin 10 -> tie
        cup(5, "2026-01-05", "mk8dx", 40, 40),
        cup(6, "2026-01-06", "mixed", 50, 45, first_edition="wii", a_blocks=(25, 25), b_blocks=(20, 25)),
        cup(7, "2026-01-07", "mk8dx", 40, 40),
        cup(8, "2026-01-08", "mixed", 50, 45, first_edition="wii", a_blocks=(25, 25), b_blocks=(20, 25)),
        cup(9, "2026-01-09", "mixed", 50, 45, first_edition="mk8dx", a_blocks=(25, 25), b_blocks=(25, 20)),
    ]
    out = lf.backtest(cups, half_life=None)
    by_id = {r["date"]: r for r in out["rows"]}
    assert by_id["2026-01-04"]["rec_line"] == 10 and by_id["2026-01-04"]["rec_outcome"] == "tie"
    # Cup 6 (mixed): 16 wii races but only 4 switch -> not enough.
    assert by_id["2026-01-06"]["rec_line"] is None
    # Cup 8: switch races so far = cup5 (4) + cup6 block (2) + cup7 (4) = 10
    # -> still short of 12, even though that is three samples.
    assert by_id["2026-01-08"]["rec_line"] is None
    # Cup 9: + cup8's switch block = 12 -> enough.
    assert by_id["2026-01-09"]["rec_line"] is not None
    assert out["summary"]["wii"]["rec"]["tie"] == 1
    assert out["summary"]["mixed"]["n"] == 1 and out["summary"]["mixed"]["skipped"] == 2


# =============================================================================
# Trend + line history
# =============================================================================


def test_margin_trend_points_and_running_means():
    trend = lf.margin_trend(_history(), half_life=None)
    assert [p["date"] for p in trend["points"]] == [f"2026-09-0{i}" for i in range(1, 8)]
    assert trend["points"][0] == {
        "date": "2026-09-01",
        "format": "wii",
        "margin": 20,
        "n_players": 3,
    }
    wii_running = [p["value"] for p in trend["running"]["wii"]]
    assert wii_running == pytest.approx([20.0, 17.5, 15.0, 12.5])
    assert [p["value"] for p in trend["running"]["mk8dx"]] == pytest.approx([0.0, 2.0, 0.0])


def test_line_history_merges_changes_with_lines_used():
    changes = [
        {"cup_id": 2, "date": "2026-09-02 19:00:00", "line_before": 9, "line_after": 6},
        {"cup_id": 3, "date": "2026-09-03 19:00:00", "line_before": 6, "line_after": 9},
    ]
    hist = lf.line_history(_history(), changes, stored_line=9)
    assert hist["stored_line"] == 9
    assert set(hist) == {"rows", "stored_line"}
    row2 = next(r for r in hist["rows"] if r["date"] == "2026-09-02")
    assert row2["line_used"] == 6 and row2["line_before"] == 9 and row2["line_after"] == 6
    row1 = next(r for r in hist["rows"] if r["date"] == "2026-09-01")
    assert row1["line_before"] is None and row1["line_after"] is None
    assert "cup_id" not in row1


# =============================================================================
# Player pair config
# =============================================================================


def test_players_default_and_env_override():
    assert lf.line_finder_players({}) == lf.DEFAULT_PLAYERS
    assert lf.line_finder_players({"LINE_FINDER_PLAYERS": " Test Toad , Dummy Diddy "}) == (
        "Test Toad",
        "Dummy Diddy",
    )


@pytest.mark.parametrize("raw", ["", "Only One", "A,B,C", "Same,Same", ",", "A,"])
def test_players_malformed_override_falls_back(raw):
    assert lf.line_finder_players({"LINE_FINDER_PLAYERS": raw}) == lf.DEFAULT_PLAYERS


# =============================================================================
# DB loader
# =============================================================================


def _insert_cup(
    conn,
    date_str,
    edition,
    scores,
    status="completed",
    deleted=False,
    first_edition=None,
    cup_players=None,
    blocks=None,
):
    """scores: {player_id: (score, line)}; blocks: {player_id: (b1, b2)}."""
    cur = conn.execute(
        "INSERT INTO cups (date, status, game_edition, first_edition, deleted_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (date_str, status, edition, first_edition, "2026-09-01 00:00:00" if deleted else None),
    )
    cup_id = cur.lastrowid
    for pid, (score, line) in scores.items():
        b1, b2 = (blocks or {}).get(pid, (None, None))
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line, line_score, block1_score, block2_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cup_id, pid, score, line, score + line, b1, b2),
        )
    for pid in cup_players or []:
        conn.execute("INSERT INTO cup_players (cup_id, player_id) VALUES (?, ?)", (cup_id, pid))
    return cup_id


@pytest.fixture
def pair_db(client):
    """Three players (A id 1, B id 2, C id 3) and a spread of cups."""
    create_player(client, "A")
    create_player(client, "B", has_line=True)
    create_player(client, "C")
    conn = get_connection()
    conn.execute("UPDATE players SET line = 9 WHERE id = 2")
    # 1: 3-player Wii session cup, both present
    _insert_cup(conn, "2026-09-01 19:00:00", "wii", {1: (60, 0), 2: (40, 9), 3: (30, 0)}, cup_players=[1, 2, 3])
    # 2: 2-player Wii session cup
    _insert_cup(conn, "2026-09-02 19:00:00", "wii", {1: (50, 0), 2: (45, 9)}, cup_players=[1, 2])
    # 3: hand-entered Wii cup (no cup_players rows) -> n_players from scores
    _insert_cup(conn, "2026-09-03 19:00:00", "wii", {1: (52, 0), 2: (44, 9)})
    # 4: Switch cup, both present
    _insert_cup(conn, "2026-09-04 19:00:00", "mk8dx", {1: (40, 0), 2: (41, 0)}, cup_players=[1, 2])
    # 5: mixed cup with blocks
    _insert_cup(
        conn,
        "2026-09-05 19:00:00",
        "mixed",
        {1: (60, 0), 2: (50, 0)},
        first_edition="mk8dx",
        cup_players=[1, 2],
        blocks={1: (30, 30), 2: (31, 19)},
    )
    # Excluded: cancelled, in_progress, soft-deleted, and one missing B
    _insert_cup(conn, "2026-09-06 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, status="cancelled", cup_players=[1, 2])
    _insert_cup(conn, "2026-09-07 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, status="in_progress", cup_players=[1, 2])
    _insert_cup(conn, "2026-09-08 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, deleted=True, cup_players=[1, 2])
    _insert_cup(conn, "2026-09-09 19:00:00", "wii", {1: (60, 0), 3: (40, 0)}, cup_players=[1, 3])
    # 10: B vs C only — the favourite wasn't in it, so its line change is not
    # the pair's business. 11: cancelled cup B played, with a line change.
    _insert_cup(conn, "2026-09-10 19:00:00", "wii", {2: (60, 6), 3: (40, 0)}, cup_players=[2, 3])
    _insert_cup(conn, "2026-09-11 19:00:00", "wii", {1: (60, 0), 2: (40, 6)}, status="cancelled", cup_players=[1, 2])
    conn.execute(
        "INSERT INTO line_changes (cup_id, player_id, line_before, line_after) VALUES (1, 2, 9, 6)"
    )
    conn.execute(
        "INSERT INTO line_changes (cup_id, player_id, line_before, line_after) VALUES (1, 1, 0, 0)"
    )
    conn.execute(
        "INSERT INTO line_changes (cup_id, player_id, line_before, line_after) VALUES (10, 2, 6, 3)"
    )
    conn.execute(
        "INSERT INTO line_changes (cup_id, player_id, line_before, line_after) VALUES (11, 2, 3, 0)"
    )
    conn.commit()
    conn.close()


def test_load_pair_filters_and_shapes(pair_db):
    conn = get_connection()
    try:
        pair = lf.load_pair(conn, ("A", "B"))
    finally:
        conn.close()
    assert pair["ids"] == (1, 2)
    assert pair["stored_line"] == 9
    cups = pair["cups"]
    assert [c["id"] for c in cups] == [1, 2, 3, 4, 5]
    assert [c["n_players"] for c in cups] == [3, 2, 2, 2, 2]
    c1 = cups[0]
    assert (c1["a_score"], c1["b_score"], c1["a_line"], c1["b_line"]) == (60, 40, 0, 9)
    assert c1["a_block1"] is None
    c5 = cups[4]
    assert c5["game_edition"] == "mixed" and c5["first_edition"] == "mk8dx"
    assert (c5["a_block1"], c5["a_block2"], c5["b_block1"], c5["b_block2"]) == (30, 30, 31, 19)
    # Only B's line changes, and only on cups the PAIR played (completed, not
    # deleted): the change on cup 10 (B vs C) and cup 11 (cancelled) are out.
    assert pair["line_changes"] == [
        {"cup_id": 1, "date": "2026-09-01 19:00:00", "line_before": 9, "line_after": 6}
    ]


def test_load_pair_reads_the_favourites_line(pair_db):
    conn = get_connection()
    try:
        conn.execute("UPDATE scores SET line = 4, line_score = score + 4 WHERE cup_id = 2 AND player_id = 1")
        conn.commit()
        pair = lf.load_pair(conn, ("A", "B"))
    finally:
        conn.close()
    c2 = next(c for c in pair["cups"] if c["id"] == 2)
    assert (c2["a_line"], c2["b_line"]) == (4, 9)
    out = lf.compute(pair["cups"], half_life=None, today=TODAY)
    row = next(r for r in out["backtest"]["rows"] if r["date"] == "2026-09-02")
    assert row["line_used"] == 5  # net 9 - 4


def test_load_pair_missing_player_returns_none(pair_db):
    conn = get_connection()
    try:
        assert lf.load_pair(conn, ("A", "Nobody")) is None
        assert lf.load_pair(conn, ("Nobody", "B")) is None
    finally:
        conn.close()


def test_load_pair_feeds_compute(pair_db):
    conn = get_connection()
    try:
        pair = lf.load_pair(conn, ("A", "B"))
    finally:
        conn.close()
    out = lf.compute(pair["cups"], half_life=None, today=TODAY, players=pair["players"],
                     line_changes=pair["line_changes"], stored_line=pair["stored_line"])
    assert out["n_cups"] == 5
    assert out["formats"]["wii"]["n"] == 3
    assert out["formats"]["mk8dx"]["n"] == 1
    assert out["formats"]["mixed"]["n"] == 1
    # The mixed blocks give Switch its 2nd sample, so Switch gets a model.
    assert len(lf.edition_samples(pair["cups"], None, TODAY)["mk8dx"]) == 2
    assert out["formats"]["mk8dx"]["rec"] is not None


# =============================================================================
# Routes
# =============================================================================


def _seed_pair(client, names=("A", "B")):
    create_player(client, names[0])
    create_player(client, names[1], has_line=True)
    conn = get_connection()
    _insert_cup(conn, "2026-09-01 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, cup_players=[1, 2])
    _insert_cup(conn, "2026-09-02 19:00:00", "wii", {1: (50, 0), 2: (45, 9)}, cup_players=[1, 2])
    _insert_cup(conn, "2026-09-03 19:00:00", "mk8dx", {1: (40, 0), 2: (41, 0)}, cup_players=[1, 2])
    _insert_cup(conn, "2026-09-04 19:00:00", "mk8dx", {1: (44, 0), 2: (41, 0)}, cup_players=[1, 2])
    conn.commit()
    conn.close()


@pytest.fixture
def pair_env(monkeypatch):
    monkeypatch.setenv("LINE_FINDER_PLAYERS", "A,B")


def test_page_renders(client, pair_env):
    _seed_pair(client)
    resp = client.get("/line-finder")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    html = resp.get_data(as_text=True)
    assert "<title>Line Finder — KM Tracker</title>" in html
    assert 'id="line-finder"' in html
    assert 'data-endpoint="/line-finder/data"' in html
    assert 'data-cup-edit-url="/cups/0/edit"' in html
    assert 'data-half-life="90"' in html
    assert 'data-two-player="0"' in html
    assert "js/line_finder.js" in html


def test_page_reflects_query_params(client, pair_env):
    _seed_pair(client)
    html = client.get("/line-finder?half_life=none&two_player=1&actual=0").get_data(as_text=True)
    assert 'data-half-life="none"' in html
    assert 'data-two-player="1"' in html
    assert 'data-actual="0"' in html
    assert 'data-fitted="1"' in html


def test_page_empty_state_when_a_player_is_missing(client, monkeypatch):
    monkeypatch.setenv("LINE_FINDER_PLAYERS", "A,Nobody")
    create_player(client, "A")
    resp = client.get("/line-finder")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    html = resp.get_data(as_text=True)
    assert "Nobody" in html and "players list" in html
    assert 'id="line-finder"' not in html


def test_page_default_pair_names_when_env_unset(client, monkeypatch):
    monkeypatch.delenv("LINE_FINDER_PLAYERS", raising=False)
    html = client.get("/line-finder").get_data(as_text=True)
    assert lf.DEFAULT_PLAYERS[0] in html and lf.DEFAULT_PLAYERS[1] in html


def test_home_links_to_line_finder(client):
    assert 'href="/line-finder"' in client.get("/").get_data(as_text=True)


def test_data_json_shape(client, pair_env):
    _seed_pair(client)
    resp = client.get("/line-finder/data")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    d = resp.get_json()
    assert d["available"] is True
    assert d["players"] == {"a": "A", "b": "B"}
    assert d["settings"]["half_life"] == 90 and d["settings"]["two_player"] is False
    assert d["n_cups"] == 4
    assert set(d["formats"]) == {"wii", "mk8dx", "mixed"}
    for key in ("lines", "trend", "backtest", "line_history", "cups"):
        assert key in d
    assert d["formats"]["wii"]["rec"] is not None
    assert len(d["formats"]["wii"]["fitted"]) == len(d["lines"])
    assert d["formats"]["mixed"]["actual_source"] == "pairs"
    assert d["line_history"]["stored_line"] == 0


def test_data_two_player_and_unweighted(client, pair_env):
    _seed_pair(client)
    conn = get_connection()
    create_player(client, "C")
    _insert_cup(conn, "2026-09-05 19:00:00", "wii", {1: (60, 0), 2: (40, 9), 3: (10, 0)}, cup_players=[1, 2, 3])
    conn.commit()
    conn.close()
    all_cups = client.get("/line-finder/data?half_life=all").get_json()
    assert all_cups["settings"]["half_life"] is None
    assert all_cups["n_cups"] == 5
    two = client.get("/line-finder/data?half_life=none&two_player=1").get_json()
    assert two["n_cups"] == 4
    assert all(c["n_players"] == 2 for c in two["cups"])


@pytest.mark.parametrize(
    "query",
    [
        "half_life=13",
        "half_life=366",
        "half_life=abc",
        "half_life=1e2",
        "half_life=-90",
        "half_life=99999999999999999999",
        "half_life=%2B14",  # "+14"
        "half_life=1_5",
        "half_life=%2090",  # " 90"
        "half_life=90%20",  # "90 "
        "half_life=%EF%BC%91%EF%BC%94",  # fullwidth digits
        "half_life=None",
        "half_life=ALL",
        "two_player=2",
        "two_player=yes",
        "two_player=%201",
        "actual=maybe",
        "fitted=2",
    ],
)
def test_bad_params_are_400_not_500(client, pair_env, query):
    _seed_pair(client)
    data = client.get("/line-finder/data?" + query)
    assert data.status_code == 400
    assert "error" in data.get_json()
    page = client.get("/line-finder?" + query)
    assert page.status_code == 400


def test_empty_params_fall_back_to_defaults(client, pair_env):
    _seed_pair(client)
    d = client.get("/line-finder/data?half_life=&two_player=").get_json()
    assert d["settings"] == {"half_life": 90, "two_player": False, "today": d["settings"]["today"]}
    html = client.get("/line-finder?actual=&fitted=").get_data(as_text=True)
    assert 'data-actual="1"' in html and 'data-fitted="1"' in html


def test_data_is_never_cached(client, pair_env):
    _seed_pair(client)
    assert client.get("/line-finder/data").headers["Cache-Control"] == "no-store"
    assert client.get("/line-finder/data?half_life=13").headers["Cache-Control"] == "no-store"


def test_data_future_dated_cup_is_200(client, pair_env):
    _seed_pair(client)
    conn = get_connection()
    _insert_cup(conn, "2400-01-01 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, cup_players=[1, 2])
    conn.commit()
    conn.close()
    for hl in (14, 90, "none"):
        resp = client.get(f"/line-finder/data?half_life={hl}")
        assert resp.status_code == 200, hl
        d = resp.get_json()
        assert d["n_cups"] == 5
        assert d["backtest"]["rows"][-1]["date"] == "2400-01-01"
    assert client.get("/line-finder?half_life=14").status_code == 200


def test_data_empty_when_player_missing(client, monkeypatch):
    monkeypatch.setenv("LINE_FINDER_PLAYERS", "A,Nobody")
    create_player(client, "A")
    resp = client.get("/line-finder/data")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    d = resp.get_json()
    assert d["available"] is False
    assert "Nobody" in d["message"]


def test_data_with_no_cups(client, pair_env):
    create_player(client, "A")
    create_player(client, "B")
    d = client.get("/line-finder/data").get_json()
    assert d["available"] is True and d["n_cups"] == 0


def test_data_excludes_non_completed_and_deleted_cups(client, pair_env):
    _seed_pair(client)
    conn = get_connection()
    _insert_cup(conn, "2026-09-06 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, status="cancelled", cup_players=[1, 2])
    _insert_cup(conn, "2026-09-07 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, status="in_progress", cup_players=[1, 2])
    _insert_cup(conn, "2026-09-08 19:00:00", "wii", {1: (60, 0), 2: (40, 9)}, deleted=True, cup_players=[1, 2])
    conn.commit()
    conn.close()
    d = client.get("/line-finder/data").get_json()
    assert d["n_cups"] == 4
    assert {c["id"] for c in d["cups"]} == {1, 2, 3, 4}


def test_routes_are_read_only(client, pair_env):
    _seed_pair(client)
    for path in ("/line-finder", "/line-finder/data"):
        assert client.post(path).status_code == 405


def test_routes_behind_password_gate(client, monkeypatch):
    monkeypatch.setattr(app_module, "APP_PASSWORD", "hunter2")
    monkeypatch.setattr(app_module, "PASSWORD_GATE_ENABLED", True)
    for path in ("/line-finder", "/line-finder/data"):
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
    # And the JSON endpoint stays gated even when asked for JSON.
    resp = client.get("/line-finder/data?half_life=30", headers={"Accept": "application/json"})
    assert resp.status_code == 302


def test_seeded_staging_pair_has_cups_on_every_console(tmp_path, monkeypatch):
    """The compose file points staging's Line Finder at Test Toad + Dummy Diddy;
    the seed must give that pair cups on both consoles, a mixed cup, and some
    2-player cups, or the staging page is an empty state."""
    import os
    import sys

    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import seed_staging

    db_path = str(tmp_path / "km_tracker.staging.db")
    assert seed_staging.main(["--db", db_path, "--reset"]) == 0
    conn = get_connection(db_path)
    try:
        pair = lf.load_pair(conn, ("Test Toad", "Dummy Diddy"))
    finally:
        conn.close()
    assert pair is not None
    editions = {c["game_edition"] for c in pair["cups"]}
    assert editions == {"wii", "mk8dx", "mixed"}
    assert sum(1 for c in pair["cups"] if c["n_players"] == 2) >= 3
    out = lf.compute(pair["cups"], half_life=None, today=date(2026, 9, 18))
    assert all(out["formats"][f]["rec"] is not None for f in ("wii", "mk8dx", "mixed"))
