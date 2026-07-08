"""Regression tests for the data-integrity batch (issues #36, #39, #40, #41, #45).

These are stat-corrupting / crash holes found by adversarial QA (break-staging).
Each test fails against the pre-fix code and passes with the guard/atomicity fix.
No test alters existing prod data — they only exercise validation/atomicity.
"""

import app as app_module
from app import (
    MAX_HALF_VETOES,
    MAX_VOTOES,
    increment_half_veto,
    increment_voto,
)
from db import get_connection
from helpers import create_cup, create_player


# --- shared helpers -------------------------------------------------------


def _count(table, where="1=1", params=()):
    conn = get_connection()
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    conn.close()
    return n


def _players(client, n=3):
    for name in ["Alice", "Bob", "Carol", "Dave"][:n]:
        create_player(client, name)


def _start_session(client, player_ids=("1", "2", "3"), edition="wii"):
    _players(client, len(player_ids))
    client.post(
        "/cup-session/new",
        data={"player_ids[]": list(player_ids), "game_edition": edition},
    )
    conn = get_connection()
    row = conn.execute("SELECT id FROM cups WHERE status = 'in_progress'").fetchone()
    conn.close()
    return row["id"]


def _post_score(client, cup_id, player_id, score):
    return client.post(
        "/scores",
        data={"cup_id": str(cup_id), "player_id": str(player_id), "score": str(score)},
        follow_redirects=True,
    )


# =============================================================================
# #40 — POST /scores must reject writes to non-in-progress / soft-deleted cups
# =============================================================================


def test_score_write_rejected_on_completed_cup(client):
    # A completed cup's standings are finalized; a raw standalone insert would
    # corrupt them (no placement/line recompute). Must reject cleanly, not 500.
    _players(client, 2)
    create_cup(client, player_id="1", score="50")  # direct cup -> status 'completed'
    resp = _post_score(client, cup_id=1, player_id=2, score=100)
    assert resp.status_code < 500
    assert b"in progress" in resp.data
    assert _count("scores", "cup_id = 1 AND player_id = 2") == 0


def test_score_write_rejected_on_soft_deleted_cup(client):
    cup_id = _start_session(client, ("1", "2"))
    conn = get_connection()
    conn.execute("UPDATE cups SET deleted_at = datetime('now') WHERE id = ?", (cup_id,))
    conn.commit()
    conn.close()
    resp = _post_score(client, cup_id=cup_id, player_id=2, score=100)
    assert resp.status_code < 500
    assert _count("scores", "cup_id = ? AND player_id = 2", (cup_id,)) == 0


def test_score_write_rejected_on_cancelled_cup(client):
    cup_id = _start_session(client, ("1", "2"))
    client.post(f"/cup-session/{cup_id}/cancel")
    resp = _post_score(client, cup_id=cup_id, player_id=2, score=100)
    assert resp.status_code < 500
    assert _count("scores", "cup_id = ? AND player_id = 2", (cup_id,)) == 0


def test_score_write_rejected_on_nonexistent_cup(client):
    _players(client, 1)
    resp = _post_score(client, cup_id=99999, player_id=1, score=100)
    assert resp.status_code < 500
    assert _count("scores") == 0


def test_score_write_allowed_on_in_progress_cup(client):
    # The valid path must still work: an in-progress cup accepts a standalone score.
    cup_id = _start_session(client, ("1", "2"))
    resp = _post_score(client, cup_id=cup_id, player_id=2, score=100)
    assert resp.status_code < 500
    assert _count("scores", "cup_id = ? AND player_id = 2", (cup_id,)) == 1


# =============================================================================
# #41 — next-race must reject arbitrary / off-edition course names
# =============================================================================


def test_next_race_rejects_arbitrary_course(client):
    cup_id = _start_session(client, ("1", "2"))
    resp = client.post(
        f"/cup-session/{cup_id}/next-race", json={"map": "Totally Not A Real Track"}
    )
    assert resp.status_code == 400
    assert _count("races", "cup_id = ?", (cup_id,)) == 0


def test_next_race_rejects_off_edition_course(client):
    # A Switch-only course name on a Wii cup must be rejected (would pollute stats).
    cup_id = _start_session(client, ("1", "2"), edition="wii")
    resp = client.post(
        f"/cup-session/{cup_id}/next-race", json={"map": "Mario Kart Stadium"}  # mk8dx-only
    )
    assert resp.status_code == 400
    assert _count("races", "cup_id = ?", (cup_id,)) == 0


def test_next_race_rejects_wii_course_on_mk8dx_cup(client):
    cup_id = _start_session(client, ("1", "2"), edition="mk8dx")
    # "Coconut Mall" is a Wii course; the mk8dx set has "Wii Coconut Mall" instead.
    resp = client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Coconut Mall"})
    assert resp.status_code == 400
    assert _count("races", "cup_id = ?", (cup_id,)) == 0
    # A genuine mk8dx course is accepted.
    ok = client.post(
        f"/cup-session/{cup_id}/next-race", json={"map": "Mario Kart Stadium"}
    )
    assert ok.status_code == 200
    assert _count("races", "cup_id = ?", (cup_id,)) == 1


def test_next_race_accepts_valid_edition_course(client):
    cup_id = _start_session(client, ("1", "2"), edition="wii")
    resp = client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Coconut Mall"})
    assert resp.status_code == 200
    assert _count("races", "cup_id = ? AND map = 'Coconut Mall'", (cup_id,)) == 1


def test_complete_form_rejects_off_edition_race_map_override(client):
    # Second door for #41: the completion form re-writes race maps via race_N
    # fields. A crafted/stale POST with an off-edition/arbitrary course must be
    # rejected cleanly and must NOT persist the bogus name into race history.
    cup_id = _start_session(client, ("1", "2"), edition="wii")
    client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Coconut Mall"})  # race 1

    resp = client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "notes": "",
            "tz_offset": "",
            "race_1": "Mario Kart Stadium",  # mk8dx-only course on a Wii cup
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "lines[]": ["0", "0"],
        },
        follow_redirects=False,
    )
    assert resp.status_code < 500  # clean rejection (flash+redirect), not a 500
    conn = get_connection()
    race_map = conn.execute(
        "SELECT map FROM races WHERE cup_id = ? AND race_number = 1", (cup_id,)
    ).fetchone()["map"]
    status = conn.execute(
        "SELECT status FROM cups WHERE id = ?", (cup_id,)
    ).fetchone()["status"]
    conn.close()
    assert race_map == "Coconut Mall"  # bogus override never persisted
    assert status == "in_progress"  # rejected before completion


# =============================================================================
# #45 — misaligned player_ids[] / scores[] must not misattribute scores
# =============================================================================


def test_cup_create_rejects_misaligned_player_scores(client):
    # Mimics the buggy client where a removed MIDDLE player's hidden player_ids[]
    # still submitted while its scores[] did not: 3 ids, 2 scores. Positional zip
    # would attribute player 3's score (60) to player 2 and drop player 3.
    _players(client, 3)
    resp = client.post(
        "/cups",
        data={
            "date": "2026-05-01T20:00",
            "tz_offset": "",
            "player_ids[]": ["1", "2", "3"],
            "scores[]": ["100", "60"],  # middle player's score missing
            "lines[]": ["0", "0", "0"],
        },
        follow_redirects=True,
    )
    assert resp.status_code < 500
    # Nothing persisted with a misattributed pairing.
    assert _count("cups") == 0
    assert _count("scores") == 0


def test_cup_create_correct_when_middle_player_properly_removed(client):
    # The correctly-behaving client drops BOTH the removed player's id and score,
    # so the arrays stay aligned and pairing is exact.
    _players(client, 3)
    resp = client.post(
        "/cups",
        data={
            "date": "2026-05-01T20:00",
            "tz_offset": "",
            "player_ids[]": ["1", "3"],  # player 2 removed entirely
            "scores[]": ["100", "60"],
            "lines[]": ["0", "0"],
        },
        follow_redirects=True,
    )
    assert resp.status_code < 500
    conn = get_connection()
    scores = {
        r["player_id"]: r["score"]
        for r in conn.execute("SELECT player_id, score FROM scores").fetchall()
    }
    conn.close()
    assert scores == {1: 100, 3: 60}  # player 3 keeps its own score, none shifted


# =============================================================================
# #39 — concurrent cup completion must apply scores/lines exactly once
# =============================================================================


def test_completion_atomic_does_not_double_apply_under_race(client, monkeypatch):
    # Simulate the TOCTOU deterministically: request A passes its status check,
    # then (in the window before A's write) request B completes the cup with its
    # own scores. The atomic guard must make A's completion a no-op so B's result
    # stands — A must NOT overwrite scores or re-apply anything.
    cup_id = _start_session(client, ("1", "2", "3"))

    real_parse = app_module.parse_scores_from_form
    state = {"raced": False}

    def racing_parse(form):
        if not state["raced"]:
            state["raced"] = True
            # Request B wins the completion race.
            conn2 = get_connection()
            conn2.execute(
                "UPDATE cups SET status = 'completed' WHERE id = ?", (cup_id,)
            )
            for pid, sc in ((1, 100), (2, 80), (3, 60)):
                conn2.execute(
                    "INSERT INTO scores (cup_id, player_id, score, line, line_score) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (cup_id, pid, sc, sc),
                )
            conn2.commit()
            conn2.close()
        return real_parse(form)

    monkeypatch.setattr(app_module, "parse_scores_from_form", racing_parse)

    # Request A submits DIFFERENT scores; the atomic guard makes it a no-op.
    resp = client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2", "3"],
            "scores[]": ["10", "20", "30"],
            "lines[]": ["0", "0", "0"],
        },
        follow_redirects=False,
    )
    assert resp.status_code < 500  # clean loss, not a crash

    conn = get_connection()
    scores = {
        r["player_id"]: r["score"]
        for r in conn.execute(
            "SELECT player_id, score FROM scores WHERE cup_id = ?", (cup_id,)
        ).fetchall()
    }
    conn.close()
    # B's scores stand; A's 10/20/30 never overwrote them.
    assert scores == {1: 100, 2: 80, 3: 60}


def test_completion_status_transition_is_conditional(client):
    # Direct proof of the atomic guard: the completion UPDATE only affects the
    # in-progress row, so a second completer sees 0 affected rows.
    cup_id = _start_session(client, ("1", "2"))
    conn = get_connection()
    first = conn.execute(
        "UPDATE cups SET status = 'completed' WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    )
    assert first.rowcount == 1
    second = conn.execute(
        "UPDATE cups SET status = 'completed' WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    )
    assert second.rowcount == 0
    conn.commit()
    conn.close()


# =============================================================================
# #52 — standalone POST /scores must not write into a cup completed mid-request
# =============================================================================


def test_create_score_atomic_rejects_cup_completed_mid_request(client, monkeypatch):
    # Simulate the TOCTOU deterministically: create_score passes its in-progress
    # SELECT check, then (in the window before its INSERT) the cup is completed
    # by another request. The conditional INSERT (WHERE EXISTS in_progress) must
    # write nothing, so no score leaks into the now-completed cup.
    cup_id = _start_session(client, ("1", "2"))

    real_checked = app_module.checked_line_score
    state = {"raced": False}

    def racing_checked(score, line):
        if not state["raced"]:
            state["raced"] = True
            # Another request completes the cup in the gap.
            conn2 = get_connection()
            conn2.execute(
                "UPDATE cups SET status = 'completed' WHERE id = ?", (cup_id,)
            )
            conn2.commit()
            conn2.close()
        return real_checked(score, line)

    monkeypatch.setattr(app_module, "checked_line_score", racing_checked)

    resp = client.post(
        "/scores",
        data={"cup_id": str(cup_id), "player_id": "1", "score": "50"},
        follow_redirects=False,
    )
    assert resp.status_code < 500  # clean loss, not a crash

    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE cup_id = ?", (cup_id,)
    ).fetchone()[0]
    conn.close()
    assert n == 0  # nothing persisted into the completed cup


# =============================================================================
# #36 — veto/voto counters must stay at/under cap under concurrent increments
# =============================================================================


def test_voto_increment_is_atomic_and_cap_guarded(client):
    cup_id = _start_session(client, ("1", "2"))
    conn = get_connection()
    counts = [increment_voto(conn, cup_id) for _ in range(MAX_VOTOES)]
    conn.commit()
    assert counts == list(range(1, MAX_VOTOES + 1))
    # At the cap: further increments are rejected atomically (0 rows), no overflow.
    assert increment_voto(conn, cup_id) is None
    conn.commit()
    final = conn.execute(
        "SELECT voto_count FROM cups WHERE id = ?", (cup_id,)
    ).fetchone()["voto_count"]
    conn.close()
    assert final == MAX_VOTOES


def test_voto_two_racing_connections_cannot_exceed_cap(client):
    cup_id = _start_session(client, ("1", "2"))
    conn = get_connection()
    for _ in range(MAX_VOTOES - 1):
        increment_voto(conn, cup_id)
    conn.commit()
    conn.close()
    # Two connections both go for the last slot; only one may win.
    a = get_connection()
    b = get_connection()
    ra = increment_voto(a, cup_id)
    a.commit()
    rb = increment_voto(b, cup_id)
    b.commit()
    a.close()
    b.close()
    assert ra == MAX_VOTOES
    assert rb is None  # cap held; no overflow to MAX+1
    assert _count("cups", "voto_count > ?", (MAX_VOTOES,)) == 0


def test_half_veto_increment_is_atomic_and_cap_guarded(client):
    cup_id = _start_session(client, ("1", "2"))
    conn = get_connection()
    counts = [increment_half_veto(conn, cup_id, 1) for _ in range(MAX_HALF_VETOES)]
    conn.commit()
    assert counts == list(range(1, MAX_HALF_VETOES + 1))
    assert increment_half_veto(conn, cup_id, 1) is None
    conn.commit()
    final = conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = ? AND player_id = 1",
        (cup_id,),
    ).fetchone()["half_veto_count"]
    conn.close()
    assert final == MAX_HALF_VETOES


def test_half_veto_two_racing_connections_cannot_exceed_cap(client):
    cup_id = _start_session(client, ("1", "2"))
    conn = get_connection()
    for _ in range(MAX_HALF_VETOES - 1):
        increment_half_veto(conn, cup_id, 1)
    conn.commit()
    conn.close()
    a = get_connection()
    b = get_connection()
    ra = increment_half_veto(a, cup_id, 1)
    a.commit()
    rb = increment_half_veto(b, cup_id, 1)
    b.commit()
    a.close()
    b.close()
    assert ra == MAX_HALF_VETOES
    assert rb is None
    assert _count("cup_players", "half_veto_count > ?", (MAX_HALF_VETOES,)) == 0
