"""Mixed-edition cups: 2 races on one console, then 2 on the other.

A mixed cup stores game_edition='mixed' plus first_edition (the coin-flip
winner). The per-race edition is DERIVED from those two columns — races
1..RACES_PER_BLOCK on first_edition, the rest on the other console — so the 2-2
block split has no writable state that could drift.

House rule (Graham): mixed cups are LINELESS. The line handicap on a mixed cup
is worked out manually, so the app must never write line_changes or touch
players.line for one.
"""

import json

import pytest

import app as appmod
from app import MAX_RACES, RACES_PER_BLOCK, edition_for_race
from db import get_connection
from helpers import create_player
from maps import MIXED_EDITION, TRACK_SETS, courses_for, other_edition
from test_cup_session import _create_session, _setup_players, _submit_scores

# Course names that exist in BOTH track lists. Playing one in the first half
# must NOT remove the (different) same-named course from the second half's pool.
OVERLAPPING_COURSES = sorted(set(TRACK_SETS["wii"]) & set(TRACK_SETS["mk8dx"]))


def _fix_flip(monkeypatch, edition):
    """Pin the console coin flip so tests are deterministic. Patching the named
    helper (not random.choice) keeps every other random draw untouched."""
    monkeypatch.setattr(appmod, "flip_first_console", lambda: edition)


def _record_race(client, cup_id, map_name):
    return client.post(f"/cup-session/{cup_id}/next-race", json={"map": map_name})


def _play_half(client, cup_id, edition, count=RACES_PER_BLOCK, offset=0):
    """Record `count` races using real courses from `edition`'s track list."""
    maps = TRACK_SETS[edition][offset : offset + count]
    for m in maps:
        _record_race(client, cup_id, m)
    return maps


def _races(cup_id=1):
    conn = get_connection()
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT race_number, map FROM races WHERE cup_id = ? ORDER BY race_number",
            (cup_id,),
        )
    ]
    conn.close()
    return rows


def _cup_row(cup_id=1):
    conn = get_connection()
    row = conn.execute(
        "SELECT status, game_edition, first_edition FROM cups WHERE id = ?", (cup_id,)
    ).fetchone()
    conn.close()
    return row


# =============================================================================
# The block invariant (2-2, never alternating, never 3-1)
# =============================================================================


@pytest.mark.parametrize(
    "first_edition,expected",
    [
        ("wii", ["wii", "wii", "mk8dx", "mk8dx"]),
        ("mk8dx", ["mk8dx", "mk8dx", "wii", "wii"]),
    ],
)
def test_edition_for_race_is_two_blocks(first_edition, expected):
    got = [
        edition_for_race(MIXED_EDITION, first_edition, n)
        for n in range(1, MAX_RACES + 1)
    ]
    assert got == expected
    # Pin the 2-2 rule itself, not just this MAX_RACES value.
    assert got.count(first_edition) == RACES_PER_BLOCK
    assert got.count(other_edition(first_edition)) == MAX_RACES - RACES_PER_BLOCK
    # Exactly one console swap.
    assert sum(1 for a, b in zip(got, got[1:]) if a != b) == 1


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_edition_for_race_on_pure_cup_is_always_the_cup_edition(edition):
    for n in range(1, MAX_RACES + 1):
        assert edition_for_race(edition, None, n) == edition


def test_edition_for_race_falls_back_when_first_edition_missing():
    # A hand-edited 'mixed' row with a NULL/garbage first_edition must resolve
    # deterministically rather than re-flipping or blowing up.
    for bad in (None, "", "nintendo64"):
        got = [
            edition_for_race(MIXED_EDITION, bad, n) for n in range(1, MAX_RACES + 1)
        ]
        assert got == ["wii", "wii", "mk8dx", "mk8dx"]


# =============================================================================
# row_value: a missing column must be LOUD, never a silent wrong edition
# =============================================================================


def test_row_value_reads_a_present_column(client):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    conn = get_connection()
    row = conn.execute(
        "SELECT game_edition, first_edition FROM cups WHERE id = 1"
    ).fetchone()
    conn.close()
    assert appmod.row_value(row, "first_edition") in ("wii", "mk8dx")


def test_row_value_raises_on_a_missing_column(client):
    # A route that forgets to SELECT first_edition must NOT quietly get None:
    # None -> edition_for_race -> DEFAULT_EDITION would render and validate a
    # Switch-first mixed cup as Wii-first, silently, with no error anywhere.
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    conn = get_connection()
    short_row = conn.execute("SELECT game_edition FROM cups WHERE id = 1").fetchone()
    conn.close()
    with pytest.raises(KeyError):
        appmod.row_value(short_row, "first_edition")


def test_row_value_returns_an_explicit_default(client):
    conn = get_connection()
    row = conn.execute("SELECT 1 AS present").fetchone()
    conn.close()
    assert appmod.row_value(row, "absent", None) is None
    assert appmod.row_value(row, "absent", "fallback") == "fallback"
    assert appmod.row_value(None, "anything", "fallback") == "fallback"


def test_row_value_raises_for_a_missing_row_with_no_default():
    with pytest.raises(KeyError):
        appmod.row_value(None, "first_edition")


def test_cup_edition_label_filter_raises_on_a_short_select(client, monkeypatch):
    # The filter is registered globally, so any template could hand it a row
    # that omits first_edition. It must fail loudly rather than label a
    # Switch-first mixed cup "Wii → Switch".
    _fix_flip(monkeypatch, "mk8dx")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    conn = get_connection()
    short_row = conn.execute("SELECT game_edition FROM cups WHERE id = 1").fetchone()
    full_row = conn.execute(
        "SELECT game_edition, first_edition FROM cups WHERE id = 1"
    ).fetchone()
    conn.close()

    with pytest.raises(KeyError):
        appmod.cup_edition_label_filter(short_row)
    # The correctly-SELECTed row still labels the real console order.
    assert appmod.cup_edition_label_filter(full_row) == "Switch → Wii"


def test_courses_for_rejects_the_mixed_cup_edition():
    # The old silent fallback returned the Wii list for anything unknown, which
    # would have let a Switch race record a Wii course.
    with pytest.raises(ValueError):
        courses_for(MIXED_EDITION)


def test_mixed_is_not_a_playable_race_edition():
    assert MIXED_EDITION not in TRACK_SETS


# =============================================================================
# Creation + the coin flip
# =============================================================================


def test_mixed_session_stores_edition_and_first_edition(client):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    row = _cup_row()
    assert row["game_edition"] == MIXED_EDITION
    assert row["first_edition"] in ("wii", "mk8dx")


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_pure_cups_leave_first_edition_null(client, edition):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=edition)
    row = _cup_row()
    assert row["game_edition"] == edition
    assert row["first_edition"] is None


def test_invalid_edition_still_falls_back_to_wii(client):
    # Widening the whitelist to include 'mixed' must not let junk through.
    _setup_players(client)
    _create_session(client, ["1", "2"], edition="not-a-real-edition")
    row = _cup_row()
    assert row["game_edition"] == "wii"
    assert row["first_edition"] is None


def test_first_console_never_rerolls(client, monkeypatch):
    # The flip happens ONCE at creation. Loading the page again, spinning, and
    # recording a race must all leave cups.first_edition untouched — a refresh
    # can never change which console goes first.
    _fix_flip(monkeypatch, "mk8dx")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    assert _cup_row()["first_edition"] == "mk8dx"

    # Any later call to the flip helper would be a bug — make it explode.
    def _boom():
        raise AssertionError("first console was re-flipped after cup creation")

    monkeypatch.setattr(appmod, "flip_first_console", _boom)

    client.get("/cup-session/1")
    client.get("/cup-session/1")
    client.post("/cup-session/1/spin")
    _record_race(client, 1, TRACK_SETS["mk8dx"][0])
    client.get("/cup-session/1")

    assert _cup_row()["first_edition"] == "mk8dx"


# =============================================================================
# Per-race wheel (spin)
# =============================================================================


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_spin_draws_from_the_first_console_in_the_first_half(
    client, monkeypatch, first_edition
):
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    data = json.loads(client.post("/cup-session/1/spin").get_data(as_text=True))
    assert data["edition"] == first_edition
    assert data["map"] in TRACK_SETS[first_edition]
    assert TRACK_SETS[first_edition][data["index"]] == data["map"]


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_spin_draws_from_the_second_console_after_the_swap(
    client, monkeypatch, first_edition
):
    second = other_edition(first_edition)
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, first_edition)

    data = json.loads(client.post("/cup-session/1/spin").get_data(as_text=True))
    assert data["edition"] == second
    assert data["map"] in TRACK_SETS[second]
    assert TRACK_SETS[second][data["index"]] == data["map"]


def test_pure_cup_spin_still_reports_its_own_edition(client):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition="mk8dx")
    data = json.loads(client.post("/cup-session/1/spin").get_data(as_text=True))
    assert data["edition"] == "mk8dx"
    assert TRACK_SETS["mk8dx"][data["index"]] == data["map"]


def test_race_page_wheel_switches_track_list_at_the_swap(client, monkeypatch):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    page = client.get("/cup-session/1").get_data(as_text=True)
    assert '"Luigi Circuit"' in page          # Wii-only
    assert '"Ninja Hideaway"' not in page     # MK8DX-only

    _play_half(client, 1, "wii")

    page = client.get("/cup-session/1").get_data(as_text=True)
    assert '"Ninja Hideaway"' in page
    assert '"Luigi Circuit"' not in page


# =============================================================================
# Per-race course validation — door 1: record race
# =============================================================================


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_record_race_rejects_an_off_half_course_in_the_first_half(
    client, monkeypatch, first_edition
):
    second = other_edition(first_edition)
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    # A course that exists ONLY on the other console.
    off_half = next(
        c for c in TRACK_SETS[second] if c not in TRACK_SETS[first_edition]
    )
    resp = _record_race(client, 1, off_half)
    assert resp.status_code == 400
    assert _races() == []


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_record_race_rejects_an_off_half_course_in_the_second_half(
    client, monkeypatch, first_edition
):
    second = other_edition(first_edition)
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    played = _play_half(client, 1, first_edition)

    # Race 3 is on the OTHER console now — a first-console-only course must be
    # refused even though it was legal two races ago.
    off_half = next(
        c for c in TRACK_SETS[first_edition] if c not in TRACK_SETS[second]
    )
    resp = _record_race(client, 1, off_half)
    assert resp.status_code == 400
    assert [m for _, m in _races()] == played  # nothing new written


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_record_race_accepts_the_correct_console_on_both_halves(
    client, monkeypatch, first_edition
):
    second = other_edition(first_edition)
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    first_maps = _play_half(client, 1, first_edition)
    second_maps = _play_half(client, 1, second)

    rows = _races()
    assert [m for _, m in rows] == first_maps + second_maps
    assert len(rows) == MAX_RACES


def test_overlapping_course_is_playable_in_both_halves(client, monkeypatch):
    # 7 names exist in both track lists. Playing Wii Rainbow Road in the first
    # half must leave (the different) MK8DX Rainbow Road available in the second
    # — and must not grey its slice on the second-half wheel.
    shared = "Rainbow Road"
    assert shared in OVERLAPPING_COURSES
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    _record_race(client, 1, shared)                    # race 1, Wii
    _record_race(client, 1, TRACK_SETS["wii"][0])      # race 2, Wii

    # The second half's wheel must show nothing greyed out yet.
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert "var PLAYED_MAPS = [];" in page

    resp = _record_race(client, 1, shared)             # race 3, MK8DX
    assert resp.status_code == 200

    rows = _races()
    assert rows[0] == (1, shared)
    assert rows[2] == (3, shared)  # both persisted


def test_played_maps_are_greyed_per_console(client, monkeypatch):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _record_race(client, 1, "Rainbow Road")

    # Still in the Wii half — the Wii course just played IS excluded.
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert '"Rainbow Road"' in page.split("var PLAYED_MAPS = ")[1].split(";")[0]


# =============================================================================
# Per-race course validation — door 2: the completion-form map override
# =============================================================================


def _complete_form(player_ids, scores, lines=None, **extra):
    data = {
        "notes": "",
        "tz_offset": "",
        "player_ids[]": player_ids,
        "scores[]": scores,
        "lines[]": lines or ["0"] * len(player_ids),
    }
    data.update(extra)
    return data


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_completion_override_rejects_an_off_half_course_first_half(
    client, monkeypatch, first_edition
):
    second = other_edition(first_edition)
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    first_maps = _play_half(client, 1, first_edition)
    _play_half(client, 1, second)

    off_half = next(
        c for c in TRACK_SETS[second] if c not in TRACK_SETS[first_edition]
    )
    client.post(
        "/cup-session/1/complete",
        data=_complete_form(["1", "2"], ["100", "80"], race_1=off_half),
        follow_redirects=True,
    )
    assert _cup_row()["status"] == "in_progress"  # nothing written
    assert _races()[0] == (1, first_maps[0])      # map unchanged


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_completion_override_rejects_an_off_half_course_second_half(
    client, monkeypatch, first_edition
):
    second = other_edition(first_edition)
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, first_edition)
    second_maps = _play_half(client, 1, second)

    off_half = next(
        c for c in TRACK_SETS[first_edition] if c not in TRACK_SETS[second]
    )
    client.post(
        "/cup-session/1/complete",
        data=_complete_form(["1", "2"], ["100", "80"], race_3=off_half),
        follow_redirects=True,
    )
    assert _cup_row()["status"] == "in_progress"
    assert _races()[2] == (3, second_maps[0])


def test_completion_override_accepts_a_same_console_course(client, monkeypatch):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "wii")
    _play_half(client, 1, "mk8dx")

    new_first = TRACK_SETS["wii"][5]
    new_third = TRACK_SETS["mk8dx"][40]
    client.post(
        "/cup-session/1/complete",
        data=_complete_form(
            ["1", "2"], ["100", "80"], race_1=new_first, race_3=new_third
        ),
        follow_redirects=True,
    )
    assert _cup_row()["status"] == "completed"
    rows = dict((n, m) for n, m in _races())
    assert rows[1] == new_first
    assert rows[3] == new_third


# =============================================================================
# Mixed cups are LINELESS (house rule: the line is handled manually)
# =============================================================================


def _setup_line_players(client):
    create_player(client, "Alice", has_line=True)
    create_player(client, "Bob", has_line=True)
    create_player(client, "Carol", has_line=True)


def test_mixed_cup_stays_lineless_with_three_players(client, monkeypatch):
    # 3 players is exactly the shape that triggers the Wii line adjustment, so
    # this is the case that must NOT fire on a mixed cup. The hostile lines[]
    # (which the UI hides) must be zeroed server-side.
    _fix_flip(monkeypatch, "wii")
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition=MIXED_EDITION)
    _submit_scores(
        client,
        cup_id=1,
        player_ids=["1", "2", "3"],
        scores=["100", "90", "80"],
        lines=["50", "30", "10"],  # hostile
    )

    conn = get_connection()
    lc = conn.execute(
        "SELECT COUNT(*) AS n FROM line_changes WHERE cup_id = 1"
    ).fetchone()["n"]
    player_lines = [
        r["line"] for r in conn.execute("SELECT line FROM players ORDER BY id")
    ]
    score_rows = conn.execute(
        "SELECT score, line, line_score FROM scores WHERE cup_id = 1"
    ).fetchall()
    conn.close()

    assert lc == 0                     # no line_changes rows
    assert player_lines == [0, 0, 0]   # players.line untouched
    for s in score_rows:
        assert s["line"] == 0
        assert s["line_score"] == s["score"]


def test_mixed_cup_stays_lineless_with_two_players(client, monkeypatch):
    _fix_flip(monkeypatch, "mk8dx")
    _setup_line_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _submit_scores(
        client,
        cup_id=1,
        player_ids=["1", "2"],
        scores=["100", "90"],
        lines=["7", "-7"],  # hostile
    )
    conn = get_connection()
    lc = conn.execute(
        "SELECT COUNT(*) AS n FROM line_changes WHERE cup_id = 1"
    ).fetchone()["n"]
    score_rows = conn.execute(
        "SELECT score, line, line_score FROM scores WHERE cup_id = 1"
    ).fetchall()
    player_lines = [
        r["line"] for r in conn.execute("SELECT line FROM players ORDER BY id")
    ]
    conn.close()
    assert lc == 0
    assert player_lines == [0, 0, 0]
    for s in score_rows:
        assert s["line"] == 0 and s["line_score"] == s["score"]


def test_mixed_cup_edit_ignores_submitted_lines(client, monkeypatch):
    _fix_flip(monkeypatch, "wii")
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition=MIXED_EDITION)
    _submit_scores(
        client,
        cup_id=1,
        player_ids=["1", "2", "3"],
        scores=["100", "90", "80"],
        lines=["0", "0", "0"],
    )
    client.post(
        "/cups/1/edit",
        data={
            "date": "2030-01-01T12:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2", "3"],
            "scores[]": ["100", "90", "80"],
            "lines[]": ["50", "30", "10"],  # hostile
        },
        follow_redirects=True,
    )
    conn = get_connection()
    score_rows = conn.execute(
        "SELECT score, line, line_score FROM scores WHERE cup_id = 1"
    ).fetchall()
    player_lines = [
        r["line"] for r in conn.execute("SELECT line FROM players ORDER BY id")
    ]
    conn.close()
    assert player_lines == [0, 0, 0]
    for s in score_rows:
        assert s["line"] == 0 and s["line_score"] == s["score"]


def test_mixed_complete_page_hides_line_inputs(client, monkeypatch):
    _fix_flip(monkeypatch, "wii")
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition=MIXED_EDITION)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert 'class="line-input"' not in page


def test_mixed_edit_page_hides_line_inputs_including_the_js_flag(
    client, monkeypatch
):
    # cup_edit.html gates BOTH the rendered rows and its add-player JS on
    # lines_on — missing the JS one silently re-enables lines on new rows.
    _fix_flip(monkeypatch, "wii")
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition=MIXED_EDITION)
    _submit_scores(
        client, cup_id=1, player_ids=["1", "2", "3"], scores=["100", "90", "80"]
    )
    page = client.get("/cups/1/edit").get_data(as_text=True)
    # The add-player JS carries a `line-input` string in its row TEMPLATE, so
    # only inspect the server-rendered form, then check the JS gate separately.
    form_html = page.split("<script")[0]
    assert 'class="line-input"' not in form_html
    assert "var linesOn = 0;" in page


def test_wii_edit_page_still_enables_lines(client):
    # Regression control: the lines_on plumbing must not disable Wii lines.
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition="wii")
    _submit_scores(
        client, cup_id=1, player_ids=["1", "2", "3"], scores=["100", "90", "80"]
    )
    page = client.get("/cups/1/edit").get_data(as_text=True)
    assert "var linesOn = 1;" in page


def test_wii_cup_still_applies_lines_control(client):
    # Regression control: same 3-player shape on a pure Wii cup DOES adjust.
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition="wii")
    _submit_scores(
        client,
        cup_id=1,
        player_ids=["1", "2", "3"],
        scores=["100", "90", "80"],
        lines=["0", "0", "0"],
    )
    conn = get_connection()
    lc = conn.execute(
        "SELECT COUNT(*) AS n FROM line_changes WHERE cup_id = 1"
    ).fetchone()["n"]
    player_lines = [
        r["line"] for r in conn.execute("SELECT line FROM players ORDER BY id")
    ]
    conn.close()
    assert lc == 3
    assert player_lines != [0, 0, 0]  # lines actually moved


def test_mk8dx_cup_still_lineless_control(client):
    _setup_line_players(client)
    _create_session(client, ["1", "2", "3"], edition="mk8dx")
    _submit_scores(
        client,
        cup_id=1,
        player_ids=["1", "2", "3"],
        scores=["100", "90", "80"],
        lines=["50", "30", "10"],
    )
    conn = get_connection()
    lc = conn.execute(
        "SELECT COUNT(*) AS n FROM line_changes WHERE cup_id = 1"
    ).fetchone()["n"]
    conn.close()
    assert lc == 0


# =============================================================================
# The raw score routes must respect the lineless rule too
#
# POST /scores and POST /scores/<id>/edit stamped players.line in
# unconditionally, so a mixed (or pure Switch) cup could still end up with a
# handicap on a score row — a live write path around the feature's headline
# "mixed cups are lineless" rule, bypassing zero_lines_if_lineless (which only
# the completion paths call). Pre-existing on main for Switch cups; closed here
# because a documented invariant with a known open write path isn't one.
#
# NOTE: these routes ALSO lack a completed-cup guard. That's issue #63 and is
# deliberately NOT addressed here — only the lineless stamping.
# =============================================================================


def _line_player(client, name="Alice", line=5):
    create_player(client, name, has_line=True)
    conn = get_connection()
    conn.execute("UPDATE players SET line = ? WHERE name = ?", (line, name))
    conn.commit()
    pid = conn.execute("SELECT id FROM players WHERE name = ?", (name,)).fetchone()["id"]
    conn.close()
    return pid


def _score_row(cup_id=1):
    conn = get_connection()
    row = conn.execute(
        "SELECT score, line, line_score FROM scores WHERE cup_id = ?", (cup_id,)
    ).fetchone()
    conn.close()
    return row


@pytest.mark.parametrize("edition", [MIXED_EDITION, "mk8dx"])
def test_create_score_route_stays_lineless(client, monkeypatch, edition):
    _fix_flip(monkeypatch, "wii")
    pid = _line_player(client)
    _create_session(client, [str(pid)], edition=edition)

    client.post(
        "/scores",
        data={"cup_id": "1", "player_id": str(pid), "score": "11"},
        follow_redirects=True,
    )
    row = _score_row()
    assert row is not None, "the score should still be created"
    assert row["score"] == 11
    assert row["line"] == 0          # was 5 — players.line stamped in
    assert row["line_score"] == 11   # was 16


@pytest.mark.parametrize("edition", [MIXED_EDITION, "mk8dx"])
def test_update_score_route_stays_lineless(client, monkeypatch, edition):
    _fix_flip(monkeypatch, "wii")
    pid = _line_player(client)
    _create_session(client, [str(pid)], edition=edition)
    _submit_scores(client, cup_id=1, player_ids=[str(pid)], scores=["40"])

    conn = get_connection()
    score_id = conn.execute(
        "SELECT id FROM scores WHERE cup_id = 1"
    ).fetchone()["id"]
    conn.close()

    client.post(
        f"/scores/{score_id}/edit", data={"score": "50"}, follow_redirects=True
    )
    row = _score_row()
    assert row["score"] == 50
    assert row["line"] == 0          # was 5 — re-stamped on every edit
    assert row["line_score"] == 50   # was 55


def test_create_score_route_still_stamps_the_line_on_a_wii_cup(client):
    # Control: the gate must not over-reach and disable lines for Wii.
    pid = _line_player(client)
    _create_session(client, [str(pid)], edition="wii")

    client.post(
        "/scores",
        data={"cup_id": "1", "player_id": str(pid), "score": "11"},
        follow_redirects=True,
    )
    row = _score_row()
    assert row["line"] == 5
    assert row["line_score"] == 16


def test_update_score_route_still_stamps_the_line_on_a_wii_cup(client):
    pid = _line_player(client)
    _create_session(client, [str(pid)], edition="wii")
    _submit_scores(client, cup_id=1, player_ids=[str(pid)], scores=["40"])

    conn = get_connection()
    score_id = conn.execute("SELECT id FROM scores WHERE cup_id = 1").fetchone()["id"]
    conn.close()

    client.post(
        f"/scores/{score_id}/edit", data={"score": "50"}, follow_redirects=True
    )
    row = _score_row()
    assert row["line"] == 5
    assert row["line_score"] == 55


# =============================================================================
# Existing guards still hold on a mixed cup
# =============================================================================


def test_mixed_roster_freshness_guard_rejects_a_stale_completion(
    client, monkeypatch
):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "wii")
    _play_half(client, 1, "mk8dx")

    # Submit a roster that doesn't match the live cup_players set.
    client.post(
        "/cup-session/1/complete",
        data=_complete_form(["1", "2", "3"], ["100", "80", "60"]),
        follow_redirects=True,
    )
    conn = get_connection()
    scores = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE cup_id = 1"
    ).fetchone()["n"]
    conn.close()
    assert _cup_row()["status"] == "in_progress"
    assert scores == 0


def test_stale_veto_forfeit_still_fires_at_the_swap(client, monkeypatch):
    # The forfeit lands on exactly the page load that shows the swap reminder.
    # No logical interaction, but both must render together.
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "wii")

    conn = get_connection()
    counts = [
        r["half_veto_count"]
        for r in conn.execute(
            "SELECT half_veto_count FROM cup_players WHERE cup_id = 1 ORDER BY player_id"
        )
    ]
    conn.close()
    assert counts == [1, 1]  # each forfeited one

    page = client.get("/cup-session/1").get_data(as_text=True)
    assert "Stale veto forfeit" in page
    assert 'id="swap-reminder-modal"' in page


def test_mixed_cup_cannot_exceed_max_races(client, monkeypatch):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "wii")
    _play_half(client, 1, "mk8dx")
    resp = _record_race(client, 1, TRACK_SETS["mk8dx"][10])
    assert resp.status_code == 400
    assert len(_races()) == MAX_RACES


# =============================================================================
# Display
# =============================================================================


def test_console_flip_modal_shows_only_before_race_one(client, monkeypatch):
    _fix_flip(monkeypatch, "mk8dx")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="console-flip-modal"' in page
    assert "Switch" in page

    _record_race(client, 1, TRACK_SETS["mk8dx"][0])
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="console-flip-modal"' not in page


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_pure_cups_never_show_the_console_flip_or_swap_modals(client, edition):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=edition)

    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="console-flip-modal"' not in page
    assert 'id="swap-reminder-modal"' not in page

    # Even at the race-3 boundary, where a mixed cup would swap consoles.
    _play_half(client, 1, edition)
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="swap-reminder-modal"' not in page
    assert 'class="console-badge"' not in page
    # Match the rendered element — the script block mentions the banner's id in
    # a comment, and the <style> block carries its CSS rule.
    assert 'id="second-console-banner"' not in page


def test_swap_reminder_shows_only_when_entering_the_second_half(
    client, monkeypatch
):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    assert 'id="swap-reminder-modal"' not in client.get(
        "/cup-session/1"
    ).get_data(as_text=True)

    _record_race(client, 1, TRACK_SETS["wii"][0])
    assert 'id="swap-reminder-modal"' not in client.get(
        "/cup-session/1"
    ).get_data(as_text=True)

    _record_race(client, 1, TRACK_SETS["wii"][1])
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="swap-reminder-modal"' in page
    assert "Switch" in page  # names the console they're switching to

    _record_race(client, 1, TRACK_SETS["mk8dx"][0])
    assert 'id="swap-reminder-modal"' not in client.get(
        "/cup-session/1"
    ).get_data(as_text=True)


def test_race_page_labels_each_completed_race_with_its_console(
    client, monkeypatch
):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "wii")
    _record_race(client, 1, TRACK_SETS["mk8dx"][0])

    page = client.get("/cup-session/1").get_data(as_text=True)
    assert page.count('class="console-badge"') == 3
    assert "Wii &rarr; Switch" in page or "Wii → Switch" in page


def test_completion_page_shows_per_race_console_labels(client, monkeypatch):
    _fix_flip(monkeypatch, "mk8dx")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "mk8dx")
    _play_half(client, 1, "wii")

    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert "Race 1 (Switch)" in page
    assert "Race 2 (Switch)" in page
    assert "Race 3 (Wii)" in page
    assert "Race 4 (Wii)" in page


def test_completion_page_scopes_each_dropdown_to_its_console(
    client, monkeypatch
):
    # Race 1 is a Switch race: its <select> must not offer Wii-only courses.
    _fix_flip(monkeypatch, "mk8dx")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "mk8dx")
    _play_half(client, 1, "wii")

    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    race_1 = page.split('id="race-1"')[1].split("</select>")[0]
    race_3 = page.split('id="race-3"')[1].split("</select>")[0]
    assert "Ninja Hideaway" in race_1        # MK8DX-only
    assert "Luigi Circuit" not in race_1     # Wii-only
    assert "Luigi Circuit" in race_3
    assert "Ninja Hideaway" not in race_3


def test_pure_cup_completion_page_is_unchanged(client):
    # No per-race console labels on a pure cup.
    _setup_players(client)
    _create_session(client, ["1", "2"], edition="wii")
    _play_half(client, 1, "wii", count=MAX_RACES)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert "Race 1:" in page
    assert "Race 1 (" not in page


def test_history_shows_the_console_order_badge(client, monkeypatch):
    _fix_flip(monkeypatch, "mk8dx")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    _play_half(client, 1, "mk8dx")
    _play_half(client, 1, "wii")
    _submit_scores(client, cup_id=1, player_ids=["1", "2"], scores=["100", "80"])

    page = client.get("/cups").get_data(as_text=True)
    assert "Switch &rarr; Wii" in page or "Switch → Wii" in page


def test_history_pure_cups_keep_their_plain_label(client):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition="mk8dx")
    _submit_scores(client, cup_id=1, player_ids=["1", "2"], scores=["100", "80"])
    page = client.get("/cups").get_data(as_text=True)
    assert ">Switch<" in page
    assert "→" not in page


def test_new_cup_page_offers_the_mixed_option(client):
    page = client.get("/cup-session/new").get_data(as_text=True)
    assert 'value="mixed"' in page
    assert "Wii + Switch" in page


# =============================================================================
# Photo extraction on a mixed cup
# =============================================================================


def test_extraction_uses_the_second_half_edition_for_a_mixed_cup(
    client, monkeypatch
):
    # The results screen photographed at completion belongs to the SECOND
    # console, so character matching + the prompt must use that edition —
    # 'mixed' would fall into the unknown-edition branches.
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)

    edition, players, partial_half, error = appmod._players_for_extraction(
        {"cup_id": 1}
    )
    assert error is None
    assert edition == "mk8dx"
    assert partial_half is True  # the photo is only half the cup
    assert len(players) == 2


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_extraction_on_a_pure_cup_is_not_partial(client, edition):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=edition)
    got, players, partial_half, error = appmod._players_for_extraction(
        {"cup_id": 1}
    )
    assert error is None
    assert got == edition
    assert partial_half is False


def test_extraction_manual_path_still_rejects_mixed(client):
    # The manual /cups/new path has no cup to resolve a half from, so 'mixed'
    # must stay an invalid edition there.
    _setup_players(client)
    edition, players, partial_half, error = appmod._players_for_extraction(
        {"edition": MIXED_EDITION, "player_ids": [1]}
    )
    assert edition is None
    assert partial_half is False
    assert error is not None
    assert error[1] == 400


# --- /extract-scores: a mixed cup must never auto-fill half totals ---------


def _fake_standings(*rows):
    """rows: (position, character, points, is_highlighted) -> a fake extractor."""
    from extraction import Standings, StandingsRow

    standings = Standings(
        rows=[
            StandingsRow(
                position=p, character=c, points=pts, is_highlighted=hl
            )
            for p, c, pts, hl in rows
        ]
    )

    def fake_extract(image_b64, media_type, edition=None):
        return standings

    return fake_extract


def _setup_character_players(client):
    for name, wii_char, switch_char in [
        ("Alice", "Funky Kong", "Yoshi"),
        ("Bob", "Yoshi", "Mario"),
    ]:
        client.post(
            "/players",
            data={
                "name": name,
                "default_cup": "on",
                "default_character_wii": wii_char,
                "default_character_switch": switch_char,
            },
        )


def _post_photo(client, cup_id):
    import base64

    return client.post(
        "/extract-scores",
        json={
            "image": base64.b64encode(b"\xff\xd8fake").decode("ascii"),
            "mime_type": "image/jpeg",
            "cup_id": cup_id,
        },
    )


def test_mixed_cup_extract_returns_no_autofill_scores(
    client, monkeypatch
):
    # THE failure this guards: the completion photo shows only the second
    # console's screen, so every extracted number is a HALF total (Alice 42
    # where her cup total is 88). Auto-filling those looks completely plausible
    # and would permanently record roughly half the true points.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _fix_flip(monkeypatch, "wii")
    _setup_character_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    monkeypatch.setattr(
        appmod,
        "extract_standings",
        _fake_standings((1, "Yoshi", 42, True), (2, "Mario", 38, True)),
    )

    data = _post_photo(client, 1).get_json()
    assert data["partial_half"] is True
    assert data["scores"] == {}           # nothing auto-filled
    assert data["ambiguous"] == []
    assert data["unmatched_players"] == []
    # The rows still ship — the mapping panel stays usable as a reference.
    assert len(data["raw_rows"]) == 2
    assert data["raw_rows"][0]["points"] == 42


def test_pure_cup_extract_still_autofills(client, monkeypatch):
    # Regression control: suppressing auto-fill must be mixed-only.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _setup_character_players(client)
    _create_session(client, ["1", "2"], edition="mk8dx")
    monkeypatch.setattr(
        appmod,
        "extract_standings",
        _fake_standings((1, "Yoshi", 42, True), (2, "Mario", 38, True)),
    )

    data = _post_photo(client, 1).get_json()
    assert data["partial_half"] is False
    assert data["scores"] == {"1": 42, "2": 38}
    assert len(data["raw_rows"]) == 2


def test_mixed_complete_page_warns_inside_the_photo_panel(
    client, monkeypatch
):
    _fix_flip(monkeypatch, "wii")
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)

    # A mixed cup no longer renders the single #photo-score panel at all — it
    # gets one panel PER CONSOLE BLOCK (see test_mixed_cup_block_photos.py).
    assert 'id="photo-score"' not in page
    panel = page.split('id="photo-blocks"')[1]
    assert 'class="photo-half-warning"' in panel
    # Names BOTH consoles: the point of the warning is that each scoreboard
    # started from zero, so there are two screens to account for.
    warning = panel.split('class="photo-half-warning"')[1].split("</p>")[0]
    assert "Switch" in warning
    assert "Wii" in warning


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_pure_complete_page_has_no_photo_half_warning(client, edition):
    _setup_players(client)
    _create_session(client, ["1", "2"], edition=edition)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    # Match the rendered element, not the .photo-half-warning CSS rule in <style>.
    assert 'class="photo-half-warning"' not in page
