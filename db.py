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


def init_db(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    backup_db(db_path)
    conn = get_connection(db_path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.close()
