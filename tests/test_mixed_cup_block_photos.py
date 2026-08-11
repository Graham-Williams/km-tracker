"""Two-photo score capture for MIXED cups (one results screen per console).

A mixed cup is 2 races on one console then 2 on the other, and EACH console's
results screen starts from zero — so there are two screens whose per-player
points must be added. This module covers the machinery that makes that safe:

  * `scores.block1_score`/`block2_score` hold the halves, `scores.score` holds
    the TOTAL, and `score == block1 + block2` is enforced SERVER-side (a
    submitted total that disagrees is overwritten, never trusted).
  * A row with exactly ONE half filled is REJECTED. Guessing the other half is
    precisely the silently-wrong number this feature exists to prevent.
  * Pure cups are untouched: no block inputs, blocks always NULL even when a
    crafted POST supplies them, and the original single-photo panel/flow.
  * Every photo is extracted against exactly ONE base edition — ITS OWN block's
    console. Resolving both editions (or 'mixed') against one screen re-opens
    the character trap: a player's other-console main can be a CPU on this
    screen, and Switch rows carry no highlight to veto the match.

NOTE ON REAL PHOTOS: km-tracker is a PUBLIC repo. Standings photos are pictures
of somebody's living room and must never enter it — every image here is a tiny
synthetic byte string.
"""

import base64

import pytest

import app as appmod
from app import MAX_RACES, RACES_PER_BLOCK, block_edition
from db import get_connection
from maps import MIXED_EDITION, other_edition
from test_cup_session import _create_session, _setup_players
from test_mixed_cups import _fix_flip, _play_half

# Synthetic, not a photograph. Any bytes work — nothing decodes the image.
PHOTO_BYTES = b"\xff\xd8\xff\xe0synthetic-jpeg"
PHOTO_B64 = base64.b64encode(PHOTO_BYTES).decode("ascii")
OTHER_BYTES = b"\xff\xd8\xff\xe0second-synthetic-jpeg"
OTHER_B64 = base64.b64encode(OTHER_BYTES).decode("ascii")


# --- helpers ---------------------------------------------------------------


def _start_mixed(client, monkeypatch, first_edition="wii", player_ids=("1", "2")):
    _fix_flip(monkeypatch, first_edition)
    _setup_players(client)
    _create_session(client, list(player_ids), edition=MIXED_EDITION)
    return 1


def _start_pure(client, edition="wii", player_ids=("1", "2")):
    _setup_players(client)
    _create_session(client, list(player_ids), edition=edition)
    return 1


def _upload(client, cup_id, block, image=PHOTO_B64, mime="image/jpeg"):
    body = {"mime_type": mime}
    if image is not None:
        body["image"] = image
    return client.post(f"/cups/{cup_id}/photo/{block}", json=body)


def _complete(client, cup_id=1, player_ids=("1", "2"), scores=None, b1=None, b2=None, **extra):
    data = {
        "notes": "",
        "tz_offset": "",
        "player_ids[]": list(player_ids),
        "scores[]": list(scores) if scores is not None else [""] * len(player_ids),
        "lines[]": ["0"] * len(player_ids),
    }
    if b1 is not None:
        data["block1_scores[]"] = list(b1)
    if b2 is not None:
        data["block2_scores[]"] = list(b2)
    data.update(extra)
    return client.post(f"/cup-session/{cup_id}/complete", data=data, follow_redirects=True)


def _score_rows(cup_id=1):
    conn = get_connection()
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT player_id, score, line, line_score, block1_score, block2_score "
            "FROM scores WHERE cup_id = ? ORDER BY player_id",
            (cup_id,),
        )
    ]
    conn.close()
    return rows


def _photo_rows(cup_id=1):
    conn = get_connection()
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, image, mime_type, block FROM cup_photos WHERE cup_id = ? ORDER BY id",
            (cup_id,),
        )
    ]
    conn.close()
    return rows


def _cup_status(cup_id=1):
    conn = get_connection()
    row = conn.execute("SELECT status FROM cups WHERE id = ?", (cup_id,)).fetchone()
    conn.close()
    return row["status"]


# =============================================================================
# block_edition: ordinal -> console, for BOTH coin-flip outcomes
# =============================================================================


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_block_edition_follows_the_coin_flip(client, monkeypatch, first_edition):
    _start_mixed(client, monkeypatch, first_edition)
    conn = get_connection()
    cup = conn.execute(
        "SELECT game_edition, first_edition FROM cups WHERE id = 1"
    ).fetchone()
    conn.close()
    assert block_edition(cup, 1) == first_edition
    assert block_edition(cup, 2) == other_edition(first_edition)
    # A block is the console of its FIRST race — same derivation the races use,
    # so a photo tag can never disagree with the cup's flip.
    assert block_edition(cup, 1) == appmod.race_edition(cup, 1)
    assert block_edition(cup, 2) == appmod.race_edition(cup, RACES_PER_BLOCK + 1)


def test_default_character_field_raises_outside_the_base_editions():
    # The dangerous old default: anything != 'wii' silently returned the SWITCH
    # column, so 'mixed'/None read one console's characters against the other
    # console's screen.
    assert appmod.default_character_field("wii") == "default_character_wii"
    assert appmod.default_character_field("mk8dx") == "default_character_switch"
    for bad in (MIXED_EDITION, None, "", "switch"):
        with pytest.raises(ValueError):
            appmod.default_character_field(bad)


# =============================================================================
# Block scores: total == block1 + block2, always
# =============================================================================


def test_blocks_are_stored_and_sum_to_the_total(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["88", "70"], b1=["46", "30"], b2=["42", "40"])
    rows = _score_rows()
    assert [(r["block1_score"], r["block2_score"], r["score"]) for r in rows] == [
        (46, 42, 88),
        (30, 40, 70),
    ]
    # line_score follows the total on a lineless cup.
    assert [r["line_score"] for r in rows] == [88, 70]


def test_a_lying_submitted_total_is_overwritten_by_the_block_sum(client, monkeypatch):
    """The server is authoritative. A stale/crafted total that disagrees with
    the halves must never be what gets recorded, or the invariant is a lie."""
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["999", "1"], b1=["46", "30"], b2=["42", "40"])
    rows = _score_rows()
    assert [r["score"] for r in rows] == [88, 70]
    assert all(r["block1_score"] + r["block2_score"] == r["score"] for r in rows)


def test_blocks_with_a_blank_total_still_record_the_row(client, monkeypatch):
    """No-JS / a stale page can submit the halves with the calculated total
    still blank. parse_scores_from_form SKIPS such a row, so it must be created
    from the blocks rather than dropped."""
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["", ""], b1=["46", "30"], b2=["42", "40"])
    assert _cup_status() == "completed"
    rows = _score_rows()
    assert [(r["player_id"], r["score"]) for r in rows] == [(1, 88), (2, 70)]


def test_a_mix_of_blocked_and_blank_rows_keeps_positional_alignment(client, monkeypatch):
    """One player's total typed by hand, another's derived from blocks, a third
    left out entirely — the rows must not shift onto the wrong players."""
    _start_mixed(client, monkeypatch, player_ids=("1", "2", "3"))
    _complete(
        client,
        player_ids=("1", "2", "3"),
        scores=["55", "", "40"],
        b1=["", "30", "18"],
        b2=["", "40", "22"],
    )
    rows = _score_rows()
    assert [(r["player_id"], r["score"], r["block1_score"]) for r in rows] == [
        (1, 55, None),
        (2, 70, 30),
        (3, 40, 18),
    ]


def test_one_block_only_is_rejected_and_writes_nothing(client, monkeypatch):
    """Half a breakdown can't satisfy total == b1 + b2, and guessing the other
    half is the exact failure class this feature prevents."""
    _start_mixed(client, monkeypatch)
    response = _complete(client, scores=["88", "70"], b1=["46", "30"], b2=["42", ""])
    assert response.status_code == 200
    assert "Enter both block scores" in response.get_data(as_text=True)
    assert _cup_status() == "in_progress"
    assert _score_rows() == []


def test_blocks_with_no_total_and_no_blocks_anywhere_is_still_rejected(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    response = _complete(client, scores=["", ""], b1=["", ""], b2=["", ""])
    assert "At least one player must have a score" in response.get_data(as_text=True)
    assert _cup_status() == "in_progress"


def test_misaligned_block_arrays_are_rejected(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    response = _complete(client, scores=["88", "70"], b1=["46"], b2=["42", "40"])
    assert "misaligned" in response.get_data(as_text=True).lower()
    assert _cup_status() == "in_progress"


def test_non_numeric_block_scores_are_rejected(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    response = _complete(client, scores=["88", "70"], b1=["4x", "30"], b2=["42", "40"])
    assert _cup_status() == "in_progress"
    assert "whole numbers" in response.get_data(as_text=True)


def test_overflowing_block_sum_is_rejected_cleanly(client, monkeypatch):
    """Two in-range halves can still add past SQLite's INTEGER range; that must
    be a clean flash, not an OverflowError 500 mid-transaction."""
    _start_mixed(client, monkeypatch)
    big = str(2**62)
    response = _complete(client, scores=[""], player_ids=("1",), b1=[big], b2=[big])
    assert response.status_code == 200
    assert _cup_status() == "in_progress"
    assert _score_rows() == []


def test_ties_are_validated_against_the_block_derived_total(client, monkeypatch):
    """Blocks resolve BEFORE validation, so a tiebreaker is checked against the
    real total — not the (possibly blank/stale) submitted one."""
    _start_mixed(client, monkeypatch)
    response = _complete(
        client,
        scores=["", ""],
        b1=["40", "30"],
        b2=["30", "40"],
        **{"tiebreakers[]": ["1"]},
    )
    assert response.status_code == 200
    assert _cup_status() == "completed"
    rows = _score_rows()
    assert [r["score"] for r in rows] == [70, 70]


def test_mixed_cup_with_blocks_stays_lineless(client, monkeypatch):
    """Mixed cups never carry the line handicap — blocks change nothing there."""
    conn = get_connection()
    conn.close()
    _fix_flip(monkeypatch, "wii")
    for name in ("Alice", "Bob", "Carol"):
        client.post("/players", data={"name": name, "default_cup": "on", "has_line": "on"})
    client.post("/players/1", data={"name": "Alice", "line": "5", "has_line": "on"})
    _create_session(client, ["1", "2", "3"], edition=MIXED_EDITION)
    _complete(
        client,
        player_ids=("1", "2", "3"),
        scores=["", "", ""],
        b1=["40", "30", "20"],
        b2=["30", "20", "10"],
        **{"lines[]": ["5", "5", "5"]},
    )
    rows = _score_rows()
    assert [r["line"] for r in rows] == [0, 0, 0]
    assert [r["line_score"] for r in rows] == [70, 50, 30]
    conn = get_connection()
    changes = conn.execute("SELECT COUNT(*) AS n FROM line_changes").fetchone()["n"]
    conn.close()
    assert changes == 0


def test_roster_freshness_guard_still_rejects_a_stale_form_with_blocks(client, monkeypatch):
    """A form carrying block arrays for a roster that has since changed must be
    rejected exactly like any other stale completion — nothing written."""
    _start_mixed(client, monkeypatch, player_ids=("1", "2"))
    client.post("/cup-session/1/players/add", data={"player_id": "3"})
    response = _complete(client, scores=["", ""], b1=["46", "30"], b2=["42", "40"])
    assert "roster changed" in response.get_data(as_text=True)
    assert _cup_status() == "in_progress"
    assert _score_rows() == []


# =============================================================================
# Pure cups are untouched
# =============================================================================


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_pure_cups_store_null_blocks_even_when_blocks_are_posted(client, edition):
    """Hostile input: a crafted POST supplying block arrays at a pure cup must
    leave the columns NULL — and must not rewrite the total either."""
    _start_pure(client, edition)
    _complete(client, scores=["88", "70"], b1=["1", "2"], b2=["3", "4"])
    rows = _score_rows()
    assert [(r["score"], r["block1_score"], r["block2_score"]) for r in rows] == [
        (88, None, None),
        (70, None, None),
    ]


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_pure_completion_page_has_no_block_inputs(client, edition):
    _start_pure(client, edition)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert "block1_scores[]" not in page
    assert "block2_scores[]" not in page
    # The original single-photo panel, unchanged — including the hidden fields
    # that carry the photo on the form.
    assert 'id="photo-score"' in page
    assert 'id="photo-blocks"' not in page
    assert 'name="photo_data"' in page
    assert "initPhotoScore(" in page
    assert "initBlockPhoto(" not in page


def test_manual_cup_create_ignores_block_arrays(client):
    """/cups (the manual form) has no block concept at all."""
    from helpers import create_player

    create_player(client, "Alice")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
            "lines[]": ["0"],
            "block1_scores[]": ["10"],
            "block2_scores[]": ["40"],
        },
        follow_redirects=True,
    )
    rows = _score_rows()
    assert [(r["score"], r["block1_score"], r["block2_score"]) for r in rows] == [
        (50, None, None)
    ]


# =============================================================================
# The mixed completion page
# =============================================================================


def test_mixed_completion_page_shows_the_block_breakdown(client, monkeypatch):
    _start_mixed(client, monkeypatch, "mk8dx")
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert "block1_scores[]" in page
    assert "block2_scores[]" in page
    # Real console names in PLAYED order (Switch first here), never "half 1".
    rows_section = page.split('id="score-rows"')[1].split("</div>")[0]
    assert 'placeholder="Switch"' in rows_section
    assert 'placeholder="Wii"' in rows_section
    # The total is calculated: readonly, never disabled (a disabled input isn't
    # submitted, which would trip the player/score misalignment guard).
    assert 'class="score-input" readonly' in page
    assert 'class="score-input" disabled' not in page
    # Mixed cups stay lineless.
    assert 'name="lines[]" value="0"' in page
    assert 'class="line-input"' not in page


def test_mixed_completion_form_carries_no_photo_payload(client, monkeypatch):
    """Each photo POSTs on its own request. If the form ever regrew a photo
    field, two base64 photos could push the body past MAX_CONTENT_LENGTH and
    413 with a flash-less error page."""
    _start_mixed(client, monkeypatch)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert 'name="photo_data"' not in page
    assert 'name="photo_mime"' not in page
    assert 'id="photo-blocks"' in page
    assert 'id="photo-block-1"' in page
    assert 'id="photo-block-2"' in page
    assert "initPhotoScore(" not in page


def test_mixed_completion_page_replays_saved_blocks(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["", ""], b1=["46", "30"], b2=["42", "40"])
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    assert 'value="46"' in page
    assert 'value="42"' in page
    assert 'value="88"' in page


def test_mixed_completion_panels_are_labelled_in_played_order(client, monkeypatch):
    _start_mixed(client, monkeypatch, "mk8dx")
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    panel1 = page.split('id="photo-block-1"')[1].split('id="photo-block-2"')[0]
    panel2 = page.split('id="photo-block-2"')[1]
    assert "Switch" in panel1 and "races 1–2" in panel1
    assert "Wii" in panel2 and "races 3–4" in panel2


def test_completed_mixed_cup_page_shows_saved_photo_state(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _upload(client, 1, 1)
    page = client.get("/cup-session/1/complete").get_data(as_text=True)
    panel1 = page.split('id="photo-block-1"')[1].split('id="photo-block-2"')[0]
    panel2 = page.split('id="photo-block-2"')[1]
    assert "photo saved ✓" in panel1
    assert "Replace photo" in panel1
    assert "photo saved ✓" not in panel2
    assert "/cups/1/photo/1" in panel1


# =============================================================================
# Per-block photo upload
# =============================================================================


def test_upload_stores_a_block_tagged_photo(client, monkeypatch):
    _start_mixed(client, monkeypatch, "wii")
    response = _upload(client, 1, 1)
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["block"] == 1
    assert body["edition"] == "wii"
    assert body["label"] == "Wii"
    rows = _photo_rows()
    assert len(rows) == 1
    assert bytes(rows[0]["image"]) == PHOTO_BYTES
    assert rows[0]["block"] == 1


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_upload_labels_the_block_with_its_own_console(client, monkeypatch, first_edition):
    _start_mixed(client, monkeypatch, first_edition)
    assert _upload(client, 1, 1).get_json()["edition"] == first_edition
    assert _upload(client, 1, 2).get_json()["edition"] == other_edition(first_edition)


def test_replacing_a_block_photo_keeps_the_other_block_intact(client, monkeypatch):
    """Append-only: replacing is another INSERT and the newest row per block
    wins. The other block is untouched, and the legacy blockless route keeps
    returning the newest photo of ANY block."""
    _start_mixed(client, monkeypatch)
    _upload(client, 1, 2, image=OTHER_B64)
    _upload(client, 1, 1, image=PHOTO_B64)
    _upload(client, 1, 1, image=OTHER_B64)  # replace block 1

    assert len(_photo_rows()) == 3  # nothing deleted
    assert client.get("/cups/1/photo/1").data == OTHER_BYTES
    assert client.get("/cups/1/photo/2").data == OTHER_BYTES
    # The blockless route is deliberately "newest of any block" — /cups' single
    # thumbnail and the existing tests rely on it.
    assert client.get("/cups/1/photo").status_code == 200


def test_block_photo_route_serves_only_that_block(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _upload(client, 1, 1, image=PHOTO_B64)
    assert client.get("/cups/1/photo/1").data == PHOTO_BYTES
    assert client.get("/cups/1/photo/2").status_code == 404


def test_block_photo_route_sends_caching_headers(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _upload(client, 1, 1)
    response = client.get("/cups/1/photo/1")
    etag = response.headers["ETag"]
    assert response.headers["Cache-Control"] == "private, max-age=86400"
    again = client.get("/cups/1/photo/1", headers={"If-None-Match": etag})
    assert again.status_code == 304


@pytest.mark.parametrize("block", [0, 3, 99])
def test_out_of_range_blocks_are_rejected(client, monkeypatch, block):
    _start_mixed(client, monkeypatch)
    assert _upload(client, 1, block).status_code == 400
    assert client.get(f"/cups/1/photo/{block}").status_code == 404
    assert _photo_rows() == []


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_upload_to_a_pure_cup_is_rejected(client, edition):
    """A pure cup keeps the single whole-cup photo flow — one door per shape."""
    _start_pure(client, edition)
    assert _upload(client, 1, 1).status_code == 404
    assert _photo_rows() == []


def test_upload_to_a_cancelled_cup_is_rejected(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    client.post("/cup-session/1/cancel")
    assert _upload(client, 1, 1).status_code == 404
    assert _photo_rows() == []


def test_upload_to_a_deleted_cup_is_rejected(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["", ""], b1=["40", "30"], b2=["30", "20"])
    client.post("/cups/1/delete")
    assert _upload(client, 1, 1).status_code == 404
    assert _photo_rows() == []


def test_upload_to_a_nonexistent_cup_is_rejected(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    assert _upload(client, 4242, 1).status_code == 404


def test_upload_to_a_completed_mixed_cup_is_allowed(client, monkeypatch):
    """A skipped swap photo can still be added after the cup is recorded, and a
    bad one swapped out."""
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["", ""], b1=["40", "30"], b2=["30", "20"])
    assert _upload(client, 1, 1).status_code == 200
    assert len(_photo_rows()) == 1


def test_upload_rejects_a_bad_mime_type(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    response = _upload(client, 1, 1, mime="image/gif")
    assert response.status_code == 400
    assert "JPEG or PNG" in response.get_json()["error"]
    assert _photo_rows() == []


def test_upload_rejects_non_base64(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    response = _upload(client, 1, 1, image="!!!not-base64!!!")
    assert response.status_code == 400
    assert _photo_rows() == []


def test_upload_rejects_a_missing_image(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    assert _upload(client, 1, 1, image=None).status_code == 400
    assert _photo_rows() == []


def test_upload_rejects_an_oversized_photo(client, monkeypatch):
    """Two caps guard this route and BOTH must refuse to write. Anything past
    MAX_PHOTO_BYTES is necessarily past MAX_CONTENT_LENGTH once base64-encoded
    (4/3 expansion vs a 900 KB / 1 MB pair), so the body cap fires first with a
    413 — which is exactly why the client downscales and why each photo gets its
    own request instead of two riding one form."""
    _start_mixed(client, monkeypatch)
    huge = base64.b64encode(b"\xff" * (appmod.MAX_PHOTO_BYTES + 1)).decode("ascii")
    response = _upload(client, 1, 1, image=huge)
    assert response.status_code in (400, 413)
    assert _photo_rows() == []


def test_decode_photo_caps_the_decoded_size(client):
    """The route's own cap, unit-tested past the body cap that shadows it."""
    oversized = base64.b64encode(b"\xff" * (appmod.MAX_PHOTO_BYTES + 1)).decode("ascii")
    with pytest.raises(appmod.InvalidInput):
        appmod.decode_photo(oversized, "image/jpeg")


def test_upload_rejects_a_non_json_body(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    response = client.post("/cups/1/photo/1", data="not json")
    assert response.status_code == 400
    assert _photo_rows() == []


# =============================================================================
# The race page: capture at the swap
# =============================================================================


def test_race_two_offers_the_finishing_console_photo_button(client, monkeypatch):
    _start_mixed(client, monkeypatch, "mk8dx")
    _play_half(client, 1, "mk8dx", count=1)  # race 1 done -> the race-2 page
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="photo-block-1"' in page
    # Labelled with the console actually finishing, not the one coming up.
    panel = page.split('id="photo-block-1"')[1].split("</div>")[0]
    assert "Switch" in panel
    assert "initBlockPhoto(" in page


def test_race_two_button_shows_the_saved_state(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _play_half(client, 1, "wii", count=1)
    _upload(client, 1, 1)
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert "photo saved ✓" in page
    assert "Replace photo" in page


def test_swap_modal_carries_the_capture_control_and_stays_skippable(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _play_half(client, 1, "wii")  # both first-half races -> the race-3 page
    page = client.get("/cup-session/1").get_data(as_text=True)
    modal = page.split('id="swap-reminder-modal"')[1].split("<!-- Cancel Cup Modal")[0]
    assert 'data-photo-input="photo-take-1"' in modal
    assert "Skip for now" in modal
    assert 'id="swap-reminder-ok-btn"' in modal


def test_race_one_and_four_have_no_capture_control(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    assert 'id="photo-block-1"' not in client.get("/cup-session/1").get_data(as_text=True)
    _play_half(client, 1, "wii")
    _play_half(client, 1, "mk8dx", count=1, offset=0)  # race 3 done -> race-4 page
    page = client.get("/cup-session/1").get_data(as_text=True)
    assert 'id="photo-block-1"' not in page


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_pure_cup_race_page_never_offers_block_capture(client, edition):
    _start_pure(client, edition)
    for n in range(MAX_RACES):
        page = client.get("/cup-session/1").get_data(as_text=True)
        assert 'id="photo-block-1"' not in page
        assert "initBlockPhoto(" not in page
        client.post("/cup-session/1/next-race", json={"map": _nth_course(edition, n)})


def _nth_course(edition, n):
    from maps import TRACK_SETS

    return TRACK_SETS[edition][n]


# =============================================================================
# Extraction, per block — the per-edition character rule
# =============================================================================


def _fake_standings(*rows):
    from extraction import Standings, StandingsRow

    standings = Standings(
        rows=[
            StandingsRow(position=p, character=c, points=pts, is_highlighted=hl)
            for p, c, pts, hl in rows
        ]
    )
    seen = {}

    def fake_extract(image_b64, media_type, edition=None):
        seen["edition"] = edition
        seen["image"] = image_b64
        return standings

    return fake_extract, seen


def _two_console_players(client):
    """Two players whose Wii and Switch mains DIFFER — the whole point of
    resolving one edition per photo."""
    for name, wii_char, switch_char in [
        ("Alice", "Baby Peach", "Daisy"),
        ("Bob", "Funky Kong", "Mario"),
    ]:
        client.post(
            "/players",
            data={
                "name": name,
                "default_cup": "on",
                "default_character_wii": wii_char,
                "default_character_switch": switch_char,
            },
        )


def _start_mixed_with_characters(client, monkeypatch, first_edition="wii"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _fix_flip(monkeypatch, first_edition)
    _two_console_players(client)
    _create_session(client, ["1", "2"], edition=MIXED_EDITION)
    return 1


def _extract(client, **payload):
    return client.post("/extract-scores", json=payload)


@pytest.mark.parametrize("first_edition", ["wii", "mk8dx"])
def test_each_block_resolves_its_own_console_edition(client, monkeypatch, first_edition):
    _start_mixed_with_characters(client, monkeypatch, first_edition)
    fake, seen = _fake_standings((1, "Baby Peach", 40, True))
    monkeypatch.setattr(appmod, "extract_standings", fake)

    _extract(client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=1)
    assert seen["edition"] == first_edition
    _extract(client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=2)
    assert seen["edition"] == other_edition(first_edition)
    # Never 'mixed' — that would drop the prompt and the character lookup into
    # their unknown-edition branches.
    assert seen["edition"] != MIXED_EDITION


def test_a_block_photo_fills_from_that_blocks_character_column(client, monkeypatch):
    """THE trap this feature has to defuse: Alice mains Baby Peach on Wii and
    Daisy on Switch. On the SWITCH screen a Baby Peach CPU sits at 39 — she must
    get Daisy's 38, never the CPU's 39 (and Switch rows carry no highlight, so
    only reading the right column can save her)."""
    _start_mixed_with_characters(client, monkeypatch, "wii")  # block 2 = Switch
    fake, _seen = _fake_standings(
        (1, "Daisy", 38, False), (2, "Baby Peach", 39, False), (3, "Mario", 30, False)
    )
    monkeypatch.setattr(appmod, "extract_standings", fake)

    body = _extract(
        client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=2
    ).get_json()
    assert body["partial_half"] is False  # this photo IS the whole of block 2
    assert body["block"] == 2
    assert body["scores"] == {"1": 38, "2": 30}


def test_a_block_one_photo_uses_the_wii_column(client, monkeypatch):
    _start_mixed_with_characters(client, monkeypatch, "wii")
    fake, _seen = _fake_standings(
        (1, "Baby Peach", 45, True), (2, "Funky Kong", 41, True), (3, "Daisy", 60, False)
    )
    monkeypatch.setattr(appmod, "extract_standings", fake)

    body = _extract(
        client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=1
    ).get_json()
    assert body["scores"] == {"1": 45, "2": 41}


def test_blockless_mixed_extraction_still_suppresses_autofill(client, monkeypatch):
    """The legacy blockless path is unchanged: it would be filling a cup TOTAL
    from one console's half, so it fills nothing."""
    _start_mixed_with_characters(client, monkeypatch, "wii")
    fake, _seen = _fake_standings((1, "Daisy", 38, False), (2, "Mario", 30, False))
    monkeypatch.setattr(appmod, "extract_standings", fake)

    body = _extract(client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1).get_json()
    assert body["partial_half"] is True
    assert body["scores"] == {}
    assert body["block"] is None
    assert len(body["raw_rows"]) == 2


def test_extract_reads_the_photo_already_stored_for_a_block(client, monkeypatch):
    """"Read scores from this photo": no image in the body — the server reads
    the swap photo it already holds."""
    _start_mixed_with_characters(client, monkeypatch, "wii")
    _upload(client, 1, 1, image=PHOTO_B64)
    fake, seen = _fake_standings((1, "Baby Peach", 45, True))
    monkeypatch.setattr(appmod, "extract_standings", fake)

    body = _extract(client, cup_id=1, block=1).get_json()
    assert body["scores"] == {"1": 45}
    assert base64.b64decode(seen["image"]) == PHOTO_BYTES
    assert seen["edition"] == "wii"


def test_extract_without_an_image_404s_when_that_block_has_no_photo(client, monkeypatch):
    _start_mixed_with_characters(client, monkeypatch)
    monkeypatch.setattr(appmod, "extract_standings", _fake_standings()[0])
    response = _extract(client, cup_id=1, block=2)
    assert response.status_code == 404


@pytest.mark.parametrize("edition", ["wii", "mk8dx"])
def test_extract_rejects_a_block_on_a_pure_cup(client, monkeypatch, edition):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _two_console_players(client)
    _create_session(client, ["1", "2"], edition=edition)
    response = _extract(client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=1)
    assert response.status_code == 400


@pytest.mark.parametrize("block", [0, 3, "x", True, 1.5])
def test_extract_rejects_an_invalid_block(client, monkeypatch, block):
    _start_mixed_with_characters(client, monkeypatch)
    response = _extract(client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=block)
    assert response.status_code == 400


def test_extract_rejects_a_block_on_the_manual_path(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _two_console_players(client)
    response = _extract(
        client, image=PHOTO_B64, mime_type="image/jpeg", edition="wii", player_ids=[1], block=1
    )
    assert response.status_code == 400


def test_extraction_never_submits_or_writes_a_score(client, monkeypatch):
    """Extraction is an editing aid only: it must not touch the DB."""
    _start_mixed_with_characters(client, monkeypatch)
    fake, _seen = _fake_standings((1, "Baby Peach", 45, True))
    monkeypatch.setattr(appmod, "extract_standings", fake)
    _extract(client, image=PHOTO_B64, mime_type="image/jpeg", cup_id=1, block=1)
    assert _score_rows() == []
    assert _cup_status() == "in_progress"


# =============================================================================
# Editing surfaces clear the breakdown rather than let it contradict the total
# =============================================================================


def test_editing_a_cup_nulls_the_block_breakdown(client, monkeypatch):
    """/cups/<id>/edit only knows the total, so keeping the old halves would
    leave block1 + block2 != score."""
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["", ""], b1=["46", "30"], b2=["42", "40"])
    client.post(
        "/cups/1/edit",
        data={
            "date": "2026-03-15T20:00",
            "notes": "",
            "tz_offset": "",
            "player_ids[]": ["1", "2"],
            "scores[]": ["90", "70"],
            "lines[]": ["0", "0"],
        },
        follow_redirects=True,
    )
    rows = _score_rows()
    assert [(r["score"], r["block1_score"], r["block2_score"]) for r in rows] == [
        (90, None, None),
        (70, None, None),
    ]


def test_editing_a_single_score_nulls_the_block_breakdown(client, monkeypatch):
    _start_mixed(client, monkeypatch)
    _complete(client, scores=["", ""], b1=["46", "30"], b2=["42", "40"])
    conn = get_connection()
    score_id = conn.execute(
        "SELECT id FROM scores WHERE cup_id = 1 AND player_id = 1"
    ).fetchone()["id"]
    conn.close()
    client.post(f"/scores/{score_id}/edit", data={"score": "95"}, follow_redirects=True)
    rows = _score_rows()
    assert (rows[0]["score"], rows[0]["block1_score"], rows[0]["block2_score"]) == (
        95,
        None,
        None,
    )


# =============================================================================
# History / edit thumbnails
# =============================================================================


def test_history_shows_a_thumbnail_per_console_block(client, monkeypatch):
    _start_mixed(client, monkeypatch, "mk8dx")
    _upload(client, 1, 1)
    _upload(client, 1, 2)
    _complete(client, scores=["", ""], b1=["40", "30"], b2=["30", "20"])
    page = client.get("/cups").get_data(as_text=True)
    assert "/cups/1/photo/1" in page
    assert "/cups/1/photo/2" in page
    assert "Switch standings photo" in page
    assert "Wii standings photo" in page


def test_cup_edit_page_shows_a_photo_per_console_block(client, monkeypatch):
    _start_mixed(client, monkeypatch, "wii")
    _upload(client, 1, 1)
    _complete(client, scores=["", ""], b1=["40", "30"], b2=["30", "20"])
    page = client.get("/cups/1/edit").get_data(as_text=True)
    assert "/cups/1/photo/1" in page
    assert "Wii standings photo" in page
    # Only the block that HAS a photo is shown.
    assert "/cups/1/photo/2" not in page


def test_pure_cup_history_thumbnail_is_unchanged(client):
    """Regression control: the blockless whole-cup thumbnail still renders from
    the legacy route."""
    from helpers import create_player

    create_player(client, "Alice")
    client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
            "lines[]": ["0"],
            "photo_data": PHOTO_B64,
            "photo_mime": "image/jpeg",
        },
        follow_redirects=True,
    )
    page = client.get("/cups").get_data(as_text=True)
    assert "/cups/1/photo" in page
    assert "/cups/1/photo/1" not in page
