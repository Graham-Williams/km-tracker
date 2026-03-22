import os

import pytest

from app import app, validate_scores
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


def create_player(client, name):
    return client.post(
        "/players", data={"name": name, "default_cup": "on"}, follow_redirects=True
    )


def create_cup(client, date="2026-03-15T20:00", player_id="1", score="50"):
    """Create a cup with one score (required)."""
    return client.post(
        "/cups",
        data={
            "date": date,
            "notes": "",
            "tz_offset": "",
            "player_ids[]": [player_id],
            "scores[]": [score],
        },
        follow_redirects=True,
    )


def create_score(client, cup_id=1, player_id=2, score=100, won_tiebreaker=False):
    data = {
        "cup_id": str(cup_id),
        "player_id": str(player_id),
        "score": str(score),
    }
    if won_tiebreaker:
        data["won_tiebreaker"] = "on"
    return client.post("/scores", data=data, follow_redirects=True)


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
