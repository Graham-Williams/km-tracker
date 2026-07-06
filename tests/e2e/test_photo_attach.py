"""Photo attach flow on the manual cup form (real browser).

The attach is async (canvas downscale), so this covers the silent-drop guards:
buttons are wired by photo_score.js (they ship disabled), a successful pick
shows the prominent success indicator, and submitting persists the photo
(confirmed by the "photo saved" flash + a 200 from the photo route).
"""

import base64

# 1x1 red PNG — any decodable image works; the client re-encodes to JPEG.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _create_player(page, base_url, name):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', name)
    page.click('button[type="submit"]')


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
