"""Malformed / hostile input against the main POST endpoints.

Contract under test: bad input must produce a 4xx (or a flash+redirect),
never a 500, and must not persist bad state.

Note on mechanics: the client fixture runs with TESTING=True, so an unhandled
exception in a view propagates into the test instead of rendering a 500 page.
A test that dies with ValueError/OverflowError/InterfaceError therefore
corresponds to a 500 Internal Server Error in production. Those cases are
real app bugs and are marked xfail (strict) below — do not "fix" the tests;
fix the app and the xfails will flag as XPASS.

KNOWN BUGS COVERED HERE (each -> 500 in production):
  BUG-1  POST /cups: non-numeric scores[] -> ValueError in
         parse_scores_from_form (int(raw), app.py ~627)
  BUG-2  POST /cups: non-numeric player_ids[] -> ValueError (int(pid),
         app.py ~626)
  BUG-3  POST /cups: score > 2**63-1 -> OverflowError binding to SQLite,
         raised mid-transaction (leaks an open write transaction)
  BUG-4  POST /scores: non-numeric score / cup_id / player_id -> ValueError
         (int() calls in create_score, app.py ~789-795)
  BUG-5  POST /scores/<id>/edit: non-numeric score -> ValueError
         (int(score_val) in update_score, app.py ~840)
  BUG-6  POST /cup-session/new: non-numeric player_ids[] -> ValueError
         (int(pid), app.py ~916) — ALSO leaks an open write transaction
         (cup INSERT happened, conn never closed), so subsequent writes on
         that worker hit "database is locked"
  BUG-7  POST /cup-session/<id>/complete: non-numeric scores[] -> ValueError
         (same parse_scores_from_form path as BUG-1)
  BUG-8  POST /cup-session/<id>/half-veto: JSON player_id of non-scalar type
         (e.g. a list) -> sqlite3.InterfaceError binding the parameter
  BUG-9  POST /cup-session/<id>/next-race: JSON map of non-string type
         (e.g. an object) -> sqlite3.InterfaceError binding the parameter
  BUG-10 POST /players/<id>/edit: line > 2**63-1 passes the int() check but
         -> OverflowError binding to SQLite
"""

import pytest

from db import get_connection
from helpers import create_player

SQLITE_INT_OVERFLOW = str(2**63)  # first value SQLite INTEGER can't hold


def _count(table, where="1=1", params=()):
    conn = get_connection()
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    conn.close()
    return n


def _start_session(client, player_ids=("1", "2")):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post("/cup-session/new", data={"player_ids[]": list(player_ids)})
    conn = get_connection()
    row = conn.execute("SELECT id FROM cups WHERE status = 'in_progress'").fetchone()
    conn.close()
    return row["id"]


# =============================================================================
# Players
# =============================================================================


def test_player_absurdly_long_name_no_crash(client):
    response = client.post(
        "/players", data={"name": "A" * 100_000}, follow_redirects=True
    )
    assert response.status_code == 200
    # App still functional afterwards.
    assert client.get("/players").status_code == 200


def test_player_weird_unicode_name_roundtrips(client):
    name = "Ünïcödé 🎮 マリオ ٱلْعَرَبِيَّة ‮"
    response = client.post("/players", data={"name": name}, follow_redirects=True)
    assert response.status_code == 200
    conn = get_connection()
    row = conn.execute("SELECT name FROM players").fetchone()
    conn.close()
    assert row["name"] == name.strip()


def test_player_html_name_is_escaped_not_executed(client):
    payload = "<script>alert(1)</script>"
    client.post("/players", data={"name": payload})
    page = client.get("/players")
    assert page.status_code == 200
    assert b"<script>alert(1)</script>" not in page.data  # Jinja autoescape


def test_player_missing_name_field_rejected(client):
    response = client.post("/players", data={}, follow_redirects=True)
    assert response.status_code == 200
    assert b"cannot be empty" in response.data
    assert _count("players") == 0


def test_update_nonexistent_player_404(client):
    assert client.post("/players/424242/edit", data={"name": "x"}).status_code == 404


def test_player_id_non_integer_in_url_404(client):
    # <int:player_id> converter rejects non-numeric ids at routing time.
    assert client.post("/players/abc/edit", data={"name": "x"}).status_code == 404


@pytest.mark.xfail(
    strict=True,
    reason="BUG-10: huge line value passes int() but overflows SQLite INTEGER -> 500",
)
def test_update_player_huge_line_rejected_gracefully(client):
    create_player(client, "Alice")
    response = client.post(
        "/players/1/edit",
        data={"name": "Alice", "has_line": "on", "line": SQLITE_INT_OVERFLOW},
        follow_redirects=True,
    )
    assert response.status_code < 500
    assert _count("players", "line != 0") == 0


# =============================================================================
# Cups (manual create/edit)
# =============================================================================


def test_cup_invalid_date_rejected(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={"date": "junk", "player_ids[]": ["1"], "scores[]": ["10"], "lines[]": ["0"]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Invalid date" in response.data
    assert _count("cups") == 0


def test_cup_non_numeric_tz_offset_rejected(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "tz_offset": "abc",
            "player_ids[]": ["1"],
            "scores[]": ["10"],
            "lines[]": ["0"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert _count("cups") == 0


def test_cup_astronomical_tz_offset_rejected(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "tz_offset": str(10**20),  # datetime overflow
            "player_ids[]": ["1"],
            "scores[]": ["10"],
            "lines[]": ["0"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert _count("cups") == 0


def test_cup_no_scores_rejected(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups", data={"date": "2026-03-15T20:00"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"at least one player" in response.data
    assert _count("cups") == 0


def test_update_nonexistent_cup_404(client):
    assert client.post("/cups/424242/edit", data={"date": "2026-03-15T20:00"}).status_code == 404


@pytest.mark.xfail(
    strict=True,
    reason="BUG-1: non-numeric scores[] -> ValueError in parse_scores_from_form -> 500",
)
def test_cup_non_numeric_score_rejected_gracefully(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["1"], "scores[]": ["abc"], "lines[]": ["0"]},
        follow_redirects=True,
    )
    assert response.status_code < 500
    assert _count("cups") == 0


@pytest.mark.xfail(
    strict=True,
    reason="BUG-2: non-numeric player_ids[] -> ValueError in parse_scores_from_form -> 500",
)
def test_cup_non_numeric_player_id_rejected_gracefully(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["abc"], "scores[]": ["10"], "lines[]": ["0"]},
        follow_redirects=True,
    )
    assert response.status_code < 500
    assert _count("cups") == 0


@pytest.mark.xfail(
    strict=True,
    reason="BUG-3: score beyond SQLite INTEGER range -> OverflowError mid-transaction -> 500",
)
def test_cup_score_beyond_sqlite_integer_rejected_gracefully(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "player_ids[]": ["1"],
            "scores[]": [SQLITE_INT_OVERFLOW],
            "lines[]": ["0"],
        },
        follow_redirects=True,
    )
    assert response.status_code < 500
    assert _count("cups") == 0


# =============================================================================
# Standalone scores
# =============================================================================


def _make_completed_cup(client):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["1"], "scores[]": ["50"], "lines[]": ["0"]},
    )


def test_score_missing_fields_rejected(client):
    _make_completed_cup(client)
    response = client.post("/scores", data={"cup_id": "1"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"required" in response.data
    assert _count("scores") == 1  # only the original score


def test_score_for_nonexistent_cup_not_persisted(client):
    # FK enforcement (PRAGMA foreign_keys=ON) rejects the insert; the app
    # catches IntegrityError and flashes instead of crashing.
    _make_completed_cup(client)
    response = client.post(
        "/scores", data={"cup_id": "999", "player_id": "2", "score": "10"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert _count("scores", "cup_id = 999") == 0


def test_score_for_nonexistent_player_not_persisted(client):
    _make_completed_cup(client)
    response = client.post(
        "/scores", data={"cup_id": "1", "player_id": "999", "score": "10"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert _count("scores", "player_id = 999") == 0


def test_update_nonexistent_score_404(client):
    assert client.post("/scores/424242/edit", data={"score": "5"}).status_code == 404


@pytest.mark.xfail(
    strict=True,
    reason="BUG-4: non-numeric score -> ValueError in create_score -> 500",
)
def test_score_non_numeric_score_rejected_gracefully(client):
    _make_completed_cup(client)
    response = client.post(
        "/scores", data={"cup_id": "1", "player_id": "2", "score": "abc"}, follow_redirects=True
    )
    assert response.status_code < 500
    assert _count("scores") == 1


@pytest.mark.xfail(
    strict=True,
    reason="BUG-4: non-numeric cup_id -> ValueError in create_score -> 500",
)
def test_score_non_numeric_cup_id_rejected_gracefully(client):
    _make_completed_cup(client)
    response = client.post(
        "/scores", data={"cup_id": "abc", "player_id": "2", "score": "10"}, follow_redirects=True
    )
    assert response.status_code < 500
    assert _count("scores") == 1


@pytest.mark.xfail(
    strict=True,
    reason="BUG-5: non-numeric score -> ValueError in update_score -> 500",
)
def test_update_score_non_numeric_rejected_gracefully(client):
    _make_completed_cup(client)
    response = client.post("/scores/1/edit", data={"score": "abc"}, follow_redirects=True)
    assert response.status_code < 500
    conn = get_connection()
    row = conn.execute("SELECT score FROM scores WHERE id = 1").fetchone()
    conn.close()
    assert row["score"] == 50  # untouched


# =============================================================================
# Cup session endpoints
# =============================================================================


def test_session_create_with_nonexistent_player_not_persisted(client):
    # FK violation is caught; no half-created in_progress cup may remain.
    create_player(client, "Alice")
    response = client.post(
        "/cup-session/new", data={"player_ids[]": ["999"]}, follow_redirects=True
    )
    assert response.status_code == 200
    assert _count("cups", "status = 'in_progress'") == 0
    assert _count("cup_players") == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG-6: non-numeric player_ids[] -> ValueError in cup_session_create -> 500; "
        "also leaks an open write transaction (cup INSERT uncommitted, conn not closed)"
    ),
)
def test_session_create_non_numeric_player_id_rejected_gracefully(client):
    create_player(client, "Alice")
    response = client.post(
        "/cup-session/new", data={"player_ids[]": ["abc"]}, follow_redirects=True
    )
    assert response.status_code < 500
    assert _count("cups", "status = 'in_progress'") == 0


@pytest.mark.xfail(
    strict=True,
    reason="BUG-7: non-numeric scores[] on session submit -> ValueError (parse_scores_from_form) -> 500",
)
def test_session_submit_non_numeric_score_rejected_gracefully(client):
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={"player_ids[]": ["1"], "scores[]": ["abc"], "lines[]": ["0"]},
        follow_redirects=True,
    )
    assert response.status_code < 500
    assert _count("scores", "cup_id = ?", (cup_id,)) == 0
    assert _count("cups", "id = ? AND status = 'in_progress'", (cup_id,)) == 1


def test_session_submit_with_nonexistent_player_keeps_cup_open(client):
    # FK violation on the score insert rolls everything back: the cup must
    # stay in_progress with no scores.
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={"player_ids[]": ["999"], "scores[]": ["10"], "lines[]": ["0"]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert _count("scores", "cup_id = ?", (cup_id,)) == 0
    assert _count("cups", "id = ? AND status = 'in_progress'", (cup_id,)) == 1


def test_half_veto_unknown_player_id_400(client):
    cup_id = _start_session(client)
    response = client.post(f"/cup-session/{cup_id}/half-veto", json={"player_id": "abc"})
    assert response.status_code == 400


def test_half_veto_non_json_body_is_4xx(client):
    cup_id = _start_session(client)
    response = client.post(f"/cup-session/{cup_id}/half-veto", data={"player_id": "1"})
    assert 400 <= response.status_code < 500  # 415 Unsupported Media Type


def test_next_race_missing_map_400(client):
    cup_id = _start_session(client)
    response = client.post(f"/cup-session/{cup_id}/next-race", json={})
    assert response.status_code == 400
    assert _count("races") == 0


def test_next_race_syntactically_bad_json_is_4xx(client):
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/next-race", data="{not json", content_type="application/json"
    )
    assert 400 <= response.status_code < 500
    assert _count("races") == 0


def test_next_race_absurdly_long_map_name_no_crash(client):
    # Accepted today (no length validation) — the contract is just "no 500".
    cup_id = _start_session(client)
    response = client.post(f"/cup-session/{cup_id}/next-race", json={"map": "X" * 100_000})
    assert response.status_code < 500
    assert client.get(f"/cup-session/{cup_id}").status_code == 200


@pytest.mark.xfail(
    strict=True,
    reason="BUG-8: non-scalar JSON player_id -> sqlite3.InterfaceError binding parameter -> 500",
)
def test_half_veto_non_scalar_player_id_rejected_gracefully(client):
    cup_id = _start_session(client)
    response = client.post(f"/cup-session/{cup_id}/half-veto", json={"player_id": [1]})
    assert 400 <= response.status_code < 500


@pytest.mark.xfail(
    strict=True,
    reason="BUG-9: non-string JSON map -> sqlite3.InterfaceError binding parameter -> 500",
)
def test_next_race_non_string_map_rejected_gracefully(client):
    cup_id = _start_session(client)
    response = client.post(f"/cup-session/{cup_id}/next-race", json={"map": {"nested": "object"}})
    assert 400 <= response.status_code < 500
    assert _count("races") == 0
