#!/usr/bin/env python3
"""Seed a STAGING km-tracker database with obviously-fake data.

This makes the hosted staging environment (staging.km.graham-williams.com) look
lived-in without ever touching real people's data. It writes directly against
the SQLite schema (schema.sql) rather than going through the Flask app.

Safety rail (most important thing in this file): the script REFUSES to run
unless the resolved DB path's basename contains the substring "staging",
UNLESS an explicit --force flag is passed. This makes it effectively impossible
to seed/wipe the production DB (km_tracker.db) by accident.

Usage (inside the container, DB_PATH already set to the staging DB):
    python scripts/seed_staging.py --reset      # wipe + reseed
    python scripts/seed_staging.py              # seed only if empty

Usage (explicit path):
    python scripts/seed_staging.py --db /data/km_tracker.staging.db --reset
"""

import argparse
import base64
import os
import random
import sys

# Make the repo root importable whether run as "scripts/seed_staging.py" or from
# inside the container where the script lives at /app/scripts/seed_staging.py.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db import get_connection, init_db  # noqa: E402
from maps import MIXED_EDITION, TRACK_SETS, other_edition  # noqa: E402

# Deterministic seed so scores/standings are reproducible run to run.
RANDOM_SEED = 20260701

# Six obviously-fake players. Names are intentionally silly so nobody confuses
# them with real game-night participants. `line`/`has_line` give a little flavor.
FAKE_PLAYERS = [
    {"name": "Test Toad", "line": 0, "has_line": 0, "skill": 95},
    {"name": "Dummy Diddy", "line": 5, "has_line": 1, "skill": 88},
    {"name": "Sample Shy Guy", "line": 0, "has_line": 0, "skill": 80},
    {"name": "Mock Mario", "line": 10, "has_line": 1, "skill": 72},
    {"name": "Fake Bowser", "line": 0, "has_line": 0, "skill": 65},
    {"name": "Demo Daisy", "line": 0, "has_line": 0, "skill": 58},
]

# 11 completed cups + 1 in-progress cup = 12 total.
NUM_COMPLETED_CUPS = 11
NUM_IN_PROGRESS_CUPS = 1
NUM_CUPS = NUM_COMPLETED_CUPS + NUM_IN_PROGRESS_CUPS

# The in-progress cup is a MIXED ("Wii + Switch") cup parked exactly at the
# console swap: both first-half races played, so loading it puts staging on the
# swap-reminder + second-half-wheel screen — the newest surface for the QA gate
# to hammer. first_edition is fixed (not flipped) so reseeds stay deterministic.
IN_PROGRESS_FIRST_EDITION = "wii"
IN_PROGRESS_MAPS = ["Luigi Circuit", "Coconut Mall"]


def seed_cup_edition(index):
    """Deterministic (game_edition, first_edition) for seeded completed cup N.

    Spreads all three cup editions across the seeded history so staging shows
    Wii, Switch and both mixed console orders ("Wii → Switch" and
    "Switch → Wii"). first_edition is NULL for every non-mixed cup.
    """
    if index % 4 == 3:
        return MIXED_EDITION, ("mk8dx" if index % 8 == 3 else "wii")
    if index % 4 == 2:
        return "mk8dx", None
    return "wii", None


def mixed_race_maps(first_edition):
    """[(race_number, map)] for a seeded mixed cup: 2 races on the flipped-to
    console, then 2 on the other. Courses are taken from the real TRACK_SETS so
    seeded history can never contain an off-edition course."""
    second = other_edition(first_edition)
    return [
        (1, TRACK_SETS[first_edition][0]),
        (2, TRACK_SETS[first_edition][1]),
        (3, TRACK_SETS[second][0]),
        (4, TRACK_SETS[second][1]),
    ]


def split_blocks(score):
    """Split a mixed cup's TOTAL into its two per-console halves.

    The invariant the app enforces everywhere — score == block1 + block2 — has
    to hold in seeded data too, or the QA gate audits a contradiction the app
    itself could never produce.
    """
    block1 = score // 2
    return block1, score - block1


# A SYNTHETIC 1x1 JPEG — generated, not photographed. Seeded photos exist only
# to exercise the per-block thumbnail/preview paths; a real standings photo is
# a picture of somebody's living room and must never enter this repo.
SYNTHETIC_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def _insert_block_photo(conn, cup_id, block, created_at):
    conn.execute(
        "INSERT INTO cup_photos (cup_id, image, mime_type, created_at, block) "
        "VALUES (?, ?, 'image/jpeg', ?, ?)",
        (cup_id, SYNTHETIC_JPEG, created_at, block),
    )


def resolve_db_path(args_db, env):
    """Resolve the target DB path from --db arg (preferred) or DB_PATH env."""
    if args_db:
        return args_db
    env_db = env.get("DB_PATH")
    if env_db:
        return env_db
    return None


def is_staging_path(db_path):
    """True if the DB path's basename contains the substring 'staging'.

    This is the guard that prevents seeding/wiping the prod DB. Matched
    case-insensitively on the file basename only (not the directory).
    """
    basename = os.path.basename(db_path).lower()
    return "staging" in basename


def db_has_data(db_path):
    """True if the DB already holds any players or cups."""
    if not os.path.exists(db_path):
        return False
    conn = get_connection(db_path)
    try:
        players = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        cups = conn.execute("SELECT COUNT(*) AS n FROM cups").fetchone()["n"]
    except Exception:
        # Tables don't exist yet -> treat as empty.
        return False
    finally:
        conn.close()
    return (players + cups) > 0


def _wipe(db_path):
    """Delete the DB file (and its SQLite sidecars) so schema is recreated fresh."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


def _insert_players(conn):
    ids = []
    for p in FAKE_PLAYERS:
        cur = conn.execute(
            "INSERT INTO players (name, default_cup, line, has_line) VALUES (?, ?, ?, ?)",
            (p["name"], 1, p["line"], p["has_line"]),
        )
        ids.append(cur.lastrowid)
    return ids


def _insert_completed_cups(conn, rng, player_ids):
    """Insert NUM_COMPLETED_CUPS completed cups with a realistic spread of scores."""
    line_by_id = {pid: FAKE_PLAYERS[i]["line"] for i, pid in enumerate(player_ids)}
    skill_by_id = {pid: FAKE_PLAYERS[i]["skill"] for i, pid in enumerate(player_ids)}

    for c in range(NUM_COMPLETED_CUPS):
        # Deterministic, unique, descending dates (one per cup, in the past).
        day = 1 + c
        date_utc = f"2026-{(1 + c % 6):02d}-{day:02d} 19:{(c * 3) % 60:02d}:00"

        edition, first_edition = seed_cup_edition(c)

        # Pick 4-6 players for this cup (varied field sizes).
        field_size = rng.randint(4, len(player_ids))
        cup_players = rng.sample(player_ids, field_size)

        rows = []
        for pid in cup_players:
            # Score = skill baseline + noise, clamped to a plausible cup total.
            score = max(12, min(120, skill_by_id[pid] + rng.randint(-25, 25)))
            # Lines are Wii-only — Switch and mixed cups are lineless, so seeded
            # history must not carry a handicap on them either (a non-zero line
            # on a lineless cup is exactly the invariant the QA gate audits).
            line = line_by_id[pid] if edition == "wii" else 0
            rows.append(
                {
                    "player_id": pid,
                    "score": score,
                    "line": line,
                    "line_score": score + line,
                }
            )

        # Break ties on line_score for exactly one top pair when needed.
        rows.sort(key=lambda r: r["line_score"], reverse=True)
        for i, r in enumerate(rows):
            tied_with_prev = i > 0 and r["line_score"] == rows[i - 1]["line_score"]
            r["won_tiebreaker"] = True if (i == 1 and tied_with_prev) else None

        notes = None if c % 3 else f"Staging demo cup #{c + 1}"
        cur = conn.execute(
            "INSERT INTO cups (date, notes, status, game_edition, first_edition) "
            "VALUES (?, ?, 'completed', ?, ?)",
            (date_utc, notes, edition, first_edition),
        )
        cup_id = cur.lastrowid
        # Mixed cups get real race rows so the completion/edit screens have
        # per-race console dropdowns to exercise. (Pure seeded cups keep their
        # existing race-less shape.)
        if edition == MIXED_EDITION:
            for race_number, course in mixed_race_maps(first_edition):
                conn.execute(
                    "INSERT INTO races (cup_id, race_number, map) VALUES (?, ?, ?)",
                    (cup_id, race_number, course),
                )
            # Two console blocks -> two results screens. Seed both photos so the
            # per-block thumbnail path (history + cup edit) has something real
            # to render, and a per-block score breakdown that ADDS UP.
            for block in (1, 2):
                _insert_block_photo(conn, cup_id, block, date_utc)
        for r in rows:
            if edition == MIXED_EDITION:
                block1, block2 = split_blocks(r["score"])
            else:
                block1 = block2 = None
            conn.execute(
                "INSERT INTO scores (cup_id, player_id, score, line, line_score, won_tiebreaker, "
                "block1_score, block2_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cup_id,
                    r["player_id"],
                    r["score"],
                    r["line"],
                    r["line_score"],
                    r["won_tiebreaker"],
                    block1,
                    block2,
                ),
            )


def _insert_in_progress_cup(conn, rng, player_ids):
    """Insert one in-progress MIXED cup, parked at the console swap (first-half
    races played, none of the second half, no scores yet)."""
    date_utc = "2026-07-01 20:00:00"
    cur = conn.execute(
        "INSERT INTO cups (date, status, voto_count, game_edition, first_edition) "
        "VALUES (?, 'in_progress', 0, ?, ?)",
        (date_utc, MIXED_EDITION, IN_PROGRESS_FIRST_EDITION),
    )
    cup_id = cur.lastrowid

    field = rng.sample(player_ids, 5)
    for pid in field:
        conn.execute(
            "INSERT INTO cup_players (cup_id, player_id) VALUES (?, ?)",
            (cup_id, pid),
        )
    for i, course in enumerate(IN_PROGRESS_MAPS, start=1):
        conn.execute(
            "INSERT INTO races (cup_id, race_number, map) VALUES (?, ?, ?)",
            (cup_id, i, course),
        )
    # The cup is parked exactly at the console swap, which is where the first
    # console's results photo gets taken — seed it so staging lands on the
    # "photo saved ✓ — replace" state rather than only the empty one.
    _insert_block_photo(conn, cup_id, 1, date_utc)
    return cup_id


def seed(db_path):
    """Create schema (idempotent) and insert the fake dataset. Returns counts."""
    init_db(db_path)  # idempotent CREATE TABLE IF NOT EXISTS from schema.sql
    rng = random.Random(RANDOM_SEED)
    conn = get_connection(db_path)
    try:
        player_ids = _insert_players(conn)
        _insert_completed_cups(conn, rng, player_ids)
        _insert_in_progress_cup(conn, rng, player_ids)
        conn.commit()
        players = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        cups = conn.execute("SELECT COUNT(*) AS n FROM cups").fetchone()["n"]
        scores = conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"]
    finally:
        conn.close()
    return {"players": players, "cups": cups, "scores": scores}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed a STAGING km-tracker DB with fake data.")
    parser.add_argument("--db", help="Path to the DB. Overrides DB_PATH env.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe existing data and recreate from schema.sql before seeding.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the staging-path safety rail. DANGEROUS. Do not use on prod.",
    )
    args = parser.parse_args(argv)

    db_path = resolve_db_path(args.db, os.environ)
    if not db_path:
        print(
            "ERROR: no DB path. Set DB_PATH or pass --db PATH.",
            file=sys.stderr,
        )
        return 2

    # realpath (not abspath) so a staging-named symlink pointing at the prod DB
    # resolves to its real target and is correctly rejected by the guard below.
    db_path = os.path.realpath(db_path)

    # --- SAFETY RAIL: refuse to touch a non-staging DB unless --force. ---
    # This runs BEFORE any file/DB access so a guard trip never creates or
    # modifies the target file.
    if not is_staging_path(db_path) and not args.force:
        print(
            f"ERROR: refusing to seed '{db_path}': its basename does not contain "
            f"'staging'. This guard prevents wiping the production DB. Pass --force "
            f"to override (only if you are certain this is not prod).",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        _wipe(db_path)
    elif db_has_data(db_path):
        print(
            f"ERROR: '{db_path}' already has data. Refusing to double-seed. "
            f"Re-run with --reset to wipe and reseed.",
            file=sys.stderr,
        )
        return 1

    counts = seed(db_path)
    print(
        f"Seeded staging DB at {db_path}: "
        f"{counts['players']} players, {counts['cups']} cups, {counts['scores']} scores."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
