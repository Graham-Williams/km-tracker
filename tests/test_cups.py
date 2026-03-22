import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app import app
from db import init_db


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = db_path
    init_db(db_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
    del os.environ["DB_PATH"]


def create_cup(client, date="", notes="", tz_offset=""):
    return client.post(
        "/cups",
        data={"date": date, "notes": notes, "tz_offset": tz_offset},
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
    fake_now = datetime(2026, 3, 15, 20, 30, 45, tzinfo=timezone.utc)
    with patch("app.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.strptime = datetime.strptime
        response = create_cup(client)
    # Seconds should be truncated to :00
    assert b"2026-03-15 20:30:00" in response.data
    assert b"No cups yet" not in response.data


def test_create_cup_with_date_and_offset(client):
    # Local time 2026-03-15T20:00, offset 300 (UTC-5 / Eastern)
    response = create_cup(client, date="2026-03-15T20:00", tz_offset="300")
    # Should be stored as UTC: 20:00 + 300min = 01:00 next day
    assert b"2026-03-16 01:00:00" in response.data


def test_create_cup_with_date_no_offset(client):
    # No offset — stored as-is
    response = create_cup(client, date="2026-03-15T20:00")
    assert b"2026-03-15 20:00:00" in response.data


def test_create_cup_with_notes(client):
    response = create_cup(client, notes="Game night at Bob's")
    assert b"Game night at Bob" in response.data


def test_create_cup_invalid_date(client):
    response = create_cup(client, date="not-a-date")
    assert b"Invalid date" in response.data


def test_create_duplicate_date(client):
    create_cup(client, date="2026-03-15T20:00")
    response = create_cup(client, date="2026-03-15T20:00")
    assert b"already exists at that time" in response.data


def test_cups_sorted_by_date_descending(client):
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
    create_cup(client, date="2026-03-15T20:00")
    response = client.get("/cups/1/edit")
    assert response.status_code == 200
    assert b"Edit Cup" in response.data


def test_update_cup_date(client):
    create_cup(client, date="2026-03-15T20:00")
    response = client.post(
        "/cups/1/edit",
        data={"date": "2026-04-01T18:00", "notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"2026-04-01 18:00:00" in response.data


def test_update_cup_notes(client):
    create_cup(client, date="2026-03-15T20:00", notes="Old notes")
    response = client.post(
        "/cups/1/edit",
        data={"date": "2026-03-15T20:00", "notes": "New notes", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"New notes" in response.data
    assert b"Old notes" not in response.data


def test_update_cup_empty_date(client):
    create_cup(client, date="2026-03-15T20:00")
    response = client.post(
        "/cups/1/edit",
        data={"date": "", "notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"cannot be empty" in response.data


def test_update_cup_invalid_date(client):
    create_cup(client, date="2026-03-15T20:00")
    response = client.post(
        "/cups/1/edit",
        data={"date": "garbage", "notes": "", "tz_offset": ""},
        follow_redirects=True,
    )
    assert b"Invalid date" in response.data


def test_update_cup_duplicate_date(client):
    create_cup(client, date="2026-03-15T20:00")
    create_cup(client, date="2026-03-16T20:00")
    response = client.post(
        "/cups/2/edit",
        data={"date": "2026-03-15T20:00", "notes": "", "tz_offset": ""},
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
    create_cup(client, date="2026-03-15T20:00")
    response = client.post("/cups/1/delete", follow_redirects=True)
    assert b"No cups yet" in response.data


def test_delete_nonexistent_cup(client):
    """Deleting a non-existent cup is a no-op — just redirects."""
    response = client.post("/cups/999/delete", follow_redirects=True)
    assert response.status_code == 200
