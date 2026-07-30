"""Mid-cup roster editing: add/remove players on an in-progress cup.

Covers the two guarded routes added for the mid-cup-roster-editing feature:
    POST /cup-session/<int:cup_id>/players/add
    POST /cup-session/<int:cup_id>/players/remove

and the two risky invariants: a late-added player is not retroactively
stale-veto-forfeited, and completion (scores + Wii 3-player line adjustments)
uses the LIVE roster after any mid-cup change.
"""

from db import get_connection
from helpers import create_player


# --- helpers -----------------------------------------------------------------


def _create_session(client, player_ids, edition=None):
    data = {"player_ids[]": [str(p) for p in player_ids]}
    if edition is not None:
        data["game_edition"] = edition
    return client.post("/cup-session/new", data=data, follow_redirects=False)


def _setup_players(client, has_line=False):
    create_player(client, "Alice", has_line=has_line)
    create_player(client, "Bob", has_line=has_line)
    create_player(client, "Carol", has_line=has_line)
    create_player(client, "Dave", has_line=has_line)


def _add(client, cup_id, player_id, follow=True):
    return client.post(
        f"/cup-session/{cup_id}/players/add",
        data={"player_id": str(player_id)},
        follow_redirects=follow,
    )


def _remove(client, cup_id, player_id, follow=True):
    return client.post(
        f"/cup-session/{cup_id}/players/remove",
        data={"player_id": str(player_id)},
        follow_redirects=follow,
    )


def _roster(cup_id=1):
    conn = get_connection()
    rows = conn.execute(
        "SELECT player_id, half_veto_count FROM cup_players WHERE cup_id = ? ORDER BY player_id",
        (cup_id,),
    ).fetchall()
    conn.close()
    return {r["player_id"]: r["half_veto_count"] for r in rows}


def _record_race(client, cup_id, map_name):
    return client.post(f"/cup-session/{cup_id}/next-race", json={"map": map_name})


def _play_four_races(client, cup_id=1):
    for m in ["Coconut Mall", "Rainbow Road", "Moo Moo Meadows", "Koopa Cape"]:
        client.post(f"/cup-session/{cup_id}/next-race", json={"map": m})


def _submit_live_roster(client, cup_id, score_by_pid, notes=""):
    """Submit the completion form for exactly the cup's LIVE roster.

    Mirrors what cup_session_complete renders (players come from cup_players),
    so this exercises "the live roster drives completion".
    """
    conn = get_connection()
    pids = [
        r["player_id"]
        for r in conn.execute(
            "SELECT cp.player_id FROM cup_players cp JOIN players p ON cp.player_id = p.id "
            "WHERE cp.cup_id = ? ORDER BY p.name",
            (cup_id,),
        ).fetchall()
    ]
    conn.close()
    data = {
        "notes": notes,
        "tz_offset": "",
        "player_ids[]": [str(p) for p in pids],
        "scores[]": [str(score_by_pid[p]) for p in pids],
        "lines[]": ["0"] * len(pids),
    }
    return client.post(
        f"/cup-session/{cup_id}/complete", data=data, follow_redirects=True
    )


# =============================================================================
# Add
# =============================================================================


def test_add_player_to_in_progress_cup(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = _add(client, 1, 3)
    assert resp.status_code == 200
    roster = _roster()
    assert set(roster) == {1, 2, 3}
    assert roster[3] == 0  # new player starts with all half-vetoes


def test_add_player_reflected_on_race_page(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    _add(client, 1, 3)
    page = client.get("/cup-session/1").data
    assert b"Carol" in page  # player 3


def test_add_duplicate_player_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = _add(client, 1, 2)  # already in cup
    assert resp.status_code == 200
    assert b"already in this cup" in resp.data
    assert set(_roster()) == {1, 2}  # unchanged, no dup


def test_add_nonexistent_player_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = _add(client, 1, 999)
    assert resp.status_code == 200
    assert b"doesn&#39;t exist" in resp.data or b"doesn't exist" in resp.data
    assert set(_roster()) == {1, 2}


def test_add_garbage_player_id_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = client.post(
        "/cup-session/1/players/add",
        data={"player_id": "not-a-number"},
        follow_redirects=True,
    )
    assert resp.status_code == 200  # friendly, never 500
    assert b"Invalid player selection" in resp.data
    assert set(_roster()) == {1, 2}


def test_add_oversized_cup_id_404(client):
    # BoundedIntConverter rejects an id past SQLite's 64-bit range -> 404, not 500.
    resp = client.post(
        "/cup-session/99999999999999999999999/players/add",
        data={"player_id": "1"},
    )
    assert resp.status_code == 404


def test_add_to_nonexistent_cup_rejected(client):
    _setup_players(client)
    resp = _add(client, 12345, 1)  # valid int, no such cup
    assert resp.status_code == 200  # friendly redirect, never 500
    assert b"in progress" in resp.data


def test_add_to_completed_cup_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    _play_four_races(client)
    _submit_live_roster(client, 1, {1: 100, 2: 80})
    resp = _add(client, 1, 3)
    assert resp.status_code == 200
    assert b"in progress" in resp.data
    # scores were written for the 2-player roster; no 3rd player sneaked in
    assert set(_roster()) == {1, 2}


def test_add_to_cancelled_cup_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    client.post("/cup-session/1/cancel")
    resp = _add(client, 1, 3)
    assert resp.status_code == 200
    assert b"in progress" in resp.data
    assert set(_roster()) == {1, 2}


def test_add_to_soft_deleted_cup_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    conn = get_connection()
    conn.execute("UPDATE cups SET deleted_at = '2020-01-01 00:00:00' WHERE id = 1")
    conn.commit()
    conn.close()
    resp = _add(client, 1, 3)
    assert resp.status_code == 200
    assert b"in progress" in resp.data
    assert set(_roster()) == {1, 2}  # deleted_at guard blocked the insert


# =============================================================================
# Remove
# =============================================================================


def test_remove_player_from_in_progress_cup(client):
    _setup_players(client)
    _create_session(client, [1, 2, 3])
    resp = _remove(client, 1, 3)
    assert resp.status_code == 200
    assert set(_roster()) == {1, 2}


def test_remove_reflected_on_race_page(client):
    _setup_players(client)
    _create_session(client, [1, 2, 3])
    _remove(client, 1, 3)
    page = client.get("/cup-session/1").data
    # Carol (3) removed; the veto-status line should no longer carry her count.
    # She reappears in the "add" dropdown, so assert on the roster, not raw name.
    assert set(_roster()) == {1, 2}
    assert b"Carol" in page  # now offered as an add candidate


def test_remove_below_minimum_rejected(client):
    # MIN_ROSTER_SIZE == 1 (mirrors cup creation's "at least one player").
    _setup_players(client)
    _create_session(client, [1])  # single-player cup is allowed at creation
    resp = _remove(client, 1, 1)
    assert resp.status_code == 200
    assert b"at least 1 player" in resp.data
    assert set(_roster()) == {1}  # last player NOT removed


def test_remove_second_to_last_allowed_then_last_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    assert _remove(client, 1, 2).status_code == 200
    assert set(_roster()) == {1}
    resp = _remove(client, 1, 1)  # would empty the cup
    assert b"at least 1 player" in resp.data
    assert set(_roster()) == {1}


def test_remove_player_not_in_cup_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = _remove(client, 1, 3)  # exists but not in this cup
    assert resp.status_code == 200
    assert b"isn&#39;t in this cup" in resp.data or b"isn't in this cup" in resp.data
    assert set(_roster()) == {1, 2}


def test_remove_nonexistent_player_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = _remove(client, 1, 999)
    assert resp.status_code == 200
    assert set(_roster()) == {1, 2}


def test_remove_garbage_player_id_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2])
    resp = client.post(
        "/cup-session/1/players/remove",
        data={"player_id": "xyz"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid player selection" in resp.data
    assert set(_roster()) == {1, 2}


def test_remove_oversized_cup_id_404(client):
    resp = client.post(
        "/cup-session/99999999999999999999999/players/remove",
        data={"player_id": "1"},
    )
    assert resp.status_code == 404


def test_remove_from_completed_cup_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2, 3])
    _play_four_races(client)
    _submit_live_roster(client, 1, {1: 100, 2: 80, 3: 60})
    resp = _remove(client, 1, 3)
    assert resp.status_code == 200
    assert b"in progress" in resp.data
    assert set(_roster()) == {1, 2, 3}


def test_remove_from_cancelled_cup_rejected(client):
    _setup_players(client)
    _create_session(client, [1, 2, 3])
    client.post("/cup-session/1/cancel")
    resp = _remove(client, 1, 3)
    assert resp.status_code == 200
    assert b"in progress" in resp.data
    assert set(_roster()) == {1, 2, 3}


# =============================================================================
# Completion uses the LIVE roster after a mid-cup change
# =============================================================================


def test_complete_after_removal_3_to_2_skips_line_adjustment(client):
    # A 3-player Wii cup dropped to 2 must complete as a 2-player cup: scores
    # save for the final roster and the 3-player line adjustment does NOT fire.
    _setup_players(client, has_line=True)
    _create_session(client, [1, 2, 3])
    _play_four_races(client)
    _remove(client, 1, 3)  # 3 -> 2
    # Completion page reflects the live roster (Carol gone).
    _submit_live_roster(client, 1, {1: 100, 2: 80})
    conn = get_connection()
    status = conn.execute("SELECT status FROM cups WHERE id = 1").fetchone()["status"]
    score_pids = {
        r["player_id"]
        for r in conn.execute("SELECT player_id FROM scores WHERE cup_id = 1").fetchall()
    }
    lc = conn.execute(
        "SELECT COUNT(*) AS n FROM line_changes WHERE cup_id = 1"
    ).fetchone()["n"]
    lines = {
        r["id"]: r["line"]
        for r in conn.execute("SELECT id, line FROM players").fetchall()
    }
    conn.close()
    assert status == "completed"
    assert score_pids == {1, 2}  # only the final roster got scores
    assert lc == 0  # 2 players -> no 3-player line adjustment
    assert lines[1] == 0 and lines[2] == 0 and lines[3] == 0  # unchanged


def test_complete_after_add_2_to_3_applies_line_adjustment(client):
    # A cup that started with 2 players and grew to 3 must complete as a
    # 3-player Wii cup: the line adjustment fires off the final roster.
    _setup_players(client, has_line=True)
    _create_session(client, [1, 2])
    _play_four_races(client)
    _add(client, 1, 3)  # 2 -> 3
    _submit_live_roster(client, 1, {1: 100, 2: 80, 3: 60})
    conn = get_connection()
    lc = conn.execute(
        "SELECT COUNT(*) AS n FROM line_changes WHERE cup_id = 1"
    ).fetchone()["n"]
    lines = {
        r["id"]: r["line"]
        for r in conn.execute("SELECT id, line FROM players").fetchall()
    }
    score_pids = {
        r["player_id"]
        for r in conn.execute("SELECT player_id FROM scores WHERE cup_id = 1").fetchall()
    }
    conn.close()
    assert score_pids == {1, 2, 3}
    assert lc == 3  # 3-player Wii -> line adjustments applied
    assert lines[1] == -3  # Alice 1st
    assert lines[2] == 0  # Bob 2nd
    assert lines[3] == 3  # Carol 3rd


# =============================================================================
# Stale-veto forfeit does not retroactively hit a late-added player
# =============================================================================


def test_late_added_player_not_stale_veto_forfeited(client):
    # Play into race 3 so the original roster forfeits, THEN add a player. The
    # late-add must keep half_veto_count == 0 (all vetoes intact) — the one-time
    # forfeit already fired and does not re-run for the newcomer.
    _setup_players(client)
    _create_session(client, [1, 2])
    _record_race(client, 1, "Coconut Mall")  # race 1
    _record_race(client, 1, "Rainbow Road")  # race 2 -> entering race 3 -> forfeit
    assert _roster() == {1: 1, 2: 1}  # originals forfeited one each
    _add(client, 1, 3)  # late add, after the race-3 check
    roster = _roster()
    assert roster[3] == 0  # newcomer NOT retroactively forfeited
    assert roster[1] == 1 and roster[2] == 1  # originals unchanged


def test_add_then_next_race_does_not_reforfeit(client):
    # Adding a player mid-cup and playing on must not re-trigger the one-time
    # forfeit for anyone.
    _setup_players(client)
    _create_session(client, [1, 2])
    _record_race(client, 1, "Coconut Mall")
    _record_race(client, 1, "Rainbow Road")  # forfeit fires (originals -> 1)
    _add(client, 1, 3)
    _record_race(client, 1, "Moo Moo Meadows")  # race 3 -> no re-check
    roster = _roster()
    assert roster == {1: 1, 2: 1, 3: 0}
