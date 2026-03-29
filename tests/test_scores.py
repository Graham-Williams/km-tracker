from app import validate_scores
from db import get_connection
from helpers import create_cup, create_player, create_score


def setup_cup_with_players(client):
    """Create two players and a cup with player 1's score."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_cup(client, player_id="1", score="50")


# --- validate_scores ---


def test_validate_scores_valid():
    scores = [
        {"score": 100, "won_tiebreaker": False},
        {"score": 80, "won_tiebreaker": False},
    ]
    assert validate_scores(scores) is None


def test_validate_scores_valid_tiebreaker():
    scores = [
        {"score": 100, "won_tiebreaker": True},
        {"score": 100, "won_tiebreaker": False},
        {"score": 80, "won_tiebreaker": False},
    ]
    assert validate_scores(scores) is None


def test_validate_scores_multiple_tiebreaker_winners():
    scores = [
        {"score": 100, "won_tiebreaker": True},
        {"score": 100, "won_tiebreaker": True},
    ]
    assert "Only one player" in validate_scores(scores)


def test_validate_scores_multiple_groups_valid():
    scores = [
        {"score": 100, "won_tiebreaker": True},
        {"score": 100, "won_tiebreaker": False},
        {"score": 80, "won_tiebreaker": True},
        {"score": 80, "won_tiebreaker": False},
    ]
    assert validate_scores(scores) is None


def test_validate_scores_tiebreaker_no_shared_score():
    scores = [
        {"score": 100, "won_tiebreaker": True},
        {"score": 80, "won_tiebreaker": False},
    ]
    assert "share their score" in validate_scores(scores)


def test_validate_scores_empty():
    assert validate_scores([]) is None


# --- Standalone score list ---


def test_scores_page_loads(client):
    response = client.get("/scores")
    assert response.status_code == 200
    assert b"Scores" in response.data


def test_scores_page_empty_state(client):
    response = client.get("/scores")
    assert b"No scores yet" in response.data


# --- Standalone score create ---


def test_create_score(client):
    setup_cup_with_players(client)
    response = create_score(client, cup_id=1, player_id=2, score=100)
    assert b"Bob" in response.data
    assert b"100" in response.data


def test_create_score_missing_fields(client):
    response = client.post(
        "/scores",
        data={"cup_id": "", "player_id": "", "score": ""},
        follow_redirects=True,
    )
    assert b"required" in response.data


def test_create_duplicate_score(client):
    setup_cup_with_players(client)
    create_score(client, player_id=2)
    response = create_score(client, player_id=2)
    assert b"already exists" in response.data


def test_create_score_with_tiebreaker(client):
    setup_cup_with_players(client)
    response = create_score(client, player_id=2, won_tiebreaker=True)
    assert b"(TB)" in response.data


# --- Standalone score edit ---


def test_edit_score_page_loads(client):
    setup_cup_with_players(client)
    create_score(client, player_id=2)
    # Score ID 1 is from cup creation (Alice), ID 2 is from create_score (Bob)
    response = client.get("/scores/2/edit")
    assert response.status_code == 200
    assert b"Edit Score" in response.data


def test_update_score(client):
    setup_cup_with_players(client)
    create_score(client, player_id=2, score=100)
    response = client.post(
        "/scores/2/edit",
        data={"score": "200"},
        follow_redirects=True,
    )
    assert b"200" in response.data


def test_update_score_empty(client):
    setup_cup_with_players(client)
    create_score(client, player_id=2)
    response = client.post(
        "/scores/2/edit",
        data={"score": ""},
        follow_redirects=True,
    )
    assert b"cannot be empty" in response.data


def test_edit_nonexistent_score(client):
    response = client.get("/scores/999/edit")
    assert response.status_code == 404


def test_update_nonexistent_score(client):
    response = client.post("/scores/999/edit", data={"score": "100"})
    assert response.status_code == 404


# --- Standalone score delete ---


def test_delete_score(client):
    setup_cup_with_players(client)
    create_score(client, player_id=2)
    response = client.post("/scores/2/delete", follow_redirects=True)
    # Alice's score from cup creation still exists
    assert b"Bob" not in response.data


def test_delete_nonexistent_score(client):
    response = client.post("/scores/999/delete", follow_redirects=True)
    assert response.status_code == 200


# --- validate_scores with line-adjusted scores ---


def test_validate_scores_line_adjusted_tie():
    """Raw scores differ but line-adjusted scores tie — tiebreaker rules apply."""
    scores = [
        {"player_id": 1, "score": 100, "won_tiebreaker": False},
        {"player_id": 2, "score": 80, "won_tiebreaker": True},
    ]
    # Player 2 has +20 line, so line-adjusted: 100 vs 100
    lines_by_id = {1: 0, 2: 20}
    # Tiebreaker winner is in a tie group (by line score) — should be valid
    assert validate_scores(scores, lines_by_id) is None


def test_validate_scores_line_adjusted_no_tie():
    """Raw scores tie but line-adjusted scores differ — tiebreaker should be rejected."""
    scores = [
        {"player_id": 1, "score": 100, "won_tiebreaker": True},
        {"player_id": 2, "score": 100, "won_tiebreaker": False},
    ]
    # Player 1 has +10 line, so line-adjusted: 110 vs 100 — no tie
    lines_by_id = {1: 10, 2: 0}
    assert "share their score" in validate_scores(scores, lines_by_id)


def test_validate_scores_line_adjusted_multiple_groups():
    """Multiple tie groups formed by line-adjusted scores."""
    scores = [
        {"player_id": 1, "score": 100, "won_tiebreaker": True},
        {"player_id": 2, "score": 80, "won_tiebreaker": False},
        {"player_id": 3, "score": 90, "won_tiebreaker": True},
        {"player_id": 4, "score": 70, "won_tiebreaker": False},
    ]
    # Line-adjusted: 100, 100, 90, 90
    lines_by_id = {1: 0, 2: 20, 3: 0, 4: 20}
    assert validate_scores(scores, lines_by_id) is None


# --- Standalone score line_score behavior ---


def test_standalone_score_uses_player_current_line(client):
    """Standalone score create uses the player's current line for line_score."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    # Set Bob's line to +10
    client.post("/players/2/edit", data={"name": "Bob", "has_line": "on", "line": "10"})
    create_cup(client, player_id="1", score="50")
    create_score(client, cup_id=1, player_id=2, score=80)
    conn = get_connection()
    score = conn.execute("SELECT line, line_score FROM scores WHERE player_id = 2").fetchone()
    conn.close()
    assert score["line"] == 10
    assert score["line_score"] == 90  # 80 + 10


def test_standalone_score_edit_uses_current_line(client):
    """Editing a standalone score recalculates line_score with player's current line."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_cup(client, player_id="1", score="50")
    create_score(client, cup_id=1, player_id=2, score=80)
    # Change Bob's line after score was created
    client.post("/players/2/edit", data={"name": "Bob", "has_line": "on", "line": "15"})
    # Edit the score (ID 2)
    client.post("/scores/2/edit", data={"score": "80"}, follow_redirects=True)
    conn = get_connection()
    score = conn.execute("SELECT line, line_score FROM scores WHERE id = 2").fetchone()
    conn.close()
    # Line_score recalculated with current line (15), not original (0)
    assert score["line"] == 15
    assert score["line_score"] == 95  # 80 + 15
