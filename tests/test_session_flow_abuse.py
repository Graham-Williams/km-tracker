"""Out-of-order and double-submit abuse of the cup-session flow.

Sequential (test-client) simulations of stale tabs, double-clicks, and
skipped steps. The app must reject gracefully (4xx / no-op redirect) and the
data invariants must hold: no duplicate scores, no double line adjustments,
race/veto counters never exceed their limits, statuses never regress.
"""

from db import get_connection
from helpers import create_player


def _setup_players(client, lines=False):
    create_player(client, "Alice", has_line=lines)
    create_player(client, "Bob", has_line=lines)
    create_player(client, "Carol", has_line=lines)


def _start_session(client, player_ids=("1", "2", "3")):
    client.post("/cup-session/new", data={"player_ids[]": list(player_ids)})
    conn = get_connection()
    row = conn.execute("SELECT id FROM cups WHERE status = 'in_progress'").fetchone()
    conn.close()
    return row["id"]


def _play_races(client, cup_id, count=4):
    maps = ["Coconut Mall", "Rainbow Road", "Moo Moo Meadows", "Koopa Cape"][:count]
    for m in maps:
        client.post(f"/cup-session/{cup_id}/next-race", json={"map": m})


def _submit(client, cup_id, player_ids=("1", "2", "3"), scores=("100", "80", "60"), lines=None):
    if lines is None:
        lines = ["0"] * len(player_ids)
    return client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "notes": "",
            "tz_offset": "",
            "player_ids[]": list(player_ids),
            "scores[]": list(scores),
            "lines[]": list(lines),
        },
        follow_redirects=False,
    )


def _snapshot(cup_id):
    """All state a double submit could corrupt."""
    conn = get_connection()
    snap = {
        "status": conn.execute("SELECT status FROM cups WHERE id = ?", (cup_id,)).fetchone()["status"],
        "scores": [
            tuple(r)
            for r in conn.execute(
                "SELECT player_id, score, line, line_score FROM scores WHERE cup_id = ? ORDER BY player_id",
                (cup_id,),
            ).fetchall()
        ],
        "line_changes": [
            tuple(r)
            for r in conn.execute(
                "SELECT player_id, line_before, line_after FROM line_changes WHERE cup_id = ? ORDER BY player_id",
                (cup_id,),
            ).fetchall()
        ],
        "player_lines": [
            tuple(r)
            for r in conn.execute("SELECT id, line FROM players ORDER BY id").fetchall()
        ],
        "races": conn.execute(
            "SELECT COUNT(*) FROM races WHERE cup_id = ?", (cup_id,)
        ).fetchone()[0],
    }
    conn.close()
    return snap


# =============================================================================
# Double submit of the final scores
# =============================================================================


def test_double_submit_second_rejected_and_state_unchanged(client):
    # Players with lines so the submit also applies line adjustments — the
    # highest-risk thing to double-apply.
    _setup_players(client, lines=True)
    cup_id = _start_session(client)
    _play_races(client, cup_id)

    first = _submit(client, cup_id)
    assert first.status_code == 302
    snap = _snapshot(cup_id)
    assert snap["status"] == "completed"
    assert len(snap["scores"]) == 3
    assert len(snap["line_changes"]) == 3  # one adjustment per lined player

    # Stale tab resubmits the exact same form.
    second = _submit(client, cup_id)
    assert second.status_code == 404  # cup no longer in_progress

    assert _snapshot(cup_id) == snap  # nothing moved: scores, lines, changes


def test_double_submit_does_not_double_adjust_lines(client):
    _setup_players(client, lines=True)
    cup_id = _start_session(client)
    _play_races(client, cup_id)
    _submit(client, cup_id)

    conn = get_connection()
    lines_after_first = {
        r["id"]: r["line"] for r in conn.execute("SELECT id, line FROM players").fetchall()
    }
    conn.close()
    # 1st place -3, 2nd 0, 3rd +3
    assert lines_after_first == {1: -3, 2: 0, 3: 3}

    _submit(client, cup_id)  # rejected

    conn = get_connection()
    lines_after_second = {
        r["id"]: r["line"] for r in conn.execute("SELECT id, line FROM players").fetchall()
    }
    conn.close()
    assert lines_after_second == lines_after_first


def test_double_submit_with_different_scores_ignored(client):
    # The second (conflicting) submit must not overwrite the recorded result.
    _setup_players(client)
    cup_id = _start_session(client)
    _play_races(client, cup_id)
    _submit(client, cup_id, scores=("100", "80", "60"))

    second = _submit(client, cup_id, scores=("1", "2", "3"))
    assert second.status_code == 404

    conn = get_connection()
    scores = {
        r["player_id"]: r["score"]
        for r in conn.execute("SELECT player_id, score FROM scores WHERE cup_id = ?", (cup_id,)).fetchall()
    }
    conn.close()
    assert scores == {1: 100, 2: 80, 3: 60}


# =============================================================================
# Race recording: over-limit and duplicate race numbers
# =============================================================================


def test_fifth_race_rejected_and_not_persisted(client):
    _setup_players(client)
    cup_id = _start_session(client)
    _play_races(client, cup_id, count=4)

    response = client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Luigi Circuit"})
    assert response.status_code == 400

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM races WHERE cup_id = ?", (cup_id,)).fetchone()[0]
    conn.close()
    assert count == 4


def test_duplicate_race_number_conflict_rejected(client):
    # Simulate a second client having already written a race with the number
    # this request will compute (count+1): races {1, 3} -> next number is 3,
    # which collides with the UNIQUE(cup_id, race_number) constraint.
    _setup_players(client)
    cup_id = _start_session(client)
    client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Coconut Mall"})
    conn = get_connection()
    conn.execute(
        "INSERT INTO races (cup_id, race_number, map) VALUES (?, 3, 'Rainbow Road')",
        (cup_id,),
    )
    conn.commit()
    conn.close()

    response = client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Koopa Cape"})
    assert response.status_code == 400
    assert b"already recorded" in response.data

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM races WHERE cup_id = ?", (cup_id,)).fetchone()[0]
    conn.close()
    assert count == 2  # the conflicting insert was not persisted


# =============================================================================
# Vetoes when not allowed
# =============================================================================


def test_voto_past_limit_rejected_and_counter_capped(client):
    _setup_players(client)
    cup_id = _start_session(client)
    for _ in range(4):
        assert client.post(f"/cup-session/{cup_id}/voto").status_code == 200

    for _ in range(3):  # hammer it a few more times
        assert client.post(f"/cup-session/{cup_id}/voto").status_code == 400

    conn = get_connection()
    voto_count = conn.execute("SELECT voto_count FROM cups WHERE id = ?", (cup_id,)).fetchone()[0]
    conn.close()
    assert voto_count == 4


def test_half_veto_past_limit_rejected_and_counter_capped(client):
    _setup_players(client)
    cup_id = _start_session(client)
    for _ in range(3):
        assert client.post(f"/cup-session/{cup_id}/half-veto", json={"player_id": 1}).status_code == 200

    for _ in range(3):
        assert client.post(f"/cup-session/{cup_id}/half-veto", json={"player_id": 1}).status_code == 400

    conn = get_connection()
    count = conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = ? AND player_id = 1", (cup_id,)
    ).fetchone()[0]
    conn.close()
    assert count == 3


def test_veto_on_completed_cup_rejected_counters_unchanged(client):
    _setup_players(client)
    cup_id = _start_session(client)
    _play_races(client, cup_id)
    _submit(client, cup_id)

    # Snapshot counters after play (the stale-veto forfeit may have adjusted
    # half_veto_count at race 3) so we assert the *rejected* calls don't mutate.
    conn = get_connection()
    voto_before = conn.execute("SELECT voto_count FROM cups WHERE id = ?", (cup_id,)).fetchone()[0]
    half_before = conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = ? AND player_id = 1", (cup_id,)
    ).fetchone()[0]
    conn.close()

    assert client.post(f"/cup-session/{cup_id}/voto").status_code == 404
    assert client.post(f"/cup-session/{cup_id}/half-veto", json={"player_id": 1}).status_code == 404

    conn = get_connection()
    voto = conn.execute("SELECT voto_count FROM cups WHERE id = ?", (cup_id,)).fetchone()[0]
    half = conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = ? AND player_id = 1", (cup_id,)
    ).fetchone()[0]
    conn.close()
    assert voto == voto_before
    assert half == half_before


def test_veto_on_cancelled_cup_rejected(client):
    _setup_players(client)
    cup_id = _start_session(client)
    client.post(f"/cup-session/{cup_id}/cancel")

    assert client.post(f"/cup-session/{cup_id}/voto").status_code == 404
    assert client.post(f"/cup-session/{cup_id}/half-veto", json={"player_id": 1}).status_code == 404


# =============================================================================
# Acting on completed / cancelled cups
# =============================================================================


def test_all_session_actions_rejected_after_completion(client):
    _setup_players(client)
    cup_id = _start_session(client)
    _play_races(client, cup_id)
    _submit(client, cup_id)
    snap = _snapshot(cup_id)

    assert client.post(f"/cup-session/{cup_id}/spin").status_code == 404
    assert client.post(f"/cup-session/{cup_id}/next-race", json={"map": "Luigi Circuit"}).status_code == 404
    assert client.get(f"/cup-session/{cup_id}").status_code == 404  # race page gone

    assert _snapshot(cup_id) == snap


def test_submit_after_cancel_rejected_no_scores(client):
    _setup_players(client)
    cup_id = _start_session(client)
    client.post(f"/cup-session/{cup_id}/cancel")

    response = _submit(client, cup_id)
    assert response.status_code == 404

    snap = _snapshot(cup_id)
    assert snap["status"] == "cancelled"
    assert snap["scores"] == []
    assert snap["line_changes"] == []


def test_cancel_after_submit_does_not_unwind_completion(client):
    _setup_players(client)
    cup_id = _start_session(client)
    _play_races(client, cup_id)
    _submit(client, cup_id)
    snap = _snapshot(cup_id)

    response = client.post(f"/cup-session/{cup_id}/cancel")
    assert response.status_code == 302  # no-op redirect

    after = _snapshot(cup_id)
    assert after["status"] == "completed"
    assert after == snap


def test_double_cancel_is_idempotent(client):
    _setup_players(client)
    cup_id = _start_session(client)
    client.post(f"/cup-session/{cup_id}/cancel")
    response = client.post(f"/cup-session/{cup_id}/cancel")
    assert response.status_code == 302

    conn = get_connection()
    status = conn.execute("SELECT status FROM cups WHERE id = ?", (cup_id,)).fetchone()["status"]
    conn.close()
    assert status == "cancelled"


# =============================================================================
# Skipping / reordering steps
# =============================================================================


def test_submit_scores_without_playing_any_races(client):
    # Skipping straight from session start to the scores form is allowed by
    # design (early finish); it must complete cleanly with zero races.
    _setup_players(client)
    cup_id = _start_session(client)

    response = _submit(client, cup_id)
    assert response.status_code == 302

    snap = _snapshot(cup_id)
    assert snap["status"] == "completed"
    assert len(snap["scores"]) == 3
    assert snap["races"] == 0


def test_second_session_cannot_start_while_one_in_progress(client):
    _setup_players(client)
    cup_id = _start_session(client)

    response = client.post(
        "/cup-session/new", data={"player_ids[]": ["1"]}, follow_redirects=False
    )
    # Redirected back to the existing session, and no second cup was created.
    assert response.status_code == 302
    assert f"/cup-session/{cup_id}" in response.headers["Location"]

    conn = get_connection()
    in_progress = conn.execute("SELECT COUNT(*) FROM cups WHERE status = 'in_progress'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM cups").fetchone()[0]
    conn.close()
    assert in_progress == 1
    assert total == 1


def test_spin_after_all_races_rejected_without_side_effects(client):
    _setup_players(client)
    cup_id = _start_session(client)
    _play_races(client, cup_id, count=4)

    response = client.post(f"/cup-session/{cup_id}/spin")
    assert response.status_code == 400

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM races WHERE cup_id = ?", (cup_id,)).fetchone()[0]
    conn.close()
    assert count == 4
