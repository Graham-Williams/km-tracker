import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app import app
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


def create_player(client, name, default_cup=True):
    data = {"name": name}
    if default_cup:
        data["default_cup"] = "on"
    return client.post("/players", data=data, follow_redirects=True)


def ensure_player(client):
    """Create a default player if none exists, for tests that just need a valid cup."""
    create_player(client, "TestPlayer")


def create_cup(client, date="", notes="", tz_offset="", player_id="1", score="50"):
    """Create a cup with one score (required). Call ensure_player first if needed."""
    return client.post(
        "/cups",
        data={
            "date": date,
            "notes": notes,
            "tz_offset": tz_offset,
            "player_ids[]": [player_id],
            "scores[]": [score],
        },
        follow_redirects=True,
    )


# --- List ---


def test_cups_page_loads(client):
    response = client.get("/cups")
    assert response.status_code == 200
    assert b"Cups" in response.data


def test_cups_page_empty_state(client):
    response = client.get("/cups")
    assert b"No cups yet" in response.data


# --- Create ---


def test_create_cup_defaults_to_utc_now(client):
    ensure_player(client)
    fake_now = datetime(2026, 3, 15, 20, 30, 45, tzinfo=timezone.utc)
    with patch("app.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.strptime = datetime.strptime
        response = create_cup(client)
    # Seconds should be truncated to :00
    assert b"2026-03-15 20:30:00" in response.data
    assert b"No cups yet" not in response.data


def test_create_cup_with_date_and_offset(client):
    ensure_player(client)
    # Local time 2026-03-15T20:00, offset 300 (UTC-5 / Eastern)
    response = create_cup(client, date="2026-03-15T20:00", tz_offset="300")
    # Should be stored as UTC: 20:00 + 300min = 01:00 next day
    assert b"2026-03-16 01:00:00" in response.data


def test_create_cup_with_date_no_offset(client):
    ensure_player(client)
    # No offset — stored as-is
    response = create_cup(client, date="2026-03-15T20:00")
    assert b"2026-03-15 20:00:00" in response.data


def test_create_cup_with_notes(client):
    ensure_player(client)
    response = create_cup(client, notes="Game night at Bob's")
    assert b"Game night at Bob" in response.data


def test_create_cup_invalid_date(client):
    ensure_player(client)
    response = create_cup(client, date="not-a-date")
    assert b"Invalid date" in response.data


def test_create_duplicate_date(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    response = create_cup(client, date="2026-03-15T20:00")
    assert b"already exists at that time" in response.data


def test_create_cup_without_scores_rejected(client):
    response = client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"at least one player" in response.data


def test_cups_sorted_by_date_descending(client):
    ensure_player(client)
    create_cup(client, date="2026-01-01T12:00")
    create_cup(client, date="2026-06-01T12:00")
    create_cup(client, date="2026-03-01T12:00")
    response = client.get("/cups")
    data = response.data.decode()
    pos_jan = data.index("2026-01-01")
    pos_mar = data.index("2026-03-01")
    pos_jun = data.index("2026-06-01")
    assert pos_jun < pos_mar < pos_jan


# --- Edit ---


def test_edit_page_loads(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    response = client.get("/cups/1/edit")
    assert response.status_code == 200
    assert b"Edit Cup" in response.data


def test_update_cup_date(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    response = client.post(
        "/cups/1/edit",
        data={
            "date": "2026-04-01T18:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
        },
        follow_redirects=True,
    )
    assert b"2026-04-01 18:00:00" in response.data


def test_update_cup_notes(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00", notes="Old notes")
    response = client.post(
        "/cups/1/edit",
        data={
            "date": "2026-03-15T20:00",
            "notes": "New notes",
            "tz_offset": "",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
        },
        follow_redirects=True,
    )
    assert b"New notes" in response.data
    assert b"Old notes" not in response.data


def test_update_cup_empty_date(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    response = client.post(
        "/cups/1/edit",
        data={"date": "", "notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"cannot be empty" in response.data


def test_update_cup_invalid_date(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    response = client.post(
        "/cups/1/edit",
        data={"date": "garbage", "notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"Invalid date" in response.data


def test_update_cup_duplicate_date(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    create_cup(client, date="2026-03-16T20:00")
    response = client.post(
        "/cups/2/edit",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
        },
        follow_redirects=True,
    )
    assert b"already exists at that time" in response.data


def test_edit_nonexistent_cup(client):
    response = client.get("/cups/999/edit")
    assert response.status_code == 404


def test_update_nonexistent_cup(client):
    response = client.post("/cups/999/edit", data={"date": "2026-03-15T20:00"})
    assert response.status_code == 404


# --- Delete ---


def test_delete_cup(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    response = client.post("/cups/1/delete", follow_redirects=True)
    assert b"No cups yet" in response.data


def test_delete_nonexistent_cup(client):
    """Deleting a non-existent cup is a no-op — just redirects."""
    response = client.post("/cups/999/delete", follow_redirects=True)
    assert response.status_code == 200


def test_soft_delete_cup(client):
    """Deleting a cup sets deleted_at instead of removing the row."""
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    client.post("/cups/1/delete")
    conn = get_connection()
    cup = conn.execute("SELECT deleted_at FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert cup["deleted_at"] is not None


def test_soft_deleted_cup_hidden_from_list(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    client.post("/cups/1/delete")
    response = client.get("/cups")
    assert b"No cups yet" in response.data


def test_soft_deleted_cup_not_editable(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    client.post("/cups/1/delete")
    response = client.get("/cups/1/edit")
    assert response.status_code == 404


def test_soft_deleted_cup_not_updatable(client):
    ensure_player(client)
    create_cup(client, date="2026-03-15T20:00")
    client.post("/cups/1/delete")
    response = client.post("/cups/1/edit", data={"date": "2026-04-01T18:00"})
    assert response.status_code == 404


# --- New cup form ---


def test_new_cup_page_loads(client):
    response = client.get("/cups/new")
    assert response.status_code == 200
    assert b"New Cup" in response.data


def test_new_cup_shows_default_players_only(client):
    create_player(client, "Alice", default_cup=True)
    create_player(client, "Bob", default_cup=False)
    response = client.get("/cups/new")
    data = response.data.decode()
    assert 'class="score-name">Alice' in data
    assert 'class="score-name">Bob' not in data


# --- Create cup with scores ---


def test_create_cup_with_scores(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
        },
        follow_redirects=True,
    )
    conn = get_connection()
    scores = conn.execute("SELECT * FROM scores WHERE cup_id = 1").fetchall()
    conn.close()
    assert len(scores) == 2


def test_create_cup_with_partial_scores(client):
    """Players with empty score fields are skipped — but at least one must have a score."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", ""],
        },
        follow_redirects=True,
    )
    conn = get_connection()
    scores = conn.execute("SELECT * FROM scores WHERE cup_id = 1").fetchall()
    conn.close()
    assert len(scores) == 1


def test_create_cup_with_tiebreaker(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "100"],
            "tiebreakers[]": ["1"],
        },
        follow_redirects=True,
    )
    conn = get_connection()
    winner = conn.execute(
        "SELECT won_tiebreaker FROM scores WHERE cup_id = 1 AND player_id = 1"
    ).fetchone()
    conn.close()
    assert winner["won_tiebreaker"] == 1


def test_create_cup_tiebreaker_validation_no_shared_score(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "tiebreakers[]": ["1"],
        },
        follow_redirects=True,
    )
    assert b"share their score" in response.data


def test_create_cup_multiple_tiebreakers(client):
    """Multiple tie groups can each have a tiebreaker winner."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    create_player(client, "Carol")
    create_player(client, "Dave")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2", "3", "4"],
            "scores[]": ["100", "100", "80", "80"],
            "tiebreakers[]": ["1", "3"],
        },
        follow_redirects=True,
    )
    conn = get_connection()
    winners = conn.execute(
        "SELECT player_id FROM scores WHERE cup_id = 1 AND won_tiebreaker = 1 ORDER BY player_id"
    ).fetchall()
    conn.close()
    assert len(winners) == 2
    assert winners[0]["player_id"] == 1
    assert winners[1]["player_id"] == 3


def test_create_cup_two_winners_same_group_rejected(client):
    """Two tiebreaker winners in the same tie group should be rejected."""
    create_player(client, "Alice")
    create_player(client, "Bob")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "100"],
            "tiebreakers[]": ["1", "2"],
        },
        follow_redirects=True,
    )
    assert b"Only one player" in response.data


# --- Edit cup with scores ---


def test_edit_cup_shows_existing_scores(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
        },
    )
    response = client.get("/cups/1/edit")
    assert b"100" in response.data
    assert b"80" in response.data


def test_update_cup_with_scores(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
        },
    )
    client.post(
        "/cups/1/edit",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["200", "150"],
        },
    )
    conn = get_connection()
    scores = conn.execute(
        "SELECT score FROM scores WHERE cup_id = 1 ORDER BY player_id"
    ).fetchall()
    conn.close()
    assert scores[0]["score"] == 200
    assert scores[1]["score"] == 150


def test_update_cup_tiebreaker_validation(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
        },
    )
    response = client.post(
        "/cups/1/edit",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "tiebreakers[]": ["1"],
        },
        follow_redirects=True,
    )
    assert b"share their score" in response.data
