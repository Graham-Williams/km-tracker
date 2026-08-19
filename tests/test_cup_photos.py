"""Photo persistence on cups (cup_photos) + the /cups/<id>/photo route.

Saving a photo never requires extraction — a fully manual score submit with
an attached photo must persist it. A malformed photo payload rejects the
whole submit with a flash (never a 500, never a silently-dropped photo).
"""

import base64

from db import get_connection
from helpers import create_player

PHOTO_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-body"
PHOTO_B64 = base64.b64encode(PHOTO_BYTES).decode("ascii")


def _photo_rows(cup_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT image, mime_type, created_at FROM cup_photos WHERE cup_id = ?", (cup_id,)
    ).fetchall()
    conn.close()
    return rows


def _start_session(client, player_ids=("1", "2")):
    create_player(client, "Alice")
    create_player(client, "Bob")
    client.post("/cup-session/new", data={"player_ids[]": list(player_ids)})
    conn = get_connection()
    row = conn.execute("SELECT id FROM cups WHERE status = 'in_progress'").fetchone()
    conn.close()
    return row["id"]


# --- Manual cup create (/cups) ---


def test_photo_persists_on_manual_cup_create(client):
    create_player(client, "Alice")
    response = client.post(
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
    assert response.status_code == 200
    rows = _photo_rows(1)
    assert len(rows) == 1
    assert bytes(rows[0]["image"]) == PHOTO_BYTES
    assert rows[0]["mime_type"] == "image/jpeg"
    assert rows[0]["created_at"]


def test_manual_cup_without_photo_saves_no_photo_row(client):
    create_player(client, "Alice")
    client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["1"], "scores[]": ["50"], "lines[]": ["0"]},
    )
    assert _photo_rows(1) == []


def test_manual_cup_malformed_photo_rejects_whole_submit(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
            "lines[]": ["0"],
            "photo_data": "!!!not-base64!!!",
            "photo_mime": "image/jpeg",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200  # flash + redirect, never 500
    assert b"Photo" in response.data
    conn = get_connection()
    cups = conn.execute("SELECT COUNT(*) FROM cups").fetchone()[0]
    scores = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    conn.close()
    assert cups == 0 and scores == 0


def test_manual_cup_bad_photo_mime_rejects_whole_submit(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={
            "date": "2026-03-15T20:00",
            "player_ids[]": ["1"],
            "scores[]": ["50"],
            "lines[]": ["0"],
            "photo_data": PHOTO_B64,
            "photo_mime": "image/gif",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"JPEG or PNG" in response.data
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM cups").fetchone()[0] == 0
    conn.close()


# --- Live session submit ---


def test_photo_persists_on_session_submit_with_manual_scores(client):
    # Fully manual score entry + photo attach: extraction never ran, the
    # photo must still be saved.
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "player_ids[]": ["1", "2"],
            "scores[]": ["60", "54"],
            "lines[]": ["0", "0"],
            "photo_data": PHOTO_B64,
            "photo_mime": "image/png",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    rows = _photo_rows(cup_id)
    assert len(rows) == 1
    assert bytes(rows[0]["image"]) == PHOTO_BYTES
    assert rows[0]["mime_type"] == "image/png"
    conn = get_connection()
    status = conn.execute("SELECT status FROM cups WHERE id = ?", (cup_id,)).fetchone()["status"]
    conn.close()
    assert status == "completed"


def test_session_submit_malformed_photo_keeps_cup_open(client):
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "player_ids[]": ["1", "2"],
            "scores[]": ["60", "54"],
            "lines[]": ["0", "0"],
            "photo_data": "@@@bad@@@",
            "photo_mime": "image/jpeg",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Photo" in response.data
    conn = get_connection()
    status = conn.execute("SELECT status FROM cups WHERE id = ?", (cup_id,)).fetchone()["status"]
    scores = conn.execute("SELECT COUNT(*) FROM scores WHERE cup_id = ?", (cup_id,)).fetchone()[0]
    conn.close()
    assert status == "in_progress"  # submit rejected wholesale
    assert scores == 0
    assert _photo_rows(cup_id) == []
    # The endpoint isn't wedged: a clean resubmit (no photo) still completes.
    ok = client.post(
        f"/cup-session/{cup_id}/complete",
        data={"player_ids[]": ["1", "2"], "scores[]": ["60", "54"], "lines[]": ["0", "0"]},
        follow_redirects=True,
    )
    assert ok.status_code == 200


# --- Serving ---


def test_cup_photo_served_with_stored_mime(client):
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
    )
    response = client.get("/cups/1/photo")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data == PHOTO_BYTES


def test_cup_photo_404_when_cup_soft_deleted(client):
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
    )
    assert client.get("/cups/1/photo").status_code == 200
    client.post("/cups/1/delete")
    assert client.get("/cups/1/photo").status_code == 404


def test_cup_photo_sends_caching_headers_and_304(client):
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
    )
    response = client.get("/cups/1/photo")
    assert response.status_code == 200
    etag = response.headers.get("ETag")
    assert etag
    assert "max-age=86400" in response.headers.get("Cache-Control", "")
    # Conditional revalidation: matching ETag -> 304 with no body.
    conditional = client.get("/cups/1/photo", headers={"If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.data == b""
    # Non-matching ETag -> full response again.
    fresh = client.get("/cups/1/photo", headers={"If-None-Match": '"nope"'})
    assert fresh.status_code == 200
    assert fresh.data == PHOTO_BYTES


def test_cup_photo_404_when_absent(client):
    create_player(client, "Alice")
    client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["1"], "scores[]": ["50"], "lines[]": ["0"]},
    )
    assert client.get("/cups/1/photo").status_code == 404
    assert client.get("/cups/424242/photo").status_code == 404


def test_cups_page_shows_thumbnail_only_when_photo_exists(client):
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
    )
    client.post(
        "/cups",
        data={"date": "2026-03-16T20:00", "player_ids[]": ["1"], "scores[]": ["40"], "lines[]": ["0"]},
    )
    page = client.get("/cups")
    assert page.status_code == 200
    assert page.data.count(b"/cups/1/photo") > 0
    assert page.data.count(b"/cups/2/photo") == 0


# --- "Photo saved" flash confirmation ---


def test_manual_create_flash_confirms_photo_saved(client):
    create_player(client, "Alice")
    response = client.post(
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
    assert response.status_code == 200
    assert b"photo saved" in response.data


def test_manual_create_without_photo_has_no_photo_flash(client):
    create_player(client, "Alice")
    response = client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["1"], "scores[]": ["50"], "lines[]": ["0"]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"photo saved" not in response.data


def test_session_submit_flash_confirms_photo_saved(client):
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={
            "player_ids[]": ["1", "2"],
            "scores[]": ["60", "54"],
            "lines[]": ["0", "0"],
            "photo_data": PHOTO_B64,
            "photo_mime": "image/jpeg",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"photo saved" in response.data


def test_session_submit_without_photo_has_no_photo_flash(client):
    cup_id = _start_session(client)
    response = client.post(
        f"/cup-session/{cup_id}/complete",
        data={"player_ids[]": ["1", "2"], "scores[]": ["60", "54"], "lines[]": ["0", "0"]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"photo saved" not in response.data


# --- Photo on the cup edit page ---


def test_cup_edit_shows_photo_when_present(client):
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
    )
    page = client.get("/cups/1/edit")
    assert page.status_code == 200
    assert b"/cups/1/photo" in page.data
    assert b"Standings photo" in page.data


def test_cup_edit_hides_photo_when_absent(client):
    create_player(client, "Alice")
    client.post(
        "/cups",
        data={"date": "2026-03-15T20:00", "player_ids[]": ["1"], "scores[]": ["50"], "lines[]": ["0"]},
    )
    page = client.get("/cups/1/edit")
    assert page.status_code == 200
    assert b"/cups/1/photo" not in page.data
    assert b"Standings photo" not in page.data


def test_newest_photo_wins_when_multiple(client):
    # Editing/resubmitting can attach a second photo; the serve route returns
    # the newest one.
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
    )
    newer = b"\x89PNG\r\nnewer-photo"
    conn = get_connection()
    conn.execute(
        "INSERT INTO cup_photos (cup_id, image, mime_type, created_at) VALUES (1, ?, 'image/png', '2026-03-16 00:00:00')",
        (newer,),
    )
    conn.commit()
    conn.close()
    response = client.get("/cups/1/photo")
    assert response.data == newer
    assert response.mimetype == "image/png"


# --- Block tagging (mixed cups) ---------------------------------------------
#
# The single whole-cup photo flow tested above is UNCHANGED by per-block photos:
# every row it writes carries block = NULL, meaning "the cup's photo". The
# per-block behaviour itself lives in test_mixed_cup_block_photos.py.


def test_form_attached_photos_are_stored_blockless(client):
    """Every existing caller of save_cup_photo passes no block, so a pure cup's
    row is byte-identical to what it was before per-block photos existed."""
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
    )
    conn = get_connection()
    blocks = [r["block"] for r in conn.execute("SELECT block FROM cup_photos")]
    conn.close()
    assert blocks == [None]


def test_blockless_route_serves_the_newest_photo_of_any_block(client):
    """/cups/<id>/photo deliberately ignores the block tag — the /cups
    thumbnail and the tests above depend on "newest photo, whatever it is"."""
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
    )
    tagged = b"\xff\xd8block-tagged"
    conn = get_connection()
    conn.execute(
        "INSERT INTO cup_photos (cup_id, image, mime_type, created_at, block) "
        "VALUES (1, ?, 'image/jpeg', '2026-03-16 00:00:00', 2)",
        (tagged,),
    )
    conn.commit()
    conn.close()
    assert client.get("/cups/1/photo").data == tagged
    assert client.get("/cups/1/photo/2").data == tagged
    # ...but the per-block route never falls back to an untagged photo.
    assert client.get("/cups/1/photo/1").status_code == 404
