"""End-to-end: removing (and re-adding) a player on the cup edit page (#116).

A removed row must submit none of its inputs. If its hidden player_ids[] (or
hidden lines[]) still submitted while its score input did not, the arrays would
misalign and parse_scores_from_form would reject the save (#45 guard), or the
lines would shift onto the wrong players.

Cups are seeded straight into the DB: a Switch cup can only be created through
the live session flow, and the edit page is what's under test here.
"""

from db import get_connection

MISALIGNED = "Player and score fields are misaligned."


def _seed_cup(db_path, edition, players):
    """Seed a completed cup. players: list of (name, has_line, line, score).

    Returns {name: player_id}.
    """
    conn = get_connection(db_path)
    cur = conn.execute(
        "INSERT INTO cups (date, status, game_edition) VALUES (?, 'completed', ?)",
        ("2026-09-01 20:00:00", edition),
    )
    cup_id = cur.lastrowid
    ids = {}
    for name, has_line, line, score in players:
        pid = conn.execute(
            "INSERT INTO players (name, has_line, line) VALUES (?, ?, ?)",
            (name, int(has_line), line),
        ).lastrowid
        ids[name] = pid
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line, line_score) "
            "VALUES (?, ?, ?, ?, ?)",
            (cup_id, pid, score, line, score + line),
        )
    conn.commit()
    conn.close()
    return cup_id, ids


def _saved_scores(db_path, cup_id):
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT p.name, s.score, s.line FROM scores s "
        "JOIN players p ON p.id = s.player_id WHERE s.cup_id = ? ORDER BY p.name",
        (cup_id,),
    ).fetchall()
    conn.close()
    return {r["name"]: (r["score"], r["line"]) for r in rows}


def _remove(page, name):
    page.locator(".score-row", has_text=name).locator(".remove-btn").click()


def _save(page, base_url):
    page.click('#cup-form button[type="submit"]')
    page.wait_for_load_state()
    assert page.locator(f"text={MISALIGNED}").count() == 0
    assert page.url == f"{base_url}/cups"


def test_edit_remove_player_saves(page, base_url, _server):
    """Wii cup: removing a lineless player ahead of line players keeps every
    remaining player's score AND line attached to the right person."""
    db = _server["db_path"]
    cup_id, _ = _seed_cup(db, "wii", [
        ("Alice", False, 0, 60),
        ("Bob", True, 5, 50),
        ("Carol", True, -3, 40),
    ])
    page.goto(f"{base_url}/cups/{cup_id}/edit")
    _remove(page, "Alice")
    _save(page, base_url)
    assert _saved_scores(db, cup_id) == {"Bob": (50, 5), "Carol": (40, -3)}


def test_edit_remove_player_saves_switch_cup(page, base_url, _server):
    """Switch (mk8dx) cup: every row is lineless, so each carries a hidden
    lines[] and a hidden player_ids[] that must both drop out on remove."""
    db = _server["db_path"]
    cup_id, _ = _seed_cup(db, "mk8dx", [
        ("Alice", False, 0, 60),
        ("Bob", True, 0, 50),
        ("Carol", False, 0, 40),
    ])
    page.goto(f"{base_url}/cups/{cup_id}/edit")
    # Lineless edit page: no visible line inputs at all.
    assert page.locator(".line-input").count() == 0
    _remove(page, "Bob")
    _save(page, base_url)
    assert _saved_scores(db, cup_id) == {"Alice": (60, 0), "Carol": (40, 0)}


def test_edit_remove_then_readd_player_saves_all(page, base_url, _server):
    """Re-adding a removed player re-enables every input the remove disabled,
    so all three players save with their own scores and lines."""
    db = _server["db_path"]
    cup_id, _ = _seed_cup(db, "wii", [
        ("Alice", False, 0, 60),
        ("Bob", True, 5, 50),
        ("Carol", True, -3, 40),
    ])
    page.goto(f"{base_url}/cups/{cup_id}/edit")
    _remove(page, "Alice")
    page.select_option("#add-player-select", label="Alice")
    page.click("#add-player-btn")
    alice = page.locator(".score-row:not(.removed)", has_text="Alice")
    assert alice.count() == 1
    # Remove clears the score box; re-enter it.
    alice.locator('input[name="scores[]"]').fill("61")
    _save(page, base_url)
    assert _saved_scores(db, cup_id) == {
        "Alice": (61, 0),
        "Bob": (50, 5),
        "Carol": (40, -3),
    }
