import os

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


# --- List ---


def test_players_page_loads(client):
    response = client.get("/players")
    assert response.status_code == 200
    assert b"Players" in response.data


def test_players_page_empty_state(client):
    response = client.get("/players")
    assert b"No players yet" in response.data


# --- Create ---


def test_create_player(client):
    response = create_player(client, "Alice")
    assert b"Alice" in response.data
    assert b"No players yet" not in response.data


def test_create_multiple_players(client):
    create_player(client, "Alice")
    response = create_player(client, "Bob")
    assert b"Alice" in response.data
    assert b"Bob" in response.data


def test_create_duplicate_name(client):
    create_player(client, "Alice")
    response = create_player(client, "Alice")
    assert b"already exists" in response.data


def test_create_empty_name(client):
    response = client.post("/players", data={"name": "  "}, follow_redirects=True)
    assert b"cannot be empty" in response.data


def test_create_strips_whitespace(client):
    create_player(client, "  Alice  ")
    response = client.get("/players")
    assert b"Alice" in response.data


# --- Edit ---


def test_edit_page_loads(client):
    create_player(client, "Alice")
    response = client.get("/players/1/edit")
    assert response.status_code == 200
    assert b"Alice" in response.data


def test_edit_player_name(client):
    create_player(client, "Alice")
    response = client.post(
        "/players/1/edit", data={"name": "Alicia"}, follow_redirects=True
    )
    assert b"Alicia" in response.data
    assert b"Alice" not in response.data


def test_edit_duplicate_name(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    response = client.post(
        "/players/2/edit", data={"name": "Alice"}, follow_redirects=True
    )
    assert b"already exists" in response.data


def test_edit_empty_name(client):
    create_player(client, "Alice")
    response = client.post(
        "/players/1/edit", data={"name": ""}, follow_redirects=True
    )
    assert b"cannot be empty" in response.data


def test_edit_nonexistent_player(client):
    response = client.get("/players/999/edit")
    assert response.status_code == 404


def test_update_nonexistent_player(client):
    response = client.post("/players/999/edit", data={"name": "Ghost"})
    assert response.status_code == 404


# --- Delete ---


def test_delete_player(client):
    create_player(client, "Alice")
    response = client.post("/players/1/delete", follow_redirects=True)
    assert b"Alice" not in response.data
    assert b"No players yet" in response.data


def test_delete_nonexistent_player(client):
    """Deleting a non-existent player is a no-op — just redirects."""
    response = client.post("/players/999/delete", follow_redirects=True)
    assert response.status_code == 200


# --- Default cup ---


def test_create_player_default_cup_on(client):
    create_player(client, "Alice", default_cup=True)
    response = client.get("/players")
    assert b"(default)" in response.data


def test_create_player_default_cup_off(client):
    create_player(client, "Alice", default_cup=False)
    response = client.get("/players")
    assert b"(default)" not in response.data


def test_edit_player_default_cup(client):
    create_player(client, "Alice", default_cup=True)
    response = client.post(
        "/players/1/edit", data={"name": "Alice"}, follow_redirects=True
    )
    # No default_cup field sent = unchecked = false
    assert b"(default)" not in response.data


def test_edit_player_set_default_cup(client):
    create_player(client, "Alice", default_cup=False)
    response = client.post(
        "/players/1/edit",
        data={"name": "Alice", "default_cup": "on"},
        follow_redirects=True,
    )
    assert b"(default)" in response.data


def test_delete_player_with_scores_blocked(client):
    """Cannot delete a player who has scores recorded."""
    create_player(client, "Alice")
    # Create a cup with a score for Alice
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1"],
            "scores[]": ["100"],
        },
    )
    response = client.post("/players/1/delete", follow_redirects=True)
    assert b"Cannot delete" in response.data
    assert b"Alice" in response.data


# --- Line ---


def test_player_line_not_shown_without_has_line(client):
    create_player(client, "Alice")
    response = client.get("/players")
    assert b"(+0)" not in response.data


def test_player_line_shown_on_list(client):
    create_player(client, "Alice")
    conn = get_connection()
    conn.execute("UPDATE players SET line = 5, has_line = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.get("/players")
    assert b"(+5)" in response.data


def test_player_line_negative_shown(client):
    create_player(client, "Alice")
    conn = get_connection()
    conn.execute("UPDATE players SET line = -3, has_line = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.get("/players")
    assert b"(-3)" in response.data


def test_edit_player_line(client):
    create_player(client, "Alice")
    client.post(
        "/players/1/edit",
        data={"name": "Alice", "default_cup": "on", "has_line": "on", "line": "6"},
        follow_redirects=True,
    )
    conn = get_connection()
    player = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    conn.close()
    assert player["line"] == 6


def test_edit_player_negative_line(client):
    create_player(client, "Alice")
    client.post(
        "/players/1/edit",
        data={"name": "Alice", "default_cup": "on", "has_line": "on", "line": "-5"},
        follow_redirects=True,
    )
    conn = get_connection()
    player = conn.execute("SELECT line FROM players WHERE id = 1").fetchone()
    conn.close()
    assert player["line"] == -5


def test_edit_player_line_resets_without_has_line(client):
    """Unchecking has_line resets line to 0."""
    create_player(client, "Alice")
    conn = get_connection()
    conn.execute("UPDATE players SET line = 5, has_line = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    client.post(
        "/players/1/edit",
        data={"name": "Alice", "default_cup": "on", "line": "5"},
        follow_redirects=True,
    )
    conn = get_connection()
    player = conn.execute("SELECT line, has_line FROM players WHERE id = 1").fetchone()
    conn.close()
    assert player["line"] == 0
    assert player["has_line"] == 0


def test_edit_player_invalid_line(client):
    create_player(client, "Alice")
    response = client.post(
        "/players/1/edit",
        data={"name": "Alice", "has_line": "on", "line": "abc"},
        follow_redirects=True,
    )
    assert b"must be a number" in response.data


def test_edit_player_line_shown_on_form(client):
    create_player(client, "Alice")
    conn = get_connection()
    conn.execute("UPDATE players SET line = 9, has_line = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.get("/players/1/edit")
    assert b'value="9"' in response.data
