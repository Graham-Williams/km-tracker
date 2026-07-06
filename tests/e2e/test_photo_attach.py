"""Photo attach flow on the manual cup form (real browser).

The attach is async (canvas downscale), so this covers the silent-drop guards:
buttons are wired by photo_score.js (they ship disabled), a successful pick
shows the prominent success indicator, and submitting persists the photo
(confirmed by the "photo saved" flash + a 200 from the photo route).

It also covers the round-trip-destroys-the-form guards (field-found bug: a
submit that the server rejects redirects back and wipes the attached photo +
all client state): submit while extraction is in flight is blocked without
auto-resume, submit with every score empty is blocked client-side, and an
extraction failure clears the in-flight hold so a manual submit still works.

The extraction tests flip ANTHROPIC_API_KEY via monkeypatch — the e2e Flask
server runs in this same process, so the template renders the extract URL —
and intercept /extract-scores at the Playwright level, so no request ever
reaches the server (or any real API).
"""

import base64
import json
import os

import pytest

# 1x1 red PNG — any decodable image works; the client re-encodes to JPEG.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# monkeypatching the key only works when the Flask server shares this process.
requires_in_process_server = pytest.mark.skipif(
    bool(os.environ.get("E2E_BASE_URL")),
    reason="needs the in-process e2e server (env-var monkeypatch)",
)


def _create_player(page, base_url, name):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', name)
    page.click('button[type="submit"]')


def _attach_photo(page):
    """Pick the tiny PNG and wait for the attach-success indicator."""
    page.set_input_files(
        "#photo-pick",
        {"name": "standings.png", "mimeType": "image/png", "buffer": TINY_PNG},
    )
    page.locator(".photo-attach-status.is-success").wait_for(state="visible")


def test_photo_attach_success_state_and_persistence(page, base_url):
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")

    page.goto(f"{base_url}/cups/new")

    # photo_score.js wires + enables the buttons (they ship disabled so a dead
    # script can't open an unguarded picker).
    take_btn = page.locator('button[data-photo-input="photo-take"]')
    assert take_btn.is_enabled()

    page.set_input_files(
        "#photo-pick",
        {"name": "standings.png", "mimeType": "image/png", "buffer": TINY_PNG},
    )

    # Prominent attach-success indicator appears once the downscale settles.
    indicator = page.locator(".photo-attach-status.is-success")
    indicator.wait_for(state="visible")
    assert "Photo attached" in indicator.inner_text()
    assert page.locator(".photo-preview").is_visible()

    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')

    # Redirected to /cups with the "photo saved" confirmation flash.
    page.wait_for_url(f"{base_url}/cups")
    assert page.locator(".flash-success", has_text="photo saved").is_visible()

    # The persisted photo is actually servable.
    photo_href = page.locator('a[href$="/photo"]').first.get_attribute("href")
    response = page.request.get(f"{base_url}{photo_href}")
    assert response.status == 200
    assert response.headers.get("content-type", "").startswith("image/")


@requires_in_process_server
def test_submit_during_extraction_blocked_photo_survives(page, base_url, monkeypatch):
    """The field-found bug: submitting mid-extraction must not round-trip.

    The extract request is held un-answered by a Playwright route, the submit
    is blocked client-side (photo + form state survive, no auto-resume), and
    once extraction completes the user-reviewed submit persists the photo.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")

    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")

    # Hold the extract request: the handler stashes the route without
    # resolving it, so the fetch stays in flight until we fulfill it below.
    held = []
    page.route("**/extract-scores", lambda route: held.append(route))

    page.goto(f"{base_url}/cups/new")
    player_ids = page.eval_on_selector_all(
        ".score-row", "rows => rows.map(r => r.dataset.playerId)"
    )

    _attach_photo(page)
    status = page.locator(".photo-status")
    assert "Reading scores from the photo" in status.inner_text()

    # Submit while extraction is in flight → blocked, message shown, and
    # crucially NOT auto-resumed (never auto-submit model-filled scores).
    page.click('button[type="submit"]')
    page.locator(".photo-status", has_text="Still reading scores").wait_for()
    assert "/cups/new" in page.url
    assert page.input_value("#photo-data") != ""  # photo survived

    # Extraction completes → scores fill in; still no navigation.
    held[0].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "scores": {player_ids[0]: 100, player_ids[1]: 80},
                "ambiguous": [],
                "unmatched_players": [],
            }
        ),
    )
    page.locator(".photo-status", has_text="Filled 2 scores").wait_for()
    assert "/cups/new" in page.url
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    assert score_inputs.nth(0).input_value() == "100"
    assert score_inputs.nth(1).input_value() == "80"

    # The user reviews and submits — cup saves WITH the photo.
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/cups")
    assert page.locator(".flash-success", has_text="photo saved").is_visible()
    photo_href = page.locator('a[href$="/photo"]').first.get_attribute("href")
    assert page.request.get(f"{base_url}{photo_href}").status == 200


def test_submit_with_empty_scores_blocked_client_side(page, base_url):
    """An all-empty submit is stopped before the server round trip.

    The server would reject it with a flash + redirect, which wipes the
    attached photo — the client-side pre-check keeps the form (and photo)
    alive, and entering a score afterwards submits normally.
    """
    _create_player(page, base_url, "Alice")

    page.goto(f"{base_url}/cups/new")
    _attach_photo(page)  # extraction unconfigured → attach-only mode

    page.click('button[type="submit"]')
    page.locator(".photo-status", has_text="Enter scores first").wait_for()
    assert "/cups/new" in page.url  # no navigation
    assert page.input_value("#photo-data") != ""  # photo survived

    # Enter a score → the same form now submits, photo intact.
    page.locator('.score-row input[name="scores[]"]').first.fill("60")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/cups")
    assert page.locator(".flash-success", has_text="photo saved").is_visible()


@requires_in_process_server
def test_extraction_failure_clears_hold_manual_submit_works(page, base_url, monkeypatch):
    """A failed extraction (502) must clear the in-flight hold: the user can
    enter scores manually and submit, and the photo still persists."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")

    _create_player(page, base_url, "Alice")

    page.route(
        "**/extract-scores",
        lambda route: route.fulfill(
            status=502,
            content_type="application/json",
            body=json.dumps({"error": "Could not read the scores"}),
        ),
    )

    page.goto(f"{base_url}/cups/new")
    _attach_photo(page)
    page.locator(".photo-status", has_text="photo is still attached").wait_for()

    page.locator('.score-row input[name="scores[]"]').first.fill("60")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/cups")
    assert page.locator(".flash-success", has_text="photo saved").is_visible()
