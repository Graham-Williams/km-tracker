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
    assert "game_edition" in columns
    assert columns["date"]["notnull"] == 1
    assert columns["notes"]["notnull"] == 0
    assert columns["game_edition"]["notnull"] == 1


def test_migration_adds_game_edition_to_existing_db(db_path):
    """A cups table created without game_edition gets the column (backfilled to
    'wii') and the migration is idempotent."""
    # Simulate a pre-migration DB: cups table without game_edition.
    conn = get_connection(db_path)
    conn.execute(
        "CREATE TABLE cups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date DATETIME NOT NULL UNIQUE, notes TEXT, deleted_at DATETIME, "
        "status TEXT NOT NULL DEFAULT 'completed', voto_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO cups (date) VALUES ('2026-01-01')")
    conn.commit()
    conn.close()

    # init_db runs schema (no-op for existing table) then migrations.
    init_db(db_path)
    init_db(db_path)  # idempotent — must not error on second run

    conn = get_connection(db_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cups)")}
    assert "game_edition" in cols
    row = conn.execute("SELECT game_edition FROM cups WHERE id = 1").fetchone()
    conn.close()
    assert row["game_edition"] == "wii"  # existing row backfilled


def test_migration_adds_first_edition_to_existing_db(db_path):
    """A cups table created before mixed-edition cups gets first_edition (NULL
    for existing rows, whose game_edition is left untouched). Idempotent."""
    conn = get_connection(db_path)
    conn.execute(
        "CREATE TABLE cups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date DATETIME NOT NULL UNIQUE, notes TEXT, deleted_at DATETIME, "
        "status TEXT NOT NULL DEFAULT 'completed', voto_count INTEGER NOT NULL DEFAULT 0, "
        "game_edition TEXT NOT NULL DEFAULT 'wii')"
    )
    conn.execute("INSERT INTO cups (date) VALUES ('2026-01-01')")
    conn.execute(
        "INSERT INTO cups (date, game_edition) VALUES ('2026-01-02', 'mk8dx')"
    )
    conn.commit()
    conn.close()

    init_db(db_path)
    init_db(db_path)  # idempotent — must not error on second run

    conn = get_connection(db_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cups)")}
    assert "first_edition" in cols
    rows = conn.execute(
        "SELECT game_edition, first_edition FROM cups ORDER BY id"
    ).fetchall()
    conn.close()
    # Existing rows: first_edition NULL, game_edition preserved.
    assert [r["first_edition"] for r in rows] == [None, None]
    assert [r["game_edition"] for r in rows] == ["wii", "mk8dx"]


def test_cups_first_edition_column_is_nullable(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(cups)")}
    conn.close()
    assert "first_edition" in columns
    assert columns["first_edition"]["notnull"] == 0  # NULL for every pure cup


def test_migration_adds_default_character_columns_to_existing_db(db_path):
    """A players table created without the default-character columns gets them
    (NULL for existing rows) and the migration is idempotent."""
    # Simulate a pre-migration DB: players table without the character columns.
    conn = get_connection(db_path)
    conn.execute(
        "CREATE TABLE players (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, default_cup BOOLEAN NOT NULL DEFAULT 1, "
        "line INTEGER NOT NULL DEFAULT 0, has_line BOOLEAN NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO players (name) VALUES ('Alice')")
    conn.commit()
    conn.close()

    init_db(db_path)
    init_db(db_path)  # idempotent — must not error on second run

    conn = get_connection(db_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    assert "default_character_wii" in cols
    assert "default_character_switch" in cols
    row = conn.execute(
        "SELECT default_character_wii, default_character_switch FROM players WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["default_character_wii"] is None
    assert row["default_character_switch"] is None


def test_cup_photos_table(db_path):
    init_db(db_path)
    conn = get_connection(db_path)
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(cup_photos)")}
    assert {"id", "cup_id", "image", "mime_type", "created_at"} <= set(columns)
    assert columns["image"]["notnull"] == 1
    assert columns["mime_type"]["notnull"] == 1
    assert columns["created_at"]["notnull"] == 1
    # FK enforcement: a photo can't reference a nonexistent cup.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cup_photos (cup_id, image, mime_type, created_at) "
            "VALUES (999, X'FFD8', 'image/jpeg', '2026-01-01 00:00:00')"
        )
    conn.close()


def test_fresh_db_matches_migrated_db(tmp_path):
    """A fresh DB from schema.sql must have the same tables/columns as an old
    pre-migration DB brought forward by run_migrations."""
    fresh_path = str(tmp_path / "fresh.db")
    migrated_path = str(tmp_path / "migrated.db")

    init_db(fresh_path)

    # Old DB: pre-game_edition cups, pre-character players, no cup_photos.
    conn = get_connection(migrated_path)
    conn.execute(
        "CREATE TABLE players (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, default_cup BOOLEAN NOT NULL DEFAULT 1, "
        "line INTEGER NOT NULL DEFAULT 0, has_line BOOLEAN NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE cups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date DATETIME NOT NULL UNIQUE, notes TEXT, deleted_at DATETIME, "
        "status TEXT NOT NULL DEFAULT 'completed', voto_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    init_db(migrated_path)

    def snapshot(path):
        conn = get_connection(path)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        cols = {
            t: {row["name"] for row in conn.execute(f"PRAGMA table_info({t})")}
            for t in tables
        }
        conn.close()
        return tables, cols

    assert snapshot(fresh_path) == snapshot(migrated_path)


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
            "INSERT INTO scores (cup_id, player_id, score, line_score) VALUES (999, 999, 100, 100)"
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
        "INSERT INTO scores (cup_id, player_id, score, line_score) VALUES (1, 1, 50, 50)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line_score) VALUES (1, 1, 60, 60)"
        )
    conn.close()
