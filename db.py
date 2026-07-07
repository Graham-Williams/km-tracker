import os
import shutil
import sqlite3
from datetime import datetime, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "db", "km_tracker.db")


def get_db_path():
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def get_connection(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    db_path = os.path.abspath(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    # Lock-contention hardening (issue #42). Under concurrent writes SQLite
    # raises "database is locked" if it can't grab the lock instantly; these
    # PRAGMAs make writers queue and let readers proceed while a writer holds
    # the lock, so contention no longer surfaces as HTTP 500s.
    #   - busy_timeout: wait up to 5s for the lock instead of failing at once.
    #     Per-connection, so it must be set every time.
    #   - journal_mode=WAL: readers don't block on a writer. This is a
    #     persistent DB-level setting; re-issuing it per connection is a cheap
    #     idempotent no-op. On a :memory: DB WAL is unavailable and the PRAGMA
    #     silently reports "memory" — harmless, we never assert WAL there.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def backup_db(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        return
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    directory = os.path.dirname(db_path)
    name = os.path.splitext(os.path.basename(db_path))[0]
    backup_dir = os.path.join(directory, "backups", date_str)
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"{name}_{time_str}.db")
    shutil.copy2(db_path, backup_path)


def run_migrations(conn):
    """Apply schema changes that CREATE TABLE IF NOT EXISTS can't make on an
    already-populated DB. Each step is guarded so it is safe to run on every
    startup (idempotent)."""
    # Add cups.game_edition for DBs created before multi-edition support.
    # Existing rows backfill to 'wii' via the column DEFAULT.
    cup_columns = {row["name"] for row in conn.execute("PRAGMA table_info(cups)")}
    if "game_edition" not in cup_columns:
        conn.execute(
            "ALTER TABLE cups ADD COLUMN game_edition TEXT NOT NULL DEFAULT 'wii'"
        )

    # Add per-edition default-character columns for DBs created before
    # photo score entry. Nullable — no default character until set.
    player_columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "default_character_wii" not in player_columns:
        conn.execute("ALTER TABLE players ADD COLUMN default_character_wii TEXT")
    if "default_character_switch" not in player_columns:
        conn.execute("ALTER TABLE players ADD COLUMN default_character_switch TEXT")
    # The cup_photos table itself comes from schema.sql (CREATE TABLE IF NOT
    # EXISTS), which init_db always runs before this — no migration step needed.


def init_db(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    backup_db(db_path)
    conn = get_connection(db_path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    run_migrations(conn)
    conn.commit()
    conn.close()
