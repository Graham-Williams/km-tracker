"""POST /extract-scores — photo score extraction + player matching.

The Claude API is never called in tests: app.extract_standings is
monkeypatched (or, for the SDK-wrapping test, extraction.anthropic.Anthropic
is patched where extraction.py imports it). Contract: bad input is a clean
4xx JSON, a missing key is 503, an upstream API failure is 502 — never a 500.
"""

import base64
import time

import anthropic
import pytest

import app as app_module
import extraction
from db import get_connection
from extraction import ExtractionError, Standings, StandingsRow

FAKE_JPEG_B64 = base64.b64encode(b"\xff\xd8fakejpegbytes").decode("ascii")


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _fake_rows(*rows):
    """rows: (position, character, points[, is_highlighted]) tuples -> extractor.

    A 3-tuple omits the highlight flag (defaults False, i.e. the pre-highlight
    behavior where zero highlighted rows fall back to matching all rows).
    """
    parsed = []
    for row in rows:
        position, character, points = row[0], row[1], row[2]
        highlighted = row[3] if len(row) > 3 else False
        parsed.append(
            StandingsRow(
                position=position,
                character=character,
                points=points,
                is_highlighted=highlighted,
            )
        )
    standings = Standings(rows=parsed)

    def fake_extract(image_b64, media_type, edition=None):
        return standings

    return fake_extract


def _setup_players(client, players):
    """players: list of (name, wii_char, switch_char). Returns ids in order."""
    ids = []
    for i, (name, wii_char, switch_char) in enumerate(players, start=1):
        data = {"name": name, "default_cup": "on"}
        if wii_char:
            data["default_character_wii"] = wii_char
        if switch_char:
            data["default_character_switch"] = switch_char
        client.post("/players", data=data)
        ids.append(i)
    return ids


def _start_session(client, player_ids, edition="wii"):
    client.post(
        "/cup-session/new",
        data={"player_ids[]": [str(p) for p in player_ids], "game_edition": edition},
    )
    conn = get_connection()
    row = conn.execute("SELECT id FROM cups WHERE status = 'in_progress'").fetchone()
    conn.close()
    return row["id"]


def _post(client, **overrides):
    payload = {"image": FAKE_JPEG_B64, "mime_type": "image/jpeg"}
    payload.update(overrides)
    return client.post("/extract-scores", json=payload)


# --- Feature gate ---


def test_no_api_key_returns_503(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    response = _post(client, cup_id=1)
    assert response.status_code == 503
    assert "not configured" in response.get_json()["error"]


# --- Happy path (cup_id contract, live session) ---


def test_happy_path_matches_players_by_character(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None), ("Bob", "Yoshi", None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Funky Kong", 60), (2, "Yoshi", 54), (3, "Mario", 40)),
    )
    response = _post(client, cup_id=cup_id)
    assert response.status_code == 200
    data = response.get_json()
    assert data["scores"] == {"1": 60, "2": 54}
    assert data["ambiguous"] == []
    assert data["unmatched_players"] == []
    assert len(data["raw_rows"]) == 3
    assert data["raw_rows"][0] == {
        "position": 1,
        "character": "Funky Kong",
        "points": 60,
        "is_highlighted": False,
    }


def test_matching_is_case_insensitive(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module, "extract_standings", _fake_rows((1, "  funky kong ", 60))
    )
    response = _post(client, cup_id=cup_id)
    assert response.get_json()["scores"] == {"1": 60}


def test_cpu_rows_are_ignored(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Funky Kong", 60), (2, "Bowser", 51), (3, "Peach", 44)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 60}
    assert data["ambiguous"] == []
    assert data["unmatched_players"] == []


def test_shared_character_goes_to_ambiguous(client, api_key, monkeypatch):
    # Alice and Bob both claim Yoshi -> neither is filled.
    _setup_players(client, [("Alice", "Yoshi", None), ("Bob", "Yoshi", None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(app_module, "extract_standings", _fake_rows((1, "Yoshi", 60)))
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {}
    assert data["ambiguous"] == ["Alice", "Bob"]


def test_duplicate_rows_for_character_go_to_ambiguous(client, api_key, monkeypatch):
    # The screen shows Yoshi twice (e.g. a CPU also picked Yoshi) -> ambiguous.
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module, "extract_standings", _fake_rows((1, "Yoshi", 60), (5, "Yoshi", 30))
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {}
    assert data["ambiguous"] == ["Alice"]


def test_player_without_character_is_unmatched(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None), ("Bob", None, None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(app_module, "extract_standings", _fake_rows((1, "Funky Kong", 60)))
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 60}
    assert data["unmatched_players"] == ["Bob"]


def test_player_character_not_on_screen_is_unmatched(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(app_module, "extract_standings", _fake_rows((1, "Mario", 60)))
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {}
    assert data["unmatched_players"] == ["Alice"]


def test_switch_cup_matches_on_switch_character(client, api_key, monkeypatch):
    # Alice mains Funky Kong on Wii but Isabelle on Switch. An mk8dx cup must
    # match against the Switch field.
    _setup_players(client, [("Alice", "Funky Kong", "Isabelle")])
    cup_id = _start_session(client, [1], edition="mk8dx")
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Isabelle", 60), (2, "Funky Kong", 55)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 60}


# --- Highlight-aware matching ---


def test_only_highlighted_rows_are_matched(client, api_key, monkeypatch):
    # Alice=Funky Kong is highlighted; a non-highlighted Funky Kong row (a CPU)
    # also exists. Only the highlighted row's score is used.
    _setup_players(client, [("Alice", "Funky Kong", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Funky Kong", 60, True), (4, "Funky Kong", 30, False)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 60}
    assert data["ambiguous"] == []
    assert data["unmatched_players"] == []


def test_cpu_sharing_human_character_is_ignored(client, api_key, monkeypatch):
    # The bug this feature fixes: a human on Yoshi (highlighted) plus a CPU
    # Yoshi (not highlighted). Pre-highlight this was ambiguous; now the human
    # matches cleanly and the CPU Yoshi is dropped.
    _setup_players(client, [("Lizzy", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Yoshi", 55, True), (6, "Yoshi", 22, False)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 55}
    assert data["ambiguous"] == []


def test_player_whose_character_only_appears_as_cpu_is_unmatched(
    client, api_key, monkeypatch
):
    # Bob mains Bowser but the only Bowser on screen is a CPU (not highlighted).
    # Bob must be left blank (unmatched), NOT auto-filled with the CPU's score.
    _setup_players(client, [("Alice", "Yoshi", None), ("Bob", "Bowser", None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Yoshi", 60, True), (3, "Bowser", 40, False)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 60}
    assert data["unmatched_players"] == ["Bob"]


def test_two_humans_sharing_character_stay_ambiguous(client, api_key, monkeypatch):
    # Genuine collision: two highlighted humans both on Yoshi -> still ambiguous.
    _setup_players(client, [("Alice", "Yoshi", None), ("Bob", "Yoshi", None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Yoshi", 60, True), (2, "Yoshi", 48, True)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {}
    assert data["ambiguous"] == ["Alice", "Bob"]


def test_off_character_human_not_auto_filled_from_cpu_row(client, api_key, monkeypatch):
    # Real cup-56 case: Graham defaults to Toad, but Toad is a CPU on screen
    # (non-highlighted, 33 pts) while he actually played a highlighted Dry
    # Bowser (10 pts). With OTHER rows highlighted, his Toad default must NOT
    # be auto-filled from the CPU Toad row — he's left blank to pick manually.
    _setup_players(client, [("Graham", "Toad", None), ("Lizzy", "Yoshi", None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows(
            (1, "Yoshi", 55, True),       # Lizzy, human
            (2, "Dry Bowser", 10, True),  # Graham actually played this (off-char)
            (7, "Toad", 33, False),       # a CPU on Graham's default character
        ),
    )
    data = _post(client, cup_id=cup_id).get_json()
    # Lizzy (player 2) auto-fills; Graham (player 1) is NOT given the CPU Toad's 33.
    assert data["scores"] == {"2": 55}
    assert 33 not in data["scores"].values()
    assert data["unmatched_players"] == ["Graham"]


def test_two_highlighted_rows_one_character_are_ambiguous(client, api_key, monkeypatch):
    # A single player mains Yoshi but TWO highlighted rows both show Yoshi
    # (e.g. a second human also on Yoshi) -> can't tell which is theirs.
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Yoshi", 60, True), (3, "Yoshi", 40, True)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {}
    assert data["ambiguous"] == ["Alice"]


def test_zero_highlighted_falls_back_to_all_rows(client, api_key, monkeypatch):
    # Robustness: if the model flags NOTHING highlighted (bad detection / old
    # behavior), fall back to matching all rows so we never regress.
    _setup_players(client, [("Alice", "Funky Kong", None), ("Bob", "Yoshi", None)])
    cup_id = _start_session(client, [1, 2])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Funky Kong", 60), (2, "Yoshi", 54), (3, "Mario", 40)),
    )
    data = _post(client, cup_id=cup_id).get_json()
    assert data["scores"] == {"1": 60, "2": 54}
    assert data["ambiguous"] == []
    assert data["unmatched_players"] == []


def test_raw_rows_carry_highlight_flag(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None)])
    cup_id = _start_session(client, [1])
    monkeypatch.setattr(
        app_module,
        "extract_standings",
        _fake_rows((1, "Funky Kong", 60, True), (2, "Bowser", 40, False)),
    )
    raw = _post(client, cup_id=cup_id).get_json()["raw_rows"]
    assert raw[0]["is_highlighted"] is True
    assert raw[1]["is_highlighted"] is False


# --- Edition-aware prompt / plumbing ---


def test_route_passes_cup_edition_to_extractor(client, api_key, monkeypatch):
    # The /extract-scores route must thread the cup's edition into
    # extract_standings so the prompt can describe the right screen cues.
    _setup_players(client, [("Alice", "Funky Kong", "Isabelle")])
    cup_id = _start_session(client, [1], edition="mk8dx")
    seen = {}

    def capturing_extract(image_b64, media_type, edition=None):
        seen["edition"] = edition
        return Standings(rows=[StandingsRow(position=1, character="Isabelle", points=60)])

    monkeypatch.setattr(app_module, "extract_standings", capturing_extract)
    _post(client, cup_id=cup_id)
    assert seen["edition"] == "mk8dx"


def test_build_extraction_prompt_is_edition_specific():
    wii = extraction.build_extraction_prompt("wii")
    mk8 = extraction.build_extraction_prompt("mk8dx")
    unknown = extraction.build_extraction_prompt(None)
    # Wii: opaque-vs-translucent cue, both layouts, and it DOES detect humans.
    assert "Wii" in wii
    assert "opaque" in wii.lower() and "translucent" in wii.lower()
    assert "is_highlighted=true" in wii
    # The dropped "outline box" cue must be gone.
    assert "outline" not in wii.lower()
    # Switch: NO highlight detection — every row left is_highlighted=false, and
    # no P#/badge language.
    assert "Deluxe" in mk8
    assert "is_highlighted=false" in mk8
    assert "P1" not in mk8 and "badge" not in mk8.lower()
    assert "is_highlighted=true" not in mk8
    # Unknown falls back to the (Wii) opaque cue.
    assert "opaque" in unknown.lower()


# --- Manual-form contract (edition + player_ids) ---


def test_manual_contract_edition_and_player_ids(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None), ("Bob", "Yoshi", None)])
    monkeypatch.setattr(
        app_module, "extract_standings", _fake_rows((1, "Funky Kong", 60), (2, "Yoshi", 54))
    )
    response = _post(client, edition="wii", player_ids=[1, 2])
    assert response.status_code == 200
    assert response.get_json()["scores"] == {"1": 60, "2": 54}


def test_manual_contract_unknown_edition_400(client, api_key):
    response = _post(client, edition="mk64", player_ids=[1])
    assert response.status_code == 400


def test_manual_contract_bad_player_ids_400(client, api_key):
    for bad in ([], "1", [True], [{"id": 1}], ["abc"], [2**63]):
        response = _post(client, edition="wii", player_ids=bad)
        assert response.status_code == 400, bad


def test_manual_contract_nonexistent_player_400(client, api_key):
    response = _post(client, edition="wii", player_ids=[424242])
    assert response.status_code == 400


# --- Input validation (never a 500) ---


def test_missing_json_body_400(client, api_key):
    response = client.post("/extract-scores", data="not json", content_type="text/plain")
    assert response.status_code == 400


def test_bad_base64_400(client, api_key):
    response = _post(client, cup_id=1, image="!!!not-base64!!!")
    assert response.status_code == 400


def test_missing_image_400(client, api_key):
    response = client.post(
        "/extract-scores", json={"mime_type": "image/jpeg", "cup_id": 1}
    )
    assert response.status_code == 400


def test_non_string_image_400(client, api_key):
    response = _post(client, cup_id=1, image=[1, 2, 3])
    assert response.status_code == 400


def test_bad_mime_type_400(client, api_key):
    response = _post(client, cup_id=1, mime_type="image/gif")
    assert response.status_code == 400


def test_oversized_image_400(client, api_key):
    big = base64.b64encode(b"x" * (950 * 1024)).decode("ascii")  # > MAX_PHOTO_BYTES
    # Body would exceed MAX_CONTENT_LENGTH too; either rejection is a clean 4xx.
    response = _post(client, cup_id=1, image=big)
    assert 400 <= response.status_code < 500


def test_nonexistent_cup_404(client, api_key):
    response = _post(client, cup_id=424242)
    assert response.status_code == 404


def test_bad_cup_id_400(client, api_key):
    for bad in ("abc", True, [1], 2**63):
        response = _post(client, cup_id=bad)
        assert response.status_code == 400, bad


def test_neither_cup_nor_players_400(client, api_key):
    response = _post(client)
    assert response.status_code == 400


# --- Per-IP throttle on the PAID call ---


def _count_calls(client, monkeypatch, n, cup_id=None):
    """Fire n extractions against a real cup, returning the status codes."""
    calls = []

    def counting(image_b64, media_type, edition=None):
        calls.append(1)
        return Standings(rows=[StandingsRow(position=1, character="Yoshi", points=60)])

    monkeypatch.setattr(app_module, "extract_standings", counting)
    statuses = [_post(client, cup_id=cup_id).status_code for _ in range(n)]
    return statuses, len(calls)


def test_extraction_allows_a_normal_cups_worth_of_reads(client, api_key, monkeypatch):
    """A mixed cup is two photos plus retries; the throttle must never get in
    the way of that. Anything under the limit sails through."""
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    statuses, calls = _count_calls(
        client, monkeypatch, app_module.EXTRACT_RATE_LIMIT_MAX, cup_id=cup_id
    )
    assert statuses == [200] * app_module.EXTRACT_RATE_LIMIT_MAX
    assert calls == app_module.EXTRACT_RATE_LIMIT_MAX


def test_extraction_is_throttled_per_ip(client, api_key, monkeypatch):
    """Each call is billed to Graham's Anthropic key, the stored-photo path
    needs no upload to trigger one, and everyone with the shared house password
    can reach it — so a loop must hit a wall rather than a credit-card bill."""
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    over = app_module.EXTRACT_RATE_LIMIT_MAX + 5
    statuses, calls = _count_calls(client, monkeypatch, over, cup_id=cup_id)

    assert statuses[: app_module.EXTRACT_RATE_LIMIT_MAX] == [200] * app_module.EXTRACT_RATE_LIMIT_MAX
    assert set(statuses[app_module.EXTRACT_RATE_LIMIT_MAX :]) == {429}
    # The point of the exercise: the refused ones never reached the API.
    assert calls == app_module.EXTRACT_RATE_LIMIT_MAX


def test_a_throttled_extraction_says_what_to_do(client, api_key, monkeypatch):
    """photo_score.js renders body.error verbatim, so the 429 has to be JSON
    with a useful sentence rather than an HTML error page."""
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    _count_calls(
        client, monkeypatch, app_module.EXTRACT_RATE_LIMIT_MAX, cup_id=cup_id
    )
    response = _post(client, cup_id=cup_id)
    assert response.status_code == 429
    assert response.is_json
    assert "wait" in response.get_json()["error"].lower()


def test_the_throttle_window_expires(client, api_key, monkeypatch):
    """The bucket is a sliding window, not a permanent lockout."""
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    _count_calls(
        client, monkeypatch, app_module.EXTRACT_RATE_LIMIT_MAX, cup_id=cup_id
    )
    assert _post(client, cup_id=cup_id).status_code == 429

    # Age every recorded call out of the window.
    stale = time.time() - app_module.EXTRACT_RATE_LIMIT_WINDOW - 1
    for ip in app_module._extract_calls:
        app_module._extract_calls[ip] = [stale] * app_module.EXTRACT_RATE_LIMIT_MAX
    assert _post(client, cup_id=cup_id).status_code == 200


def test_a_rejected_request_does_not_spend_quota(client, api_key, monkeypatch):
    """Only calls that are about to reach Anthropic are counted — a malformed
    request costs nothing, so it must not eat a real user's budget."""
    _setup_players(client, [("Alice", "Yoshi", None)])
    cup_id = _start_session(client, [1])
    for _ in range(app_module.EXTRACT_RATE_LIMIT_MAX * 2):
        assert _post(client, cup_id=99999).status_code == 404
    statuses, _calls = _count_calls(client, monkeypatch, 1, cup_id=cup_id)
    assert statuses == [200]


# --- Upstream API failure -> 502 ---


def test_api_error_returns_502(client, api_key, monkeypatch):
    _setup_players(client, [("Alice", "Funky Kong", None)])
    cup_id = _start_session(client, [1])

    def boom(image_b64, media_type, edition=None):
        raise ExtractionError("api down")

    monkeypatch.setattr(app_module, "extract_standings", boom)
    response = _post(client, cup_id=cup_id)
    assert response.status_code == 502
    assert "error" in response.get_json()


def test_extract_standings_wraps_anthropic_errors(monkeypatch):
    """extraction.extract_standings turns any anthropic exception (incl.
    connection errors / timeouts) into ExtractionError."""

    class FakeAPIError(anthropic.APIError):
        def __init__(self):
            Exception.__init__(self, "boom")

    class FakeMessages:
        def parse(self, **kwargs):
            raise FakeAPIError()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(extraction.anthropic, "Anthropic", FakeClient)
    with pytest.raises(ExtractionError):
        extraction.extract_standings(FAKE_JPEG_B64, "image/jpeg")


def test_extract_standings_rejects_unparsable_output(monkeypatch):
    class FakeResponse:
        parsed_output = None

    class FakeMessages:
        def parse(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(extraction.anthropic, "Anthropic", FakeClient)
    with pytest.raises(ExtractionError):
        extraction.extract_standings(FAKE_JPEG_B64, "image/jpeg")
