import json

from db import get_connection
from helpers import create_player


def _create_session(client, player_ids=None, edition=None):
    """Create a cup session with the given player IDs. Returns redirect response."""
    if player_ids is None:
        player_ids = ["1", "2"]
    data = {"player_ids[]": player_ids}
    if edition is not None:
        data["game_edition"] = edition
    return client.post(
        "/cup-session/new",
        data=data,
        follow_redirects=False,
    )


def _setup_players(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")


def _play_four_races(client, cup_id=1):
    """Record 4 races for a cup session."""
    maps = ["Coconut Mall", "Rainbow Road", "Moo Moo Meadows", "Koopa Cape"]
    for m in maps:
        client.post(f"/cup-session/{cup_id}/next-race", json={"map": m})
    return maps


def _submit_scores(client, cup_id=1, player_ids=None, scores=None, lines=None, notes="", tiebreaker_ids=None):
    """Submit scores for a completed session."""
    if player_ids is None:
        player_ids = ["1", "2"]
    if scores is None:
        scores = ["100", "80"]
    if lines is None:
        lines = ["0"] * len(player_ids)
    data = {
        "notes": notes,
        "tz_offset": "",
        "player_ids[]": player_ids,
        "scores[]": scores,
        "lines[]": lines,
    }
    if tiebreaker_ids:
        data["tiebreakers[]"] = [str(pid) for pid in tiebreaker_ids]
    return client.post(
        f"/cup-session/{cup_id}/complete",
        data=data,
        follow_redirects=True,
    )


# =============================================================================
# Maps data
# =============================================================================


def test_wii_edition_has_32_courses():
    from maps import TRACK_SETS
    assert len(TRACK_SETS["wii"]) == 32


def test_mk8dx_edition_has_96_courses():
    from maps import TRACK_SETS
    assert len(TRACK_SETS["mk8dx"]) == 96


def test_courses_backward_compat_alias():
    # COURSES stays a flat alias of the Wii list for older importers.
    from maps import COURSES, TRACK_SETS
    assert COURSES == TRACK_SETS["wii"]


def test_no_edition_has_duplicate_courses():
    from maps import TRACK_SETS
    for edition, courses in TRACK_SETS.items():
        assert len(courses) == len(set(courses)), f"duplicates in {edition}"


def test_new_session_defaults_to_wii(client):
    _setup_players(client)
    _create_session(client)
    conn = get_connection()
    row = conn.execute("SELECT game_edition FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert row["game_edition"] == "wii"


def test_mk8dx_session_stores_edition_and_uses_96_tracks(client):
    from maps import TRACK_SETS
    _setup_players(client)
    _create_session(client, edition="mk8dx")

    conn = get_connection()
    row = conn.execute("SELECT game_edition FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert row["game_edition"] == "mk8dx"

    # The race page should surface the MK8DX track set, not the Wii one.
    # (Tracks with apostrophes are HTML-escaped in the page, so only assert on
    # distinctive apostrophe-free names; the 96-count is covered separately.)
    page = client.get("/cup-session/1").get_data(as_text=True)
    for track in ["Ninja Hideaway", "Sky-High Sundae", "Merry Mountain", "Mount Wario"]:
        assert track in page, f"missing MK8DX track on race page: {track}"
    assert "Luigi Circuit" not in page  # Wii-only track must not appear


def test_invalid_edition_falls_back_to_wii(client):
    _setup_players(client)
    _create_session(client, edition="not-a-real-edition")
    conn = get_connection()
    row = conn.execute("SELECT game_edition FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert row["game_edition"] == "wii"


def test_spin_returns_mk8dx_track_for_mk8dx_session(client):
    from maps import TRACK_SETS
    _setup_players(client)
    _create_session(client, edition="mk8dx")
    resp = client.post("/cup-session/1/spin")
    data = json.loads(resp.get_data(as_text=True))
    assert data["map"] in TRACK_SETS["mk8dx"]
    assert TRACK_SETS["mk8dx"][data["index"]] == data["map"]


# =============================================================================
# Setup page (GET /cup-session/new)
# =============================================================================


def test_cup_session_new_page_loads(client):
    response = client.get("/cup-session/new")
    assert response.status_code == 200
    assert b"Start Cup" in response.data


def test_cup_session_new_shows_default_players(client):
    _setup_players(client)
    response = client.get("/cup-session/new")
    assert b"Alice" in response.data
    assert b"Bob" in response.data


def test_cup_session_new_only_shows_default_players_checked(client):
    create_player(client, "Alice", default_cup=True)
    create_player(client, "Bob", default_cup=False)
    response = client.get("/cup-session/new")
    data = response.data.decode()
    # Alice should be in the player list (checked), Bob should be in the dropdown
    assert 'value="1" checked' in data
    assert 'value="2" checked' not in data


def test_cup_session_new_shows_player_lines(client):
    create_player(client, "Alice", has_line=True)
    # Set Alice's line to +5
    conn = get_connection()
    conn.execute("UPDATE players SET line = 5 WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.get("/cup-session/new")
    assert b"Line: +5" in response.data


def test_cup_session_new_redirects_if_in_progress(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/cup-session/new", follow_redirects=False)
    assert response.status_code == 302
    assert "/cup-session/" in response.headers["Location"]


# =============================================================================
# Create session (POST /cup-session/new)
# =============================================================================


def test_create_session(client):
    _setup_players(client)
    response = _create_session(client, ["1", "2"])
    assert response.status_code == 302
    conn = get_connection()
    cup = conn.execute("SELECT status, voto_count FROM cups WHERE id = 1").fetchone()
    assert cup["status"] == "in_progress"
    assert cup["voto_count"] == 0
    conn.close()


def test_create_session_stores_cup_players(client):
    _setup_players(client)
    _create_session(client, ["1", "2", "3"])
    conn = get_connection()
    players = conn.execute(
        "SELECT player_id, half_veto_count FROM cup_players WHERE cup_id = 1 ORDER BY player_id"
    ).fetchall()
    conn.close()
    assert len(players) == 3
    assert all(p["half_veto_count"] == 0 for p in players)


def test_create_session_sets_date(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    conn = get_connection()
    cup = conn.execute("SELECT date FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["date"] is not None


def test_create_session_no_players_rejected(client):
    response = client.post("/cup-session/new", data={}, follow_redirects=True)
    assert b"Select at least one player" in response.data


def test_create_session_blocked_by_existing(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = _create_session(client, ["1", "2"])
    assert response.status_code == 302
    # Only one cup should exist
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) as cnt FROM cups").fetchone()["cnt"]
    conn.close()
    assert count == 1


def test_create_session_blocked_redirects_to_race_page(client):
    """Attempting to create a second session redirects to the existing one."""
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post(
        "/cup-session/new",
        data={"player_ids[]": ["1", "2"]},
        follow_redirects=True,
    )
    # Ends up on the race page for the existing session
    assert b"Cup Session" in response.data
    assert b"Race 1 of 4" in response.data


def test_create_session_with_single_player(client):
    create_player(client, "Alice")
    response = _create_session(client, ["1"])
    assert response.status_code == 302
    conn = get_connection()
    players = conn.execute("SELECT * FROM cup_players WHERE cup_id = 1").fetchall()
    conn.close()
    assert len(players) == 1


# =============================================================================
# Race page (GET /cup-session/<id>)
# =============================================================================


def test_race_page_loads(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/cup-session/1")
    assert response.status_code == 200
    assert b"Race 1 of 4" in response.data


def test_race_page_shows_player_names(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/cup-session/1")
    assert b"Alice" in response.data
    assert b"Bob" in response.data


def test_race_page_shows_veto_status(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/cup-session/1")
    assert b"Votoes remaining" in response.data
    assert b"Half Vetoes" in response.data


def test_race_page_updates_after_race(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    response = client.get("/cup-session/1")
    assert b"Race 2 of 4" in response.data
    assert b"Coconut Mall" in response.data


def test_race_page_shows_completed_races(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    client.post("/cup-session/1/next-race", json={"map": "Rainbow Road"})
    response = client.get("/cup-session/1")
    data = response.data.decode()
    assert "Coconut Mall" in data
    assert "Rainbow Road" in data
    assert "Race 3 of 4" in data


def test_race_page_after_all_races_shows_enter_scores(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = client.get("/cup-session/1")
    assert b"Enter Scores" in response.data
    assert b"All races complete" in response.data


def test_race_page_404_for_completed_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    conn = get_connection()
    conn.execute("UPDATE cups SET status = 'completed' WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.get("/cup-session/1")
    assert response.status_code == 404


def test_race_page_404_for_cancelled_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    response = client.get("/cup-session/1")
    assert response.status_code == 404


def test_race_page_404_for_nonexistent_cup(client):
    response = client.get("/cup-session/999")
    assert response.status_code == 404


# =============================================================================
# Spin (POST /cup-session/<id>/spin)
# =============================================================================


def test_spin_returns_valid_map(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/spin")
    data = json.loads(response.data)
    assert "map" in data
    assert "index" in data
    from maps import COURSES
    assert data["map"] in COURSES


def test_spin_returns_correct_index(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/spin")
    data = json.loads(response.data)
    from maps import COURSES
    assert COURSES[data["index"]] == data["map"]


def test_spin_excludes_played_maps(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    # Spin many times — Coconut Mall should never appear
    for _ in range(50):
        response = client.post("/cup-session/1/spin")
        data = json.loads(response.data)
        assert data["map"] != "Coconut Mall"


def test_spin_does_not_exclude_vetoed_maps(client):
    """Vetoed maps can still be spun — only played maps are excluded."""
    _setup_players(client)
    _create_session(client, ["1", "2"])
    # Use a voto (doesn't play the map, just skips it)
    client.post("/cup-session/1/voto")
    # Spin should still return any of the 32 maps since nothing is played
    seen = set()
    for _ in range(200):
        response = client.post("/cup-session/1/spin")
        data = json.loads(response.data)
        seen.add(data["map"])
    # Should have seen more than 28 unique maps (statistically certain)
    assert len(seen) > 28


def test_spin_after_all_races_rejected(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = client.post("/cup-session/1/spin")
    assert response.status_code == 400
    data = json.loads(response.data)
    assert "All races completed" in data["error"]


def test_spin_404_for_nonexistent_cup(client):
    response = client.post("/cup-session/999/spin")
    assert response.status_code == 404


def test_spin_404_for_completed_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    conn = get_connection()
    conn.execute("UPDATE cups SET status = 'completed' WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.post("/cup-session/1/spin")
    assert response.status_code == 404


def test_spin_404_for_cancelled_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    response = client.post("/cup-session/1/spin")
    assert response.status_code == 404


# =============================================================================
# Half Veto (POST /cup-session/<id>/half-veto)
# =============================================================================


def test_half_veto_decrements(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/half-veto", json={"player_id": 1})
    data = json.loads(response.data)
    assert "success" in data
    assert data["remaining"] == 2


def test_half_veto_returns_boolean_success(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/half-veto", json={"player_id": 1})
    data = json.loads(response.data)
    assert isinstance(data["success"], bool)


def test_half_veto_persists_count(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/half-veto", json={"player_id": 1})
    client.post("/cup-session/1/half-veto", json={"player_id": 1})
    conn = get_connection()
    cp = conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = 1 AND player_id = 1"
    ).fetchone()
    conn.close()
    assert cp["half_veto_count"] == 2


def test_half_veto_independent_per_player(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    # Use all 3 vetoes for player 1
    for _ in range(3):
        client.post("/cup-session/1/half-veto", json={"player_id": 1})
    # Player 2 should still have all 3
    response = client.post("/cup-session/1/half-veto", json={"player_id": 2})
    data = json.loads(response.data)
    assert response.status_code == 200
    assert data["remaining"] == 2


def test_half_veto_limit(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    for _ in range(3):
        client.post("/cup-session/1/half-veto", json={"player_id": 1})
    response = client.post("/cup-session/1/half-veto", json={"player_id": 1})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert "No half vetoes remaining" in data["error"]


def test_half_veto_remaining_counts_down(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    for expected_remaining in [2, 1, 0]:
        response = client.post("/cup-session/1/half-veto", json={"player_id": 1})
        data = json.loads(response.data)
        assert data["remaining"] == expected_remaining


def test_half_veto_non_player_override(client):
    """Half veto without player_id (non-player override) works and doesn't decrement anyone."""
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/half-veto", json={})
    data = json.loads(response.data)
    assert "success" in data
    assert data["remaining"] is None
    # No player counts should change
    conn = get_connection()
    counts = conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = 1"
    ).fetchall()
    conn.close()
    assert all(c["half_veto_count"] == 0 for c in counts)


def test_half_veto_player_not_in_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    # Player 3 (Carol) is not in this cup
    response = client.post("/cup-session/1/half-veto", json={"player_id": 3})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert "Player not in this cup" in data["error"]


def test_half_veto_404_for_nonexistent_cup(client):
    response = client.post("/cup-session/999/half-veto", json={"player_id": 1})
    assert response.status_code == 404


def test_half_veto_404_for_completed_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    conn = get_connection()
    conn.execute("UPDATE cups SET status = 'completed' WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.post("/cup-session/1/half-veto", json={"player_id": 1})
    assert response.status_code == 404


def test_half_veto_coin_flip_is_random(client):
    """Over many tries, the coin flip should produce both outcomes."""
    _setup_players(client)
    _create_session(client, ["1", "2", "3"])
    outcomes = set()
    # Use non-player override so we don't hit the 3-veto limit
    for _ in range(20):
        response = client.post("/cup-session/1/half-veto", json={})
        data = json.loads(response.data)
        outcomes.add(data["success"])
        if len(outcomes) == 2:
            break
    assert True in outcomes and False in outcomes


# =============================================================================
# Voto (POST /cup-session/<id>/voto)
# =============================================================================


def test_voto_decrements(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/voto")
    data = json.loads(response.data)
    assert data["remaining"] == 3


def test_voto_remaining_counts_down(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    for expected_remaining in [3, 2, 1, 0]:
        response = client.post("/cup-session/1/voto")
        data = json.loads(response.data)
        assert data["remaining"] == expected_remaining


def test_voto_limit(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    for _ in range(4):
        client.post("/cup-session/1/voto")
    response = client.post("/cup-session/1/voto")
    assert response.status_code == 400
    data = json.loads(response.data)
    assert "No votoes remaining" in data["error"]


def test_voto_persists_count(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/voto")
    client.post("/cup-session/1/voto")
    conn = get_connection()
    cup = conn.execute("SELECT voto_count FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["voto_count"] == 2


def test_voto_404_for_nonexistent_cup(client):
    response = client.post("/cup-session/999/voto")
    assert response.status_code == 404


def test_voto_404_for_cancelled_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    response = client.post("/cup-session/1/voto")
    assert response.status_code == 404


# =============================================================================
# Next Race (POST /cup-session/<id>/next-race)
# =============================================================================


def test_next_race_first(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    data = json.loads(response.data)
    assert data["race_number"] == 1
    assert data["complete"] is False


def test_next_race_increments(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    maps = ["Coconut Mall", "Rainbow Road", "Moo Moo Meadows"]
    for i, m in enumerate(maps):
        response = client.post("/cup-session/1/next-race", json={"map": m})
        data = json.loads(response.data)
        assert data["race_number"] == i + 1


def test_next_race_fourth_signals_complete(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    maps = ["Coconut Mall", "Rainbow Road", "Moo Moo Meadows", "Koopa Cape"]
    for m in maps:
        response = client.post("/cup-session/1/next-race", json={"map": m})
    data = json.loads(response.data)
    assert data["race_number"] == 4
    assert data["complete"] is True


def test_next_race_stores_in_db(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    conn = get_connection()
    race = conn.execute(
        "SELECT race_number, map FROM races WHERE cup_id = 1"
    ).fetchone()
    conn.close()
    assert race["race_number"] == 1
    assert race["map"] == "Coconut Mall"


def test_next_race_fifth_rejected(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = client.post("/cup-session/1/next-race", json={"map": "DK Summit"})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert "All races completed" in data["error"]


def test_next_race_no_map_rejected(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/next-race", json={})
    assert response.status_code == 400


def test_next_race_empty_body_rejected(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post(
        "/cup-session/1/next-race",
        content_type="application/json",
    )
    assert response.status_code == 400


def test_next_race_404_for_nonexistent_cup(client):
    response = client.post("/cup-session/999/next-race", json={"map": "Coconut Mall"})
    assert response.status_code == 404


def test_next_race_404_for_completed_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    conn = get_connection()
    conn.execute("UPDATE cups SET status = 'completed' WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    assert response.status_code == 404


def test_next_race_allows_duplicate_map_name(client):
    """Manual override can play the same map twice — only race_number is unique per cup."""
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    response = client.post("/cup-session/1/next-race", json={"map": "Coconut Mall"})
    data = json.loads(response.data)
    assert data["race_number"] == 2


# =============================================================================
# Complete page (GET /cup-session/<id>/complete)
# =============================================================================


def test_complete_page_loads(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = client.get("/cup-session/1/complete")
    assert response.status_code == 200
    assert b"Complete Cup" in response.data


def test_complete_page_shows_races(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    maps = _play_four_races(client)
    response = client.get("/cup-session/1/complete")
    for m in maps:
        assert m.encode() in response.data


def test_complete_page_shows_players(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = client.get("/cup-session/1/complete")
    assert b"Alice" in response.data
    assert b"Bob" in response.data


def test_complete_page_404_for_nonexistent_cup(client):
    response = client.get("/cup-session/999/complete")
    assert response.status_code == 404


def test_complete_page_404_for_cancelled_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    response = client.get("/cup-session/1/complete")
    assert response.status_code == 404


def test_complete_page_accessible_for_in_progress(client):
    """Complete page is accessible even before 4 races (for edge cases)."""
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/cup-session/1/complete")
    assert response.status_code == 200


# =============================================================================
# Submit cup (POST /cup-session/<id>/complete)
# =============================================================================


def test_submit_cup_creates_scores(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    _submit_scores(client)
    conn = get_connection()
    scores = conn.execute(
        "SELECT player_id, score FROM scores WHERE cup_id = 1 ORDER BY player_id"
    ).fetchall()
    conn.close()
    assert len(scores) == 2
    assert scores[0]["score"] == 100
    assert scores[1]["score"] == 80


def test_submit_cup_sets_completed(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    _submit_scores(client)
    conn = get_connection()
    cup = conn.execute("SELECT status FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["status"] == "completed"


def test_submit_cup_with_notes(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = _submit_scores(client, notes="Great game night")
    assert b"Great game night" in response.data


def test_submit_cup_with_date(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    client.post(
        "/cup-session/1/complete",
        data={
            "date": "2026-04-01T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "lines[]": ["0", "0"],
        },
        follow_redirects=True,
    )
    conn = get_connection()
    cup = conn.execute("SELECT date FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert "2026-04-01" in cup["date"]


def test_submit_cup_with_date_and_timezone(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    client.post(
        "/cup-session/1/complete",
        data={
            "date": "2026-04-01T20:00",
            "notes": "",
            "tz_offset": "300",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "lines[]": ["0", "0"],
        },
    )
    conn = get_connection()
    cup = conn.execute("SELECT date FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert "2026-04-02 01:00" in cup["date"]


def test_submit_cup_with_line_adjustments(client):
    create_player(client, "Alice", has_line=True)
    create_player(client, "Bob", has_line=True)
    create_player(client, "Carol", has_line=True)
    _create_session(client, ["1", "2", "3"])
    _play_four_races(client)
    _submit_scores(
        client,
        player_ids=["1", "2", "3"],
        scores=["100", "80", "60"],
        lines=["0", "0", "0"],
    )
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -3  # 1st place
    assert bob["line"] == 0    # 2nd place
    assert carol["line"] == 3   # 3rd place


def test_submit_cup_line_adjustment_flash(client):
    create_player(client, "Alice", has_line=True)
    create_player(client, "Bob", has_line=True)
    create_player(client, "Carol", has_line=True)
    _create_session(client, ["1", "2", "3"])
    _play_four_races(client)
    response = _submit_scores(
        client,
        player_ids=["1", "2", "3"],
        scores=["100", "80", "60"],
        lines=["0", "0", "0"],
    )
    assert b"Lines adjusted" in response.data


def test_submit_cup_no_scores_rejected(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = client.post(
        "/cup-session/1/complete",
        data={"notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"At least one player must have a score" in response.data
    # Cup should still be in_progress
    conn = get_connection()
    cup = conn.execute("SELECT status FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["status"] == "in_progress"


def test_submit_cup_tiebreaker_validation(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = _submit_scores(
        client,
        player_ids=["1", "2"],
        scores=["100", "80"],
        tiebreaker_ids=[1],  # No tie exists
    )
    assert b"share their score" in response.data


def test_submit_cup_with_valid_tiebreaker(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    _submit_scores(
        client,
        player_ids=["1", "2"],
        scores=["100", "100"],
        tiebreaker_ids=[1],
    )
    conn = get_connection()
    winner = conn.execute(
        "SELECT won_tiebreaker FROM scores WHERE cup_id = 1 AND player_id = 1"
    ).fetchone()
    conn.close()
    assert winner["won_tiebreaker"] == 1


def test_submit_cup_404_for_completed_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    _submit_scores(client)
    # Try to submit again
    response = client.post(
        "/cup-session/1/complete",
        data={
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["200", "150"],
            "lines[]": ["0", "0"],
        },
    )
    assert response.status_code == 404


def test_submit_cup_edits_races(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    client.post(
        "/cup-session/1/complete",
        data={
            "race_1": "DK Summit",
            "race_2": "Daisy Circuit",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "lines[]": ["0", "0"],
        },
    )
    conn = get_connection()
    races = conn.execute(
        "SELECT race_number, map FROM races WHERE cup_id = 1 ORDER BY race_number"
    ).fetchall()
    conn.close()
    assert races[0]["map"] == "DK Summit"
    assert races[1]["map"] == "Daisy Circuit"
    # Races 3 and 4 should be unchanged
    assert races[2]["map"] == "Moo Moo Meadows"
    assert races[3]["map"] == "Koopa Cape"


def test_submit_cup_appears_in_cups_list(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    response = _submit_scores(client, notes="Cup from session")
    assert b"Cup from session" in response.data


# =============================================================================
# Cancel (POST /cup-session/<id>/cancel)
# =============================================================================


def test_cancel_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/cancel", follow_redirects=False)
    assert response.status_code == 302
    conn = get_connection()
    cup = conn.execute("SELECT status FROM cups WHERE id = 1").fetchone()
    assert cup["status"] == "cancelled"
    conn.close()


def test_cancel_redirects_to_index(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.post("/cup-session/1/cancel", follow_redirects=False)
    assert response.headers["Location"].endswith("/")


def test_cancelled_cup_not_in_cups_list(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    response = client.get("/cups")
    assert b"No cups yet" in response.data


def test_cancel_nonexistent_cup_is_noop(client):
    """Cancelling a nonexistent cup is a no-op — just redirects."""
    response = client.post("/cup-session/999/cancel", follow_redirects=False)
    assert response.status_code == 302


def test_cancel_already_completed_cup_is_noop(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    _submit_scores(client)
    client.post("/cup-session/1/cancel")
    # Status should still be completed
    conn = get_connection()
    cup = conn.execute("SELECT status FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["status"] == "completed"


def test_can_start_new_session_after_cancel(client):
    """After cancelling, the setup page loads (not redirected to old session)."""
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    # The setup page should load without redirecting (no in-progress cup)
    response = client.get("/cup-session/new")
    assert response.status_code == 200
    assert b"Start Cup" in response.data


# =============================================================================
# Index page integration
# =============================================================================


def test_index_shows_begin_new_cup(client):
    response = client.get("/")
    assert b"Begin New Cup" in response.data
    assert b"Continue Cup" not in response.data


def test_index_shows_continue_cup(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/")
    assert b"Continue Cup" in response.data
    assert b"Begin New Cup" not in response.data


def test_index_shows_begin_after_cancel(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    client.post("/cup-session/1/cancel")
    response = client.get("/")
    assert b"Begin New Cup" in response.data
    assert b"Continue Cup" not in response.data


def test_index_shows_begin_after_complete(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    _play_four_races(client)
    _submit_scores(client)
    response = client.get("/")
    assert b"Begin New Cup" in response.data
    assert b"Continue Cup" not in response.data


def test_in_progress_cup_hidden_from_cups_list(client):
    _setup_players(client)
    _create_session(client, ["1", "2"])
    response = client.get("/cups")
    assert b"No cups yet" in response.data


# =============================================================================
# Existing functionality unaffected
# =============================================================================


def test_manual_cup_creation_still_works(client):
    """The existing /cups POST (manual backdoor) still creates completed cups."""
    _setup_players(client)
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "manual cup",
            "tz_offset": "",
            "player_ids[]": ["1"],
            "scores[]": ["100"],
            "lines[]": ["0"],
        },
        follow_redirects=True,
    )
    assert b"manual cup" in response.data
    conn = get_connection()
    cup = conn.execute("SELECT status FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["status"] == "completed"


def test_session_and_manual_cups_coexist(client):
    """A completed session cup and a manual cup both show in the cups list."""
    _setup_players(client)
    # Manual cup
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "manual cup",
            "tz_offset": "",
            "player_ids[]": ["1"],
            "scores[]": ["100"],
            "lines[]": ["0"],
        },
    )
    # Session cup
    _create_session(client, ["1", "2"])
    _play_four_races(client, cup_id=2)
    _submit_scores(client, cup_id=2, notes="session cup")
    response = client.get("/cups")
    data = response.data.decode()
    assert "manual cup" in data
    assert "session cup" in data
