import os
import sqlite3
import tempfile
import time

import pytest

from db import init_db, get_connection, backup_db


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


def test_init_creates_tables(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in cursor.fetchall()]
    conn.close()
    assert "players" in tables
    assert "cups" in tables
    assert "scores" in tables


def test_init_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)
    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in cursor.fetchall()]
    conn.close()
    assert "players" in tables


def test_players_columns(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    cursor = conn.execute("PRAGMA table_info(players)")
    columns = {row["name"]: row for row in cursor.fetchall()}
    conn.close()
    assert "id" in columns
    assert "name" in columns
    assert columns["name"]["notnull"] == 1


def test_cups_columns(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    cursor = conn.execute("PRAGMA table_info(cups)")
    columns = {row["name"]: row for row in cursor.fetchall()}
    conn.close()
    assert "id" in columns
    assert "date" in columns
    assert "notes" in columns
    assert columns["date"]["notnull"] == 1
    assert columns["notes"]["notnull"] == 0


def test_scores_columns(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    cursor = conn.execute("PRAGMA table_info(scores)")
    columns = {row["name"]: row for row in cursor.fetchall()}
    conn.close()
    assert "id" in columns
    assert "cup_id" in columns
    assert "player_id" in columns
    assert "score" in columns
    assert "won_tiebreaker" in columns
    assert columns["won_tiebreaker"]["notnull"] == 0


def test_foreign_key_enforcement(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score) VALUES (999, 999, 100)"
        )
    conn.close()


def test_backup_creates_timestamped_copy(tmp_path):
    path = str(tmp_path / "km_tracker.db")
    init_db(path)
    backup_db(path)
    backups_dir = tmp_path / "backups"
    assert backups_dir.exists()
    date_dirs = list(backups_dir.iterdir())
    assert len(date_dirs) == 1
    backup_files = list(date_dirs[0].iterdir())
    assert len(backup_files) == 1
    assert backup_files[0].name.startswith("km_tracker_")


def test_backup_skips_if_no_db(tmp_path):
    path = str(tmp_path / "nonexistent.db")
    backup_db(path)  # should not raise
    assert not (tmp_path / "backups").exists()


def test_backup_creates_multiple_per_day(tmp_path):
    path = str(tmp_path / "km_tracker.db")
    init_db(path)
    backup_db(path)
    time.sleep(0.002)
    backup_db(path)
    backups_dir = tmp_path / "backups"
    date_dirs = list(backups_dir.iterdir())
    assert len(date_dirs) == 1
    backup_files = list(date_dirs[0].iterdir())
    assert len(backup_files) == 2


def test_backup_same_millisecond_overwrites(tmp_path):
    path = str(tmp_path / "km_tracker.db")
    init_db(path)
    backup_db(path)
    backup_db(path)  # no sleep — may collide on same millisecond
    backups_dir = tmp_path / "backups"
    date_dirs = list(backups_dir.iterdir())
    assert len(date_dirs) == 1
    backup_files = list(date_dirs[0].iterdir())
    assert len(backup_files) >= 1  # 1 if collision, 2 if not


def test_unique_player_per_cup(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute("INSERT INTO players (name) VALUES ('Alice')")
    conn.execute("INSERT INTO cups (date) VALUES ('2026-03-17')")
    conn.execute(
        "INSERT INTO scores (cup_id, player_id, score) VALUES (1, 1, 50)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score) VALUES (1, 1, 60)"
        )
    conn.close()
