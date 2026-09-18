"""Line Finder — how many points the second of two players should get at the
start of a cup for it to be a coin flip, per cup format (Wii / Switch / mixed).

Pure math + data shaping. No Flask in here: `compute()` takes plain dicts and
returns a JSON-serialisable dict the page renders from, and `load_pair()` is the
one function that reads the DB (given a connection). Everything is read-only.

Model, in short: each completed cup where both players scored is a sample of
the favourite's margin (raw score minus raw score, never line_score). Per
edition, the recency-weighted PER-RACE edge is mu = sum(w*m) / sum(w*r) over
samples — a pure cup contributes (m, r=4), each block of a mixed cup with a
per-console breakdown contributes (block margin, r=2) to ITS console. The
per-race spread sd = sqrt(sum((m - r*mu)^2) / sum(r)) is unweighted. A format
is then a normal: Wii ~ N(4mu_w, (2sd_w)^2), Switch likewise, and
Mixed ~ N(2mu_w + 2mu_s, 2sd_w^2 + 2sd_s^2). The recommended line is the
rounded model mean; the fitted win % at line L is 1 - Phi((L - mean) / sd);
the actual win % counts real cups (a tie at the line is half a win).
"""

import math
import os
from datetime import date

from maps import (
    BASE_EDITIONS,
    DEFAULT_EDITION,
    MIXED_EDITION,
    edition_label,
    other_edition,
)

# (favourite, receiver): the "line" is always points given to the SECOND name.
DEFAULT_PLAYERS = ("Naked Graham", "EVDV")
# Override as "Name A,Name B" — exists so staging can point at two seeded
# players. The page has no player picker on purpose.
PLAYERS_ENV = "LINE_FINDER_PLAYERS"

DEFAULT_HALF_LIFE = 90
MIN_HALF_LIFE = 14
MAX_HALF_LIFE = 365

LINE_MIN = -10
LINE_MAX = 35
# Half-step grid so the curves (and the pairs proxy, whose margins can be
# half-integers) render smoothly; the tables use the integer subset.
LINES = [x / 2 for x in range(LINE_MIN * 2, LINE_MAX * 2 + 1)]

# Mirror app.MAX_RACES / app.RACES_PER_BLOCK (pinned by a test) without
# importing the Flask app.
RACES_PER_CUP = 4
RACES_PER_BLOCK = 2

# The Wii x Switch pairs proxy is O(n_wii * n_switch); cap each side at the
# most recent PAIRS_CAP cups so a long history can't blow up the response.
PAIRS_CAP = 100

# Per-race sd needs at least this many samples of an edition.
MIN_SD_SAMPLES = 2
# Backtest: prior samples of each needed edition before a recommendation.
BACKTEST_MIN_SAMPLES = 3
RECENT_DAYS = 90

FORMATS = ("wii", "mk8dx", "mixed")
# Which editions (and how many races of each) make up a cup of each format.
FORMAT_RACES = {
    "wii": {"wii": RACES_PER_CUP},
    "mk8dx": {"mk8dx": RACES_PER_CUP},
    "mixed": {"wii": RACES_PER_BLOCK, "mk8dx": RACES_PER_BLOCK},
}
FORMAT_LABELS = {"wii": "Wii", "mk8dx": "Switch", "mixed": "Mixed"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def line_finder_players(env=None):
    """The (favourite, receiver) name pair. LINE_FINDER_PLAYERS="A,B" overrides
    the default; anything malformed (not exactly two distinct non-empty names)
    falls back to the default rather than half-applying."""
    env = os.environ if env is None else env
    raw = env.get(PLAYERS_ENV) or ""
    names = [n.strip() for n in raw.split(",")]
    names = [n for n in names if n]
    if len(names) == 2 and names[0] != names[1]:
        return (names[0], names[1])
    return DEFAULT_PLAYERS


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def weight(age_days, half_life):
    """Recency weight 0.5 ** (age / half_life); half_life None = unweighted.

    A future-dated cup (negative age) is clamped to age 0 -> weight 1: it
    counts as "today", never as a weight above 1 (which would also overflow
    for a far-future date). Very old cups underflow to exactly 0.0 — every
    consumer treats a zero weight-sum as "no usable samples" rather than
    dividing by it.
    """
    if half_life is None:
        return 1.0
    return 0.5 ** (max(age_days, 0) / half_life)


def phi(x):
    """Standard normal CDF."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fitted_pct(mean, sd, line):
    """Model win % for the favourite at `line`. A zero spread degenerates to a
    step at the mean rather than dividing by zero."""
    if sd <= 0:
        if line < mean:
            return 100.0
        return 0.0 if line > mean else 50.0
    return (1 - phi((line - mean) / sd)) * 100


def actual_curve(margins, lines=LINES):
    """(pcts, wins, ties) per line: share of cups the favourite wins with the
    line applied, a tie at the line counting half. All None when no cups."""
    n = len(margins)
    if n == 0:
        return None, None, None
    pcts, wins, ties = [], [], []
    for line in lines:
        w = sum(1 for m in margins if m > line)
        t = sum(1 for m in margins if m == line)
        pcts.append((w + 0.5 * t) / n * 100)
        wins.append(w)
        ties.append(t)
    return pcts, wins, ties


def recommend(mean):
    """Recommended line = the model mean rounded half away from zero."""
    return int(math.floor(abs(mean) + 0.5)) * (1 if mean >= 0 else -1)


def recommend_line(model):
    """(recommended line, within_noise) for a format model.

    The model mean rounded half away from zero — unless the mean is smaller
    than its own standard error (|mean| < se), in which case the edge is not
    distinguishable from zero and the recommendation is "even" (0), flagged
    so the tile can say why. Every consumer (tiles, chart markers, shaded
    table rows, the backtest walk) goes through this one function.
    """
    if abs(model["mean"]) < model["se"]:
        return 0, True
    return recommend(model["mean"]), False


def half_line(mean, rec):
    """The nearest half-point line to the mean — the tie-proof option."""
    return rec - 0.5 if mean < rec else rec + 0.5


def fmt_line(value):
    """+18 / even / -4."""
    if isinstance(value, float) and not value.is_integer():
        whole = int(math.floor(abs(value)))
        sign = "-" if value < 0 else "+"
        return f"{sign}{whole or ''}½"
    n = int(value)
    if n == 0:
        return "even"
    return f"+{n}" if n > 0 else str(n)


def fmt_signed(value, places=1):
    if value is None:
        return "n/a"
    return f"{value:+.{places}f}"


def cup_date(cup):
    return date.fromisoformat(str(cup["date"])[:10])


def _sort_key(cup):
    return (str(cup["date"]), cup["id"])


def margin_of(cup):
    return cup["a_score"] - cup["b_score"]


def outcome(margin, line):
    """Who wins the cup once `line` is handed to the receiver."""
    if margin > line:
        return "a"
    if margin < line:
        return "b"
    return "tie"


def _has_blocks(cup):
    return all(
        cup.get(k) is not None for k in ("a_block1", "a_block2", "b_block1", "b_block2")
    )


def _block_editions(first_edition):
    """(block 1 console, block 2 console). A NULL/garbage first_edition falls
    back to DEFAULT_EDITION — the same deterministic fallback as
    app.edition_for_race, so this module never disagrees with the race page."""
    first = first_edition if first_edition in BASE_EDITIONS else DEFAULT_EDITION
    return first, other_edition(first)


def _r(x, places=3):
    return None if x is None else round(x, places)


# ---------------------------------------------------------------------------
# Per-edition pooling and the format models
# ---------------------------------------------------------------------------


def edition_samples(cups, half_life, today):
    """{edition: [(margin, races, weight), ...]}. A pure cup is one sample of
    its edition (4 races). A mixed cup with a per-console breakdown for BOTH
    players is two samples of 2 races, one per console; a mixed cup entered as
    a plain total contributes nothing here (it still counts for the mixed
    actual curve)."""
    samples = {e: [] for e in BASE_EDITIONS}
    for cup in cups:
        w = weight((today - cup_date(cup)).days, half_life)
        edition = cup["game_edition"]
        if edition in BASE_EDITIONS:
            samples[edition].append((margin_of(cup), RACES_PER_CUP, w))
        elif edition == MIXED_EDITION and _has_blocks(cup):
            first, second = _block_editions(cup.get("first_edition"))
            samples[first].append(
                (cup["a_block1"] - cup["b_block1"], RACES_PER_BLOCK, w)
            )
            samples[second].append(
                (cup["a_block2"] - cup["b_block2"], RACES_PER_BLOCK, w)
            )
    return samples


def edition_stats(samples):
    """Per-race edge (recency-weighted) and per-race sd (unweighted) for one
    edition's samples. sd is None below MIN_SD_SAMPLES."""
    n = len(samples)
    if n == 0:
        return {"n_samples": 0, "races": 0, "mu": None, "sd": None}
    races = sum(r for _, r, _ in samples)
    weighted_races = sum(w * r for _, r, w in samples)
    if weighted_races <= 0:
        # Every sample's weight underflowed to 0 (all far older than the
        # half-life allows, e.g. when weighting from a far-future cup date).
        # No usable evidence -> same as having no samples.
        return {"n_samples": n, "races": races, "mu": None, "sd": None}
    mu = sum(w * m for m, _, w in samples) / weighted_races
    sd = None
    if n >= MIN_SD_SAMPLES:
        sd = math.sqrt(sum((m - r * mu) ** 2 for m, r, _ in samples) / races)
    return {"n_samples": n, "races": races, "mu": mu, "sd": sd}


def format_model(fmt, stats):
    """Normal model for a cup of `fmt` from per-edition stats, or None when any
    edition it needs has no sd yet. `se` is the standard error of the model
    mean — sd_cup / sqrt(n) for a pure format, and its two-edition analogue
    (each console's share added in quadrature) for mixed."""
    mean = 0.0
    var = 0.0
    se_var = 0.0
    for edition, races in FORMAT_RACES[fmt].items():
        st = stats[edition]
        if st["sd"] is None:
            return None
        mean += races * st["mu"]
        var += races * st["sd"] ** 2
        se_var += (races * st["sd"]) ** 2 / st["races"]
    return {"mean": mean, "sd": math.sqrt(var), "se": math.sqrt(se_var)}


def format_margins(cups, fmt):
    return [margin_of(c) for c in cups if c["game_edition"] == fmt]


def pairs_proxy_margins(wii_margins, switch_margins, cap=PAIRS_CAP):
    """Every Wii cup x every Switch cup, half of each standing in for its two
    races. Empty when either list is empty. Inputs are chronological; only the
    most recent `cap` cups per console are paired. Returns (margins, capped)."""
    capped = len(wii_margins) > cap or len(switch_margins) > cap
    wii_margins = wii_margins[-cap:]
    switch_margins = switch_margins[-cap:]
    return [w / 2 + s / 2 for w in wii_margins for s in switch_margins], capped


def _weighted_mean(cups, half_life, today):
    if not cups:
        return None
    ws = [weight((today - cup_date(c)).days, half_life) for c in cups]
    total = sum(ws)
    if total <= 0:
        return None
    return sum(w * margin_of(c) for w, c in zip(ws, cups)) / total


def _mean(values):
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# Backtest, trend, history
# ---------------------------------------------------------------------------


def _recommend_from(prior, fmt, half_life, as_of):
    """Recommended line for a `fmt` cup using only `prior` cups, or None when
    any needed edition has fewer than BACKTEST_MIN_SAMPLES prior samples."""
    samples = edition_samples(prior, half_life, as_of)
    if any(len(samples[e]) < BACKTEST_MIN_SAMPLES for e in FORMAT_RACES[fmt]):
        return None
    stats = {e: edition_stats(samples[e]) for e in BASE_EDITIONS}
    model = format_model(fmt, stats)
    return None if model is None else recommend_line(model)[0]


def backtest(cups, half_life):
    """Walk the cups chronologically; for each, recommend a line from the cups
    BEFORE it (same settings) and record who would have won under that line
    and under the line actually used. Weights are taken relative to the cup's
    own date — which is what would have been computed that night."""
    ordered = sorted(cups, key=_sort_key)
    rows = []
    summary = {
        f: {
            "n": 0,
            "skipped": 0,
            "rec": {"a": 0, "b": 0, "tie": 0},
            "used": {"a": 0, "b": 0, "tie": 0},
        }
        for f in FORMATS
    }
    for i, cup in enumerate(ordered):
        fmt = cup["game_edition"]
        margin = margin_of(cup)
        used = cup["b_line"]
        rec = _recommend_from(ordered[:i], fmt, half_life, cup_date(cup))
        row = {
            "cup_id": cup["id"],
            "date": str(cup["date"])[:10],
            "format": fmt,
            "margin": margin,
            "rec_line": rec,
            "rec_outcome": None if rec is None else outcome(margin, rec),
            "line_used": used,
            "used_outcome": outcome(margin, used),
        }
        rows.append(row)
        s = summary[fmt]
        if rec is None:
            s["skipped"] += 1
        else:
            s["n"] += 1
            s["rec"][row["rec_outcome"]] += 1
            s["used"][row["used_outcome"]] += 1
    return {"rows": rows, "summary": summary}


def margin_trend(cups, half_life):
    """Each cup's margin by date, plus the recency-weighted running mean for
    each pure edition (weights relative to the cup being drawn)."""
    ordered = sorted(cups, key=_sort_key)
    points = [
        {
            "cup_id": c["id"],
            "date": str(c["date"])[:10],
            "format": c["game_edition"],
            "margin": margin_of(c),
            "n_players": c["n_players"],
        }
        for c in ordered
    ]
    running = {}
    for edition in BASE_EDITIONS:
        series = []
        seen = []
        for c in ordered:
            if c["game_edition"] != edition:
                continue
            seen.append(c)
            series.append(
                {
                    "date": str(c["date"])[:10],
                    "value": _r(_weighted_mean(seen, half_life, cup_date(c))),
                }
            )
        running[edition] = series
    return {"points": points, "running": running}


def line_history(cups, line_changes, stored_line):
    """The receiver's line over time: every cup's line actually used, merged
    with the before/after of each recorded line change."""
    changes_by_cup = {lc["cup_id"]: lc for lc in line_changes}
    rows = []
    for c in sorted(cups, key=_sort_key):
        lc = changes_by_cup.get(c["id"])
        rows.append(
            {
                "cup_id": c["id"],
                "date": str(c["date"])[:10],
                "format": c["game_edition"],
                "line_used": c["b_line"],
                "line_before": None if lc is None else lc["line_before"],
                "line_after": None if lc is None else lc["line_after"],
            }
        )
    return {
        "rows": rows,
        "changes": [
            {
                "cup_id": lc["cup_id"],
                "date": str(lc["date"])[:10],
                "line_before": lc["line_before"],
                "line_after": lc["line_after"],
            }
            for lc in line_changes
        ],
        "stored_line": stored_line,
    }


# ---------------------------------------------------------------------------
# The one entry point the route calls
# ---------------------------------------------------------------------------


def _tile_notes(fmt, a, b, cups, all_cups, fm, stats, half_life, today, stored_line, extra):
    """Plain-text notes under a tile. Built here (not in JS) so they are
    testable and the page stays a dumb renderer."""
    label = FORMAT_LABELS[fmt]
    notes = []
    if fm["rec"] is None:
        notes.append(
            f"Not enough {label} cups to fit a line yet — each console needs at "
            f"least {MIN_SD_SAMPLES} samples."
        )
    else:
        notes.append(
            f"{a} wins about {round(fm['fitted_at_rec'])}% of {label} cups at "
            f"{fmt_line(fm['rec'])} (fitted, ±{fm['se']:.1f}). "
            f"Use {fmt_line(fm['half_line'])} to rule out ties."
        )
        if fm["rec_within_noise"]:
            notes.append(
                f"Fitted {fmt_signed(fm['model']['mean'])} ± {fm['se']:.1f} — not "
                f"distinguishable from even, so play it straight."
            )
    own = [c for c in cups if c["game_edition"] == fmt]
    if fmt in BASE_EDITIONS and own:
        margins = [margin_of(c) for c in own]
        recent = [
            margin_of(c) for c in own if (today - cup_date(c)).days <= RECENT_DAYS
        ]
        recent_txt = (
            f"last {RECENT_DAYS} days {fmt_signed(_mean(recent))} ({len(recent)} cups)"
            if recent
            else f"none in the last {RECENT_DAYS} days"
        )
        notes.append(
            f"Margin: all-time {fmt_signed(_mean(margins))} · recency-weighted "
            f"{fmt_signed(_weighted_mean(own, half_life, today))} · {recent_txt}."
        )
    if fmt == "mixed":
        mu_w, mu_s = stats["wii"]["mu"], stats["mk8dx"]["mu"]
        if mu_w is not None and mu_s is not None:
            notes.append(
                f"Two Wii races at {fmt_signed(mu_w)} each, two Switch at "
                f"{fmt_signed(mu_s)}."
            )
        if extra["n_real"]:
            notes.append(
                f"{extra['n_real']} real mixed cup{'s' if extra['n_real'] != 1 else ''} "
                f"drive the actual curve (the Wii×Switch pairs proxy would be "
                f"{extra['n_pairs']} combos)."
            )
        elif extra["n_pairs"]:
            notes.append(
                f"No completed mixed cup yet — the actual curve is built from "
                f"{extra['n_wii']}×{extra['n_switch']} = {extra['n_pairs']} "
                f"Wii×Switch pairs"
                + (f" (the most recent {PAIRS_CAP} cups per console)." if extra["pairs_capped"] else ".")
            )
        else:
            notes.append("No completed mixed cup yet, and no Wii×Switch pairs to stand in.")
    # The 2-player-only read, from the unfiltered cups.
    two = [c for c in all_cups if c["n_players"] == 2]
    if fmt in BASE_EDITIONS:
        two_own = [margin_of(c) for c in two if c["game_edition"] == fmt]
        if two_own:
            notes.append(
                f"Just the two of them: {len(two_own)} {label} cup"
                f"{'s' if len(two_own) != 1 else ''}, average "
                f"{fmt_signed(_mean(two_own))}."
            )
        else:
            notes.append(f"No 2-player {label} cups yet.")
    else:
        two_w = _mean([margin_of(c) for c in two if c["game_edition"] == "wii"])
        two_s = _mean([margin_of(c) for c in two if c["game_edition"] == "mk8dx"])
        if two_w is not None and two_s is not None:
            notes.append(
                f"Just the two of them: about {fmt_line(recommend(two_w / 2 + two_s / 2))} "
                f"from the 2-player Wii and Switch halves."
            )
    if fmt == "wii" and stored_line is not None and fm["actual"] is not None:
        idx = LINES.index(float(stored_line)) if float(stored_line) in LINES else None
        at = f" → {a} wins ~{round(fm['actual'][idx])}% of real Wii cups at it" if idx is not None else ""
        notes.append(f"The app's stored line for {b} is {fmt_line(stored_line)} today{at}.")
    return notes


def compute(
    cups,
    *,
    half_life=DEFAULT_HALF_LIFE,
    two_player=False,
    today=None,
    players=DEFAULT_PLAYERS,
    line_changes=(),
    stored_line=None,
):
    """Everything the page renders, as one JSON-serialisable dict.

    `cups`: dicts with id, date ('YYYY-MM-DD…'), game_edition, first_edition,
    n_players, a_score, b_score, a_block1, a_block2, b_block1, b_block2,
    b_line — see load_pair(). `half_life` in days or None for unweighted.
    """
    today = today or date.today()
    a, b = players
    all_cups = sorted((c for c in cups if c["game_edition"] in FORMATS), key=_sort_key)
    cups = [c for c in all_cups if c["n_players"] == 2] if two_player else all_cups

    samples = edition_samples(cups, half_life, today)
    stats = {e: edition_stats(samples[e]) for e in BASE_EDITIONS}

    margins = {f: format_margins(cups, f) for f in FORMATS}
    pairs, pairs_capped = pairs_proxy_margins(margins["wii"], margins["mk8dx"])
    n_pairs = len(pairs)

    formats = {}
    for fmt in FORMATS:
        model = format_model(fmt, stats)
        real = margins[fmt]
        source = None
        curve_margins = real
        if fmt == "mixed" and not real:
            curve_margins = pairs
            source = "pairs" if curve_margins else None
        elif real:
            source = "real"
        actual, wins, ties = actual_curve(curve_margins)
        fitted = None if model is None else [fitted_pct(model["mean"], model["sd"], L) for L in LINES]
        rec, within_noise = (None, False) if model is None else recommend_line(model)
        fm = {
            "label": FORMAT_LABELS[fmt],
            "n": len(real),
            "actual": None if actual is None else [_r(v, 2) for v in actual],
            "actual_wins": wins,
            "actual_ties": ties,
            "actual_n": len(curve_margins),
            "actual_source": source,
            "pairs_capped": pairs_capped if fmt == "mixed" else False,
            "fitted": None if fitted is None else [_r(v, 2) for v in fitted],
            "model": None
            if model is None
            else {"mean": _r(model["mean"]), "sd": _r(model["sd"])},
            "rec": rec,
            "rec_within_noise": within_noise,
            "se": None if model is None else _r(model["se"], 2),
            "half_line": None if rec is None else half_line(model["mean"], rec),
            "fitted_at_rec": None if rec is None else _r(fitted_pct(model["mean"], model["sd"], rec), 2),
            "margins": real,
            "dates": [str(c["date"])[:10] for c in cups if c["game_edition"] == fmt],
            "n_players": [c["n_players"] for c in cups if c["game_edition"] == fmt],
        }
        extra = {
            "n_real": len(real),
            "n_pairs": n_pairs,
            "n_wii": min(len(margins["wii"]), PAIRS_CAP),
            "n_switch": min(len(margins["mk8dx"]), PAIRS_CAP),
            "pairs_capped": pairs_capped,
        }
        fm["notes"] = _tile_notes(
            fmt, a, b, cups, all_cups, fm, stats, half_life, today, stored_line, extra
        )
        formats[fmt] = fm

    cup_rows = [
        {
            "id": c["id"],
            "date": str(c["date"]),
            "format": c["game_edition"],
            "format_label": _format_label(c),
            "n_players": c["n_players"],
            "a_score": c["a_score"],
            "b_score": c["b_score"],
            "margin": margin_of(c),
            "line_used": c["b_line"],
        }
        for c in sorted(cups, key=_sort_key, reverse=True)
    ]

    return {
        "available": True,
        "players": {"a": a, "b": b},
        "settings": {
            "half_life": half_life,
            "two_player": bool(two_player),
            "today": today.isoformat(),
        },
        "n_cups": len(cups),
        "n_all_cups": len(all_cups),
        "lines": LINES,
        "editions": {
            e: {
                "label": edition_label(e),
                "n_samples": stats[e]["n_samples"],
                "races": stats[e]["races"],
                "mu": _r(stats[e]["mu"]),
                "sd": _r(stats[e]["sd"]),
            }
            for e in BASE_EDITIONS
        },
        "formats": formats,
        "trend": margin_trend(cups, half_life),
        "backtest": backtest(cups, half_life),
        "line_history": line_history(cups, line_changes, stored_line),
        "cups": cup_rows,
        "stored_line": stored_line,
    }


def _format_label(cup):
    if cup["game_edition"] == MIXED_EDITION:
        first, second = _block_editions(cup.get("first_edition"))
        return f"{edition_label(first)} → {edition_label(second)}"
    return FORMAT_LABELS[cup["game_edition"]]


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def load_pair(conn, players=DEFAULT_PLAYERS):
    """Read everything compute() needs for the (favourite, receiver) pair.

    Returns a dict {players, ids, cups, line_changes, stored_line}, or None
    when either name has no players row (the page renders an empty state).
    Cups: status='completed', not soft-deleted, BOTH players have a scores
    row. n_players is the cup_players count, falling back to the scores count
    for cups entered by hand (POST /cups writes no cup_players rows).
    """
    ids = []
    for name in players:
        row = conn.execute("SELECT id, line FROM players WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        ids.append(row)
    a_id, b_id = ids[0]["id"], ids[1]["id"]
    rows = conn.execute(
        """
        SELECT c.id, c.date, c.game_edition, c.first_edition,
               sa.score AS a_score, sb.score AS b_score,
               sa.block1_score AS a_block1, sa.block2_score AS a_block2,
               sb.block1_score AS b_block1, sb.block2_score AS b_block2,
               sb.line AS b_line,
               (SELECT COUNT(*) FROM cup_players cp WHERE cp.cup_id = c.id) AS n_cup_players,
               (SELECT COUNT(*) FROM scores s WHERE s.cup_id = c.id) AS n_scores
        FROM cups c
        JOIN scores sa ON sa.cup_id = c.id AND sa.player_id = ?
        JOIN scores sb ON sb.cup_id = c.id AND sb.player_id = ?
        WHERE c.status = 'completed' AND c.deleted_at IS NULL
        ORDER BY c.date, c.id
        """,
        (a_id, b_id),
    ).fetchall()
    cups = []
    for r in rows:
        cups.append(
            {
                "id": r["id"],
                "date": r["date"],
                "game_edition": r["game_edition"],
                "first_edition": r["first_edition"],
                "n_players": r["n_cup_players"] or r["n_scores"],
                "a_score": r["a_score"],
                "b_score": r["b_score"],
                "a_block1": r["a_block1"],
                "a_block2": r["a_block2"],
                "b_block1": r["b_block1"],
                "b_block2": r["b_block2"],
                "b_line": r["b_line"],
            }
        )
    changes = conn.execute(
        """
        SELECT lc.cup_id, c.date, lc.line_before, lc.line_after
        FROM line_changes lc JOIN cups c ON c.id = lc.cup_id
        WHERE lc.player_id = ?
        ORDER BY c.date, lc.id
        """,
        (b_id,),
    ).fetchall()
    return {
        "players": tuple(players),
        "ids": (a_id, b_id),
        "cups": cups,
        "line_changes": [dict(lc) for lc in changes],
        "stored_line": ids[1]["line"],
    }
