import os
import tempfile

import pytest

from app import app
from db import get_db_path, init_db


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
    return client.post("/players", data={"name": name}, follow_redirects=True)


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
