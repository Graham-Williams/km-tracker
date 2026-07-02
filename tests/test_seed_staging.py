import os
import sys

import pytest

# The seed script lives in scripts/, alongside the repo root already on sys.path
# via conftest. Import it by adding scripts/ to the path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import seed_staging  # noqa: E402
from db import get_connection  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db_env():
    """Keep DB_PATH out of the environment so tests drive it via --db only."""
    saved = os.environ.pop("DB_PATH", None)
    yield
    if saved is not None:
        os.environ["DB_PATH"] = saved
    else:
        os.environ.pop("DB_PATH", None)


def test_seed_populates_expected_players_and_cups(tmp_path):
    db_path = str(tmp_path / "km_tracker.staging.db")
    rc = seed_staging.main(["--db", db_path, "--reset"])
    assert rc == 0
    assert os.path.exists(db_path)

    conn = get_connection(db_path)
    try:
        players = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        cups = conn.execute("SELECT COUNT(*) AS n FROM cups").fetchone()["n"]
        in_progress = conn.execute(
            "SELECT COUNT(*) AS n FROM cups WHERE status = 'in_progress'"
        ).fetchone()["n"]
        scores = conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"]
        # line_score integrity: must equal score + line for every row.
        bad = conn.execute(
            "SELECT COUNT(*) AS n FROM scores WHERE line_score != score + line"
        ).fetchone()["n"]
    finally:
        conn.close()

    assert players == len(seed_staging.FAKE_PLAYERS) == 6
    assert cups == seed_staging.NUM_CUPS == 12
    assert in_progress == seed_staging.NUM_IN_PROGRESS_CUPS == 1
    assert scores > 0
    assert bad == 0


def test_seed_is_deterministic(tmp_path):
    """Same seed -> identical scores, so staging looks stable across reseeds."""
    a = str(tmp_path / "a.staging.db")
    b = str(tmp_path / "b.staging.db")
    assert seed_staging.main(["--db", a, "--reset"]) == 0
    assert seed_staging.main(["--db", b, "--reset"]) == 0

    def score_rows(path):
        conn = get_connection(path)
        try:
            return conn.execute(
                "SELECT cup_id, player_id, score, line_score FROM scores "
                "ORDER BY cup_id, player_id"
            ).fetchall()
        finally:
            conn.close()

    rows_a = [tuple(r) for r in score_rows(a)]
    rows_b = [tuple(r) for r in score_rows(b)]
    assert rows_a == rows_b


def test_safety_rail_blocks_non_staging_path_and_does_not_create_db(tmp_path):
    """Pointing at a non-'staging' path without --force must exit non-zero and
    must NOT create or modify the DB file."""
    prod_path = str(tmp_path / "km_tracker.db")  # basename lacks "staging"
    assert not os.path.exists(prod_path)

    rc = seed_staging.main(["--db", prod_path])
    assert rc != 0
    # Guard tripped BEFORE any DB access -> file never created.
    assert not os.path.exists(prod_path)


def test_safety_rail_blocks_non_staging_path_even_with_reset(tmp_path):
    prod_path = str(tmp_path / "km_tracker.db")
    rc = seed_staging.main(["--db", prod_path, "--reset"])
    assert rc != 0
    assert not os.path.exists(prod_path)


def test_force_flag_overrides_safety_rail(tmp_path):
    """--force lets a non-staging path through (escape hatch)."""
    path = str(tmp_path / "km_tracker.db")
    rc = seed_staging.main(["--db", path, "--reset", "--force"])
    assert rc == 0
    assert os.path.exists(path)


def test_refuses_to_double_seed_without_reset(tmp_path):
    db_path = str(tmp_path / "km_tracker.staging.db")
    assert seed_staging.main(["--db", db_path, "--reset"]) == 0
    # Second run without --reset must refuse (data already present).
    assert seed_staging.main(["--db", db_path]) != 0


def test_reset_reseeds_without_duplicating(tmp_path):
    db_path = str(tmp_path / "km_tracker.staging.db")
    assert seed_staging.main(["--db", db_path, "--reset"]) == 0
    assert seed_staging.main(["--db", db_path, "--reset"]) == 0
    conn = get_connection(db_path)
    try:
        players = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        cups = conn.execute("SELECT COUNT(*) AS n FROM cups").fetchone()["n"]
    finally:
        conn.close()
    assert players == 6
    assert cups == 12


def test_db_path_from_env(tmp_path):
    db_path = str(tmp_path / "km_tracker.staging.db")
    os.environ["DB_PATH"] = db_path
    try:
        assert seed_staging.main(["--reset"]) == 0
        assert os.path.exists(db_path)
    finally:
        os.environ.pop("DB_PATH", None)


def test_is_staging_path_matcher():
    assert seed_staging.is_staging_path("/data/km_tracker.staging.db")
    assert seed_staging.is_staging_path("/data/STAGING.db")
    assert not seed_staging.is_staging_path("/data/km_tracker.db")
    # Only the basename is matched, not the directory.
    assert not seed_staging.is_staging_path("/staging/km_tracker.db")
