import os

import pytest

from app import app, calculate_placements, format_line
from db import get_connection, init_db


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = db_path
    init_db(db_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
    del os.environ["DB_PATH"]


def create_player(client, name, has_line=True):
    data = {"name": name, "default_cup": "on"}
    if has_line:
        data["has_line"] = "on"
    return client.post("/players", data=data, follow_redirects=True)


def create_cup_with_scores(client, date, player_scores, lines=None, tiebreaker_ids=None):
    """Create a cup with multiple players and scores.

    player_scores: list of (player_id, score) tuples.
    lines: optional list of line values per player. If None, fetches from DB.
    tiebreaker_ids: optional list of player_ids who won tiebreakers.
    """
    player_ids = [str(pid) for pid, _ in player_scores]
    if lines is None:
        conn = get_connection()
        lines = []
        for pid, _ in player_scores:
            row = conn.execute("SELECT line FROM players WHERE id = ?", (pid,)).fetchone()
            lines.append(str(row["line"]) if row else "0")
        conn.close()
    else:
        lines = [str(l) for l in lines]
    data = {
        "date": date,
        "notes": "",
        "tz_offset": "",
        "player_ids[]": player_ids,
        "scores[]": [str(score) for _, score in player_scores],
        "lines[]": lines,
    }
    if tiebreaker_ids:
        data["tiebreakers[]"] = [str(pid) for pid in tiebreaker_ids]
    return client.post("/cups", data=data, follow_redirects=True)


# --- format_line ---


def test_format_line_positive():
    assert format_line(3) == "+3"


def test_format_line_zero():
    assert format_line(0) == "+0"


def test_format_line_negative():
    assert format_line(-5) == "-5"


# --- calculate_placements ---


def test_placements_basic():
    scores = [
        {"player_id": 1, "score": 100, "line": 0, "won_tiebreaker": False},
        {"player_id": 2, "score": 80, "line": 0, "won_tiebreaker": False},
        {"player_id": 3, "score": 60, "line": 0, "won_tiebreaker": False},
    ]
    result = calculate_placements(scores)
    assert result[0]["player_id"] == 1
    assert result[0]["placement"] == 1
    assert result[1]["placement"] == 2
    assert result[2]["placement"] == 3


def test_placements_line_changes_order():
    """Player with lower raw score but positive line can place higher."""
    scores = [
        {"player_id": 1, "score": 80, "line": 0, "won_tiebreaker": False},
        {"player_id": 2, "score": 70, "line": 15, "won_tiebreaker": False},
        {"player_id": 3, "score": 60, "line": 0, "won_tiebreaker": False},
    ]
    result = calculate_placements(scores)
    assert result[0]["player_id"] == 2  # 70 + 15 = 85
    assert result[1]["player_id"] == 1  # 80 + 0 = 80
    assert result[2]["player_id"] == 3  # 60 + 0 = 60


def test_placements_tie_with_tiebreaker():
    scores = [
        {"player_id": 1, "score": 100, "line": 0, "won_tiebreaker": True},
        {"player_id": 2, "score": 100, "line": 0, "won_tiebreaker": False},
        {"player_id": 3, "score": 60, "line": 0, "won_tiebreaker": False},
    ]
    result = calculate_placements(scores)
    assert result[0]["player_id"] == 1
    assert result[0]["placement"] == 1
    assert result[1]["player_id"] == 2
    assert result[1]["placement"] == 2
    assert result[2]["placement"] == 3


def test_placements_tie_without_tiebreaker():
    scores = [
        {"player_id": 1, "score": 100, "line": 0, "won_tiebreaker": False},
        {"player_id": 2, "score": 100, "line": 0, "won_tiebreaker": False},
        {"player_id": 3, "score": 60, "line": 0, "won_tiebreaker": False},
    ]
    result = calculate_placements(scores)
    assert result[0]["placement"] == 1
    assert result[1]["placement"] == 1
    assert result[2]["placement"] == 3


# --- apply_line_adjustments (via cup creation) ---


def test_line_adjustment_3_player_cup(client):
    """1st gets -3, 2nd unchanged, 3rd gets +3."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -3
    assert bob["line"] == 0
    assert carol["line"] == 3


def test_line_adjustment_first_by_line_score_not_raw(client):
    """Player with highest line score (not raw) should get 1st place adjustment."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    # Set Bob's line to +15 via player edit
    client.post("/players/2/edit", data={"name": "Bob", "has_line": "on", "line": "15"})
    # Raw: Alice 80, Bob 70, Carol 60
    # Line scores: Alice 80, Bob 85, Carol 60 → Bob is 1st by line score
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 80), (2, 70), (3, 60)])
    conn = get_connection()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert bob["line"] == 12    # was 15, 1st place gets -3
    assert alice["line"] == 0   # 2nd, no change
    assert carol["line"] == 3   # 3rd, gets +3


def test_no_line_adjustment_2_player_cup(client):
    """Line adjustments only apply for exactly 3 players."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80)])
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    conn.close()
    assert alice["line"] == 0
    assert bob["line"] == 0


def test_no_line_adjustment_4_player_cup(client):
    """Line adjustments only apply for exactly 3 players."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_player(client, "Dave")
    create_cup_with_scores(
        client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60), (4, 40)]
    )
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    dave = conn.execute("SELECT line FROM players WHERE id = 4").fetchone()
    conn.close()
    assert alice["line"] == 0
    assert bob["line"] == 0
    assert carol["line"] == 0
    assert dave["line"] == 0


def test_line_changes_records_created(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    conn = get_connection()
    changes = conn.execute(
        "SELECT player_id, line_before, line_after FROM line_changes WHERE cup_id = 1 ORDER BY player_id"
    ).fetchall()
    conn.close()
    assert len(changes) == 3
    # Alice: 1st, 0 → -3
    assert changes[0]["line_before"] == 0
    assert changes[0]["line_after"] == -3
    # Bob: 2nd, 0 → 0
    assert changes[1]["line_before"] == 0
    assert changes[1]["line_after"] == 0
    # Carol: 3rd, 0 → +3
    assert changes[2]["line_before"] == 0
    assert changes[2]["line_after"] == 3


def test_no_adjustment_for_non_line_player(client):
    """Players without has_line don't get their line adjusted."""
    create_player(client, "Alice", has_line=True)
    create_player(client, "Bob", has_line=True)
    create_player(client, "Carol", has_line=False)
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -3   # 1st, has_line
    assert bob["line"] == 0      # 2nd, has_line
    assert carol["line"] == 0    # 3rd, no has_line — unchanged


def test_no_line_change_record_for_non_line_player(client):
    create_player(client, "Alice", has_line=True)
    create_player(client, "Bob", has_line=True)
    create_player(client, "Carol", has_line=False)
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    conn = get_connection()
    changes = conn.execute(
        "SELECT player_id FROM line_changes WHERE cup_id = 1"
    ).fetchall()
    conn.close()
    player_ids = [c["player_id"] for c in changes]
    assert 3 not in player_ids  # Carol has no line_changes record


def test_line_adjustment_uses_line_scores(client):
    """Placement is based on line-adjusted scores, not raw scores."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    # Give Carol a +30 line via form so she places higher despite low raw score
    create_cup_with_scores(
        client, "2026-03-15T20:00",
        [(1, 100), (2, 80), (3, 60)],
        lines=[0, 0, 30],
    )
    # Line scores: Alice 100, Bob 80, Carol 90 → Alice 1st, Carol 2nd, Bob 3rd
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -3   # 1st
    assert carol["line"] == 0    # 2nd, no change (has_line=True but delta=0)
    assert bob["line"] == 3      # 3rd


def test_line_adjustment_cumulative(client):
    """Multiple cups accumulate line adjustments."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    create_cup_with_scores(client, "2026-03-16T20:00", [(1, 100), (2, 80), (3, 60)])
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -6   # -3 twice
    assert carol["line"] == 6    # +3 twice


def test_cup_deletion_does_not_revert_lines(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    client.post("/cups/1/delete", follow_redirects=True)
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -3
    assert carol["line"] == 3


def test_line_changes_shown_on_cup_list(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    response = client.get("/cups")
    data = response.data.decode()
    assert "Lines:" in data
    # Alice went from +0 to -3
    assert "+0" in data
    assert "-3" in data


def test_no_line_changes_shown_for_2_player_cup(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80)])
    response = client.get("/cups")
    assert b"Lines:" not in response.data


def test_new_cup_shows_player_lines(client):
    create_player(client, "Alice")
    conn = get_connection()
    conn.execute("UPDATE players SET line = 5, has_line = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.get("/cups/new")
    assert b'value="5"' in response.data


def test_edit_cup_shows_player_lines(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    response = client.get("/cups/1/edit")
    # Alice should have line -3 after adjustment
    assert b"-3" in response.data


def test_line_score_saved_in_db(client):
    """line_score = raw + line from form."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(
        client, "2026-03-15T20:00",
        [(1, 100), (2, 80), (3, 60)],
        lines=[10, -5, 0],
    )
    conn = get_connection()
    scores = conn.execute(
        "SELECT player_id, score, line, line_score FROM scores WHERE cup_id = 1 ORDER BY player_id"
    ).fetchall()
    conn.close()
    # Alice: raw 100 + line 10 = 110
    assert scores[0]["score"] == 100
    assert scores[0]["line"] == 10
    assert scores[0]["line_score"] == 110
    # Bob: raw 80 + line -5 = 75
    assert scores[1]["score"] == 80
    assert scores[1]["line"] == -5
    assert scores[1]["line_score"] == 75
    # Carol: raw 60 + line 0 = 60
    assert scores[2]["score"] == 60
    assert scores[2]["line"] == 0
    assert scores[2]["line_score"] == 60


def test_edit_cup_shows_line_scores(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    response = client.get("/cups/1/edit")
    data = response.data.decode()
    # Alice: raw 100, stored line 0 (line at creation time), line score = 100
    assert 'value="100"' in data


def test_edit_cup_shows_no_recalculation_note(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    response = client.get("/cups/1/edit")
    assert b"will not recalculate line adjustments" in response.data


def test_new_cup_does_not_show_recalculation_note(client):
    response = client.get("/cups/new")
    assert b"will not recalculate" not in response.data


def test_delete_warning_mentions_lines(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)])
    response = client.get("/cups")
    assert b"Line adjustments from this cup will not be reverted" in response.data


def test_line_adjustment_flash_message(client):
    """Cup creation should flash a message about line changes."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    response = create_cup_with_scores(
        client, "2026-03-15T20:00", [(1, 100), (2, 80), (3, 60)]
    )
    assert b"Lines adjusted" in response.data


def test_no_line_adjustment_with_unresolved_tie(client):
    """Tied line scores without a tiebreaker should skip line adjustments entirely."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    # Alice and Bob both have line score 100
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 100), (2, 100), (3, 60)])
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    changes = conn.execute("SELECT * FROM line_changes WHERE cup_id = 1").fetchall()
    conn.close()
    assert alice["line"] == 0
    assert bob["line"] == 0
    assert carol["line"] == 0
    assert len(changes) == 0


def test_line_adjustment_with_resolved_tie(client):
    """Tied line scores with a tiebreaker selected should still adjust lines."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(
        client, "2026-03-15T20:00", [(1, 100), (2, 100), (3, 60)],
        tiebreaker_ids=[1],
    )
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == -3  # 1st (won tiebreaker)
    assert bob["line"] == 0     # 2nd
    assert carol["line"] == 3   # 3rd


def test_no_line_adjustment_three_way_tie(client):
    """Three-way tie with no tiebreaker should skip all adjustments."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_cup_with_scores(client, "2026-03-15T20:00", [(1, 80), (2, 80), (3, 80)])
    conn = get_connection()
    alice = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    bob = conn.execute("SELECT line FROM players WHERE id = 2").fetchone()
    carol = conn.execute("SELECT line FROM players WHERE id = 3").fetchone()
    conn.close()
    assert alice["line"] == 0
    assert bob["line"] == 0
    assert carol["line"] == 0
