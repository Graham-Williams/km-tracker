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


def test_seed_includes_mixed_cups_with_valid_shape(tmp_path):
    """Staging must carry mixed-cup data for the QA gate to hammer: completed
    mixed cups in BOTH console orders, an in-progress mixed cup parked at the
    swap, and no lines on any lineless (non-Wii) cup."""
    from maps import MIXED_EDITION, TRACK_SETS, other_edition

    db_path = str(tmp_path / "km_tracker.staging.db")
    assert seed_staging.main(["--db", db_path, "--reset"]) == 0

    conn = get_connection(db_path)
    try:
        cups = conn.execute(
            "SELECT id, status, game_edition, first_edition FROM cups ORDER BY id"
        ).fetchall()
        races = conn.execute(
            "SELECT cup_id, race_number, map FROM races ORDER BY cup_id, race_number"
        ).fetchall()
        lined_lineless = conn.execute(
            "SELECT COUNT(*) AS n FROM scores s JOIN cups c ON c.id = s.cup_id "
            "WHERE c.game_edition != 'wii' AND s.line != 0"
        ).fetchone()["n"]
    finally:
        conn.close()

    mixed = [c for c in cups if c["game_edition"] == MIXED_EDITION]
    completed_mixed = [c for c in mixed if c["status"] == "completed"]
    in_progress = [c for c in cups if c["status"] == "in_progress"]

    assert completed_mixed, "expected at least one completed mixed cup"
    # Both coin-flip orders present so history shows both order badges.
    assert {c["first_edition"] for c in completed_mixed} == {"wii", "mk8dx"}
    # Every pure cup leaves first_edition NULL.
    for c in cups:
        if c["game_edition"] != MIXED_EDITION:
            assert c["first_edition"] is None

    # The in-progress cup is mixed and sits exactly at the console swap.
    assert len(in_progress) == 1
    ip = in_progress[0]
    assert ip["game_edition"] == MIXED_EDITION
    assert ip["first_edition"] == seed_staging.IN_PROGRESS_FIRST_EDITION
    ip_races = [r for r in races if r["cup_id"] == ip["id"]]
    assert len(ip_races) == len(seed_staging.IN_PROGRESS_MAPS) == 2

    # Every seeded race sits on the correct console's track list (2-2 blocks).
    by_cup = {c["id"]: c for c in cups}
    for r in races:
        cup = by_cup[r["cup_id"]]
        first = cup["first_edition"]
        expected = first if r["race_number"] <= 2 else other_edition(first)
        assert r["map"] in TRACK_SETS[expected], (
            f"cup {cup['id']} race {r['race_number']} ({r['map']}) "
            f"is not a {expected} course"
        )

    assert lined_lineless == 0


def test_seed_block_scores_and_photos(tmp_path):
    """Seeded mixed cups carry the per-block breakdown and a photo per console
    block; pure cups carry neither. The QA gate audits `score == block1 +
    block2`, so seeded data must never contradict it."""
    from maps import MIXED_EDITION

    db_path = str(tmp_path / "km_tracker.staging.db")
    assert seed_staging.main(["--db", db_path, "--reset"]) == 0

    conn = get_connection(db_path)
    try:
        scores = conn.execute(
            "SELECT c.id AS cup_id, c.game_edition, c.status, s.score, "
            "s.block1_score, s.block2_score FROM scores s "
            "JOIN cups c ON c.id = s.cup_id"
        ).fetchall()
        photos = conn.execute(
            "SELECT c.id AS cup_id, c.game_edition, c.status, p.block, p.mime_type "
            "FROM cup_photos p JOIN cups c ON c.id = p.cup_id"
        ).fetchall()
    finally:
        conn.close()

    mixed_rows = [s for s in scores if s["game_edition"] == MIXED_EDITION]
    assert mixed_rows
    for s in mixed_rows:
        assert s["block1_score"] is not None and s["block2_score"] is not None
        assert s["block1_score"] + s["block2_score"] == s["score"]
    for s in scores:
        if s["game_edition"] != MIXED_EDITION:
            assert s["block1_score"] is None and s["block2_score"] is None

    assert photos
    # Only mixed cups get photos, always block-tagged (never a NULL block).
    assert {p["game_edition"] for p in photos} == {MIXED_EDITION}
    assert {p["block"] for p in photos} == {1, 2}
    assert {p["mime_type"] for p in photos} == {"image/jpeg"}
    # A completed mixed cup has BOTH blocks (two thumbnails to render).
    completed = [p for p in photos if p["status"] == "completed"]
    by_cup = {}
    for p in completed:
        by_cup.setdefault(p["cup_id"], set()).add(p["block"])
    assert by_cup and all(blocks == {1, 2} for blocks in by_cup.values())
    # The in-progress cup is parked at the swap: first console's photo only.
    in_progress = [p for p in photos if p["status"] == "in_progress"]
    assert [p["block"] for p in in_progress] == [1]


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
