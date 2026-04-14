import pytest

from app import resolve_db_path


def test_resolve_db_path_prod_mode_returns_db_path():
    env = {"DB_PATH": "/prod/km_tracker.db"}
    assert resolve_db_path(False, env) == "/prod/km_tracker.db"


def test_resolve_db_path_prod_mode_returns_none_when_env_missing():
    assert resolve_db_path(False, {}) is None


def test_resolve_db_path_staging_mode_returns_staging_path():
    env = {
        "DB_PATH": "/prod/km_tracker.db",
        "STAGING_DB_PATH": "/staging/km_tracker.staging.db",
    }
    assert resolve_db_path(True, env) == "/staging/km_tracker.staging.db"


def test_resolve_db_path_staging_mode_missing_env_raises():
    env = {"DB_PATH": "/prod/km_tracker.db"}
    with pytest.raises(SystemExit) as exc_info:
        resolve_db_path(True, env)
    message = str(exc_info.value)
    assert "--staging" in message
    assert "STAGING_DB_PATH" in message


def test_resolve_db_path_staging_mode_empty_string_raises():
    with pytest.raises(SystemExit):
        resolve_db_path(True, {"STAGING_DB_PATH": ""})
