"""The new-cup forms preselect the roster of the last cup.

Both forms (GET /cup-session/new and GET /cups/new) share
app.default_roster_players: the most recent cup that is neither soft-deleted
nor cancelled supplies the preselected players (its live cup_players roster
while in progress, its scored players once finished), and with no such cup,
or one with nobody on it, every player is preselected. The assertions read
the rendered player_ids[] inputs (a checked checkbox per player on the session
form, a hidden input per score row on the manual form) and the allPlayers list
that feeds the "Add a player" select.
"""

import re
from html.parser import HTMLParser

import pytest

from db import get_connection
from helpers import create_cup_with_scores, create_player

FORMS = ["/cup-session/new", "/cups/new"]

NAMES = ["Alice", "Bob", "Carol", "Dave"]


class _PlayerInputs(HTMLParser):
    """Collects the player_ids[] inputs the server rendered into the form."""

    def __init__(self):
        super().__init__()
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "player_ids[]":
            self.inputs.append(attrs)


def _preselected(client, path):
    """Ids of the players preselected on a new-cup form."""
    response = client.get(path)
    assert response.status_code == 200
    parser = _PlayerInputs()
    parser.feed(response.data.decode())
    for attrs in parser.inputs:
        # The session form's rows are checkboxes: preselected means checked.
        if attrs.get("type") == "checkbox":
            assert "checked" in attrs
    return {int(attrs["value"]) for attrs in parser.inputs}


def _addable(client, path):
    """Ids in the allPlayers list the page builds its "Add a player" select from."""
    html = client.get(path).data.decode()
    block = re.search(r"var allPlayers = \[(.*?)\];", html, re.S).group(1)
    return {int(pid) for pid in re.findall(r"\{ id: (\d+),", block)}


def _setup_players(client):
    for name in NAMES:
        create_player(client, name)


def _start_session(client, player_ids):
    client.post(
        "/cup-session/new", data={"player_ids[]": [str(p) for p in player_ids]}
    )


def _complete_session(client, cup_id, player_ids):
    for race_map in ["Coconut Mall", "Rainbow Road", "Moo Moo Meadows", "Koopa Cape"]:
        client.post(f"/cup-session/{cup_id}/next-race", json={"map": race_map})
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "notes": "",
            "tz_offset": "",
            "player_ids[]": [str(p) for p in player_ids],
            "scores[]": [str(100 - 10 * i) for i in range(len(player_ids))],
            "lines[]": ["0"] * len(player_ids),
        },
    )
    assert response.status_code == 302


def _play_session(client, cup_id, player_ids):
    _start_session(client, player_ids)
    _complete_session(client, cup_id, player_ids)


def _set_cup_date(cup_id, date):
    conn = get_connection()
    conn.execute("UPDATE cups SET date = ? WHERE id = ?", (date, cup_id))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("path", FORMS)
def test_preselects_the_roster_of_the_last_session_cup(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 3])
    assert _preselected(client, path) == {1, 3}


@pytest.mark.parametrize("path", FORMS)
def test_preselects_the_roster_of_the_last_manually_entered_cup(client, path):
    """A manual cup writes scores but no cup_players rows; its scored players are its roster."""
    _setup_players(client)
    create_cup_with_scores(client, "2026-03-15T20:00", [(2, 100), (4, 80)])
    assert _preselected(client, path) == {2, 4}


@pytest.mark.parametrize("path", FORMS)
def test_player_outside_the_last_cup_can_still_be_added(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 2])
    assert 3 not in _preselected(client, path)
    assert _addable(client, path) == {1, 2, 3, 4}


@pytest.mark.parametrize("path", FORMS)
def test_the_most_recent_cup_replaces_the_one_before_it(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 2, 3])
    _set_cup_date(1, "2026-03-01 20:00:00")
    _play_session(client, 2, [3, 4])
    assert _preselected(client, path) == {3, 4}


@pytest.mark.parametrize("path", FORMS)
def test_soft_deleted_cup_is_skipped(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 2])
    _set_cup_date(1, "2026-03-01 20:00:00")
    _play_session(client, 2, [3, 4])
    client.post("/cups/2/delete")
    assert _preselected(client, path) == {1, 2}


@pytest.mark.parametrize("path", FORMS)
def test_cancelled_cup_is_skipped(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 2])
    _set_cup_date(1, "2026-03-01 20:00:00")
    _start_session(client, [3, 4])
    client.post("/cup-session/2/cancel")
    assert _preselected(client, path) == {1, 2}


def test_in_progress_cup_counts_on_the_manual_form(client):
    """Only /cups/new can render mid-cup: /cup-session/new redirects to the live cup."""
    _setup_players(client)
    _play_session(client, 1, [1, 2])
    _set_cup_date(1, "2026-03-01 20:00:00")
    _start_session(client, [2, 3, 4])
    assert _preselected(client, "/cups/new") == {2, 3, 4}


@pytest.mark.parametrize("path", FORMS)
def test_latest_date_wins_over_highest_id(client, path):
    """A backdated manual cup has the highest id but is not the last cup."""
    _setup_players(client)
    _play_session(client, 1, [1, 2])
    _set_cup_date(1, "2026-06-01 20:00:00")
    create_cup_with_scores(client, "2026-01-10T20:00", [(3, 100), (4, 80)])
    assert _preselected(client, path) == {1, 2}


def test_mid_cup_roster_edits_are_reflected(client):
    _setup_players(client)
    _start_session(client, [1, 2])
    client.post("/cup-session/1/players/add", data={"player_id": "3"})
    client.post("/cup-session/1/players/remove", data={"player_id": "1"})
    assert _preselected(client, "/cups/new") == {2, 3}
    _complete_session(client, 1, [2, 3])
    for path in FORMS:
        assert _preselected(client, path) == {2, 3}


def _score_id(cup_id, player_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM scores WHERE cup_id = ? AND player_id = ?", (cup_id, player_id)
    ).fetchone()
    conn.close()
    return row["id"]


@pytest.mark.parametrize("path", FORMS)
def test_player_edited_out_of_a_finished_session_cup_is_dropped(client, path):
    """Editing a finished cup rewrites its scores but leaves cup_players as it was."""
    _setup_players(client)
    _play_session(client, 1, [1, 2, 3])
    response = client.post(
        "/cups/1/edit",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["100", "80"],
            "lines[]": ["0", "0"],
        },
    )
    assert response.headers["Location"].endswith("/cups")
    assert _preselected(client, path) == {1, 2}


@pytest.mark.parametrize("path", FORMS)
def test_player_whose_score_was_deleted_from_a_finished_session_cup_is_dropped(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 2, 3])
    client.post(f"/scores/{_score_id(1, 3)}/delete")
    assert _preselected(client, path) == {1, 2}


@pytest.mark.parametrize("path", FORMS)
def test_finished_session_cup_with_no_scores_left_uses_its_session_roster(client, path):
    _setup_players(client)
    _play_session(client, 1, [1, 2])
    for player_id in (1, 2):
        client.post(f"/scores/{_score_id(1, player_id)}/delete")
    assert _preselected(client, path) == {1, 2}


def test_stray_score_on_an_in_progress_cup_does_not_widen_the_roster(client):
    """A standalone score for someone outside the live roster does not add them to it."""
    _setup_players(client)
    _start_session(client, [1, 2])
    client.post("/scores", data={"cup_id": "1", "player_id": "3", "score": "99"})
    assert _score_id(1, 3)
    assert _preselected(client, "/cups/new") == {1, 2}


@pytest.mark.parametrize("path", FORMS)
def test_last_cup_with_nobody_on_it_preselects_every_player(client, path):
    """A manual cup whose only score was deleted has no roster to copy."""
    _setup_players(client)
    create_cup_with_scores(client, "2026-03-15T20:00", [(2, 100)])
    client.post(f"/scores/{_score_id(1, 2)}/delete")
    assert _preselected(client, path) == {1, 2, 3, 4}


@pytest.mark.parametrize("path", FORMS)
def test_no_cups_preselects_every_player(client, path):
    _setup_players(client)
    assert _preselected(client, path) == {1, 2, 3, 4}


@pytest.mark.parametrize("path", FORMS)
def test_only_unusable_cups_preselects_every_player(client, path):
    """With every cup cancelled or deleted there is no last cup to copy."""
    _setup_players(client)
    _start_session(client, [1, 2])
    client.post("/cup-session/1/cancel")
    create_cup_with_scores(client, "2026-03-15T20:00", [(3, 100)])
    client.post("/cups/2/delete")
    assert _preselected(client, path) == {1, 2, 3, 4}


@pytest.mark.parametrize("path", FORMS)
def test_no_players_renders_an_empty_form(client, path):
    assert _preselected(client, path) == set()
    assert _addable(client, path) == set()


@pytest.mark.parametrize("path", FORMS)
def test_preselected_players_are_listed_by_name(client, path):
    for name in ["Zed", "Mia", "Abe"]:
        create_player(client, name)
    _play_session(client, 1, [1, 2, 3])
    html = client.get(path).data.decode()
    parser = _PlayerInputs()
    parser.feed(html)
    # Abe (3), Mia (2), Zed (1)
    assert [int(a["value"]) for a in parser.inputs] == [3, 2, 1]


# --- A mistyped year must not outrank real dates ---


def _stored_date(cup_id):
    conn = get_connection()
    row = conn.execute("SELECT date FROM cups WHERE id = ?", (cup_id,)).fetchone()
    conn.close()
    return row["date"]


@pytest.mark.parametrize("path", FORMS)
def test_cup_created_with_a_mistyped_year_is_not_the_last_cup(client, path):
    """cups.date sorts as text, so year 0026 must be stored zero-padded."""
    _setup_players(client)
    create_cup_with_scores(client, "0026-10-11T20:00", [(3, 9)])
    create_cup_with_scores(client, "2026-10-11T20:00", [(1, 50), (2, 40)])
    assert _stored_date(1) == "0026-10-11 20:00:00"
    assert _preselected(client, path) == {1, 2}


@pytest.mark.parametrize("path", FORMS)
def test_cup_edited_to_a_mistyped_year_is_not_the_last_cup(client, path):
    _setup_players(client)
    create_cup_with_scores(client, "2026-03-15T20:00", [(3, 9)])
    create_cup_with_scores(client, "2026-03-16T20:00", [(1, 50), (2, 40)])
    client.post(
        "/cups/1/edit",
        data={
            "date": "0300-01-01T10:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["3"],
            "scores[]": ["9"],
            "lines[]": ["0"],
        },
    )
    assert _stored_date(1) == "0300-01-01 10:00:00"
    assert _preselected(client, path) == {1, 2}
