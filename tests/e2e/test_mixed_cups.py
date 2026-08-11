"""End-to-end: a full 4-race MIXED cup (2 races on one console, then 2 on the
other) through a real browser.

Covers the pieces that only exist client-side: the console coin-flip modal, the
wheel swapping to the other console's track list at race 3, the one-shot swap
photo reminder, and the two-photo score capture — one photo per console, each
filling ONLY its own half, with the cup total calculated (and readonly) from
both. That last part is the one that matters: a console's screen shows that
console's points, so those numbers may never reach the cup-total field.
"""

import base64
import json
import os

import pytest
from playwright.sync_api import expect

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


def _start_mixed_session(page, base_url):
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    page.goto(f"{base_url}/cup-session/new")
    page.select_option('select[name="game_edition"]', "mixed")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")


def _dismiss_console_flip(page):
    """The flip modal auto-opens before race 1 and blocks the controls."""
    page.locator("#console-flip-modal.active").wait_for(timeout=5000)
    page.locator("#console-flip-ok-btn").wait_for(state="visible", timeout=5000)
    page.click("#console-flip-ok-btn")
    page.locator("#console-flip-modal.active").wait_for(state="hidden", timeout=5000)


def _slice_count(page):
    return page.evaluate("NUM_SLICES")


def _play_one_race(page, next_race=None):
    """Spin, confirm, and wait for the RELOADED page.

    The confirm handler calls window.location.reload() after its fetch resolves,
    so wait_for_load_state("networkidle") can settle on the OLD document and
    hand back a stale DOM (same trap noted in test_cup_session.py). Anchor on
    something only the new page has instead: the next race heading, or the
    completion URL."""
    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=15000)
    page.locator("#next-race-btn, #complete-btn").first.click()
    page.locator("#next-race-confirm-btn").click()
    if next_race is None:
        page.wait_for_url("**/complete", timeout=15000)
    else:
        page.locator(f"text=Race {next_race} of 4").wait_for(timeout=15000)


def test_mixed_cup_full_flow(page, base_url):
    """Console flip → 2 races on console A → swap reminder → 2 on console B."""
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda err: errors.append(str(err)))

    _start_mixed_session(page, base_url)

    # The order label names both consoles, in flip order.
    subtitle = page.locator(".page-subtitle").first.text_content()
    assert "→" in subtitle

    # --- Console coin flip: shown once, before race 1 ---
    _dismiss_console_flip(page)
    first_console_slices = _slice_count(page)
    assert first_console_slices in (32, 96)

    # No swap reminder yet.
    assert page.locator("#swap-reminder-modal").count() == 0

    # --- First half (2 races on the flipped-to console) ---
    _play_one_race(page, next_race=2)
    # The flip modal must NOT replay once a race is on the board.
    assert page.locator("#console-flip-modal").count() == 0
    assert _slice_count(page) == first_console_slices

    _play_one_race(page, next_race=3)

    # --- The swap: reminder modal + a different track list ---
    page.locator("#swap-reminder-modal.active").wait_for(timeout=5000)
    assert "before" in page.locator("#swap-reminder-modal p").text_content().lower()
    page.click("#swap-reminder-ok-btn")
    page.locator("#swap-reminder-modal.active").wait_for(state="hidden", timeout=5000)

    second_console_slices = _slice_count(page)
    assert second_console_slices != first_console_slices
    assert {first_console_slices, second_console_slices} == {32, 96}

    # The persistent banner keeps the "second console starts from zero" message
    # around after the modal is dismissed.
    assert page.locator("#second-console-banner").is_visible()

    # --- Second half ---
    _play_one_race(page, next_race=4)
    # Reminder is a one-shot: it must not reappear on race 4.
    assert page.locator("#swap-reminder-modal").count() == 0
    assert _slice_count(page) == second_console_slices

    _play_one_race(page)

    # --- Completion page: per-race console labels, per-block score entry ---
    assert "complete" in page.url
    # wait_for_url can resolve before the new document has finished parsing, so
    # anchor on a locator (auto-retrying) before reading page.content().
    expect(page.locator("#cup-form")).to_be_visible(timeout=15000)
    body = page.content()
    assert "Race 1 (" in body and "Race 3 (" in body

    # Each console's half is entered separately; the total is calculated.
    block1 = page.locator('input[name="block1_scores[]"]')
    block2 = page.locator('input[name="block2_scores[]"]')
    totals = page.locator('input[name="scores[]"]')
    block1.nth(0).fill("55")
    block2.nth(0).fill("45")
    block1.nth(1).fill("40")
    block2.nth(1).fill("40")
    expect(totals.nth(0)).to_have_value("100")
    expect(totals.nth(1)).to_have_value("80")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cups")

    # History shows the console-order badge.
    expect(page.locator(".badge-info").first).to_contain_text("→", timeout=15000)

    assert errors == [], f"JS errors during a mixed cup: {errors}"


def test_console_flip_does_not_replay_on_refresh(page, base_url):
    """A refresh must never re-run (or re-roll) the console flip — the outcome
    lives in the DB and sessionStorage only suppresses the animation."""
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)
    before = page.locator(".page-subtitle").first.text_content()

    page.reload()
    # The modal is still RENDERED (no races yet) but must not auto-open. Anchor
    # on it being present before asserting it isn't active, so the check can't
    # land on a half-parsed document.
    expect(page.locator("#console-flip-modal")).to_have_count(1, timeout=15000)
    expect(page.locator("#console-flip-modal.active")).to_have_count(0)
    expect(page.locator(".page-subtitle").first).to_have_text(before)


def test_pure_cup_has_no_mixed_chrome(page, base_url):
    """Regression control: a pure Wii cup renders exactly as before."""
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    page.goto(f"{base_url}/cup-session/new")
    page.select_option('select[name="game_edition"]', "wii")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")

    assert page.locator("#console-flip-modal").count() == 0
    assert page.locator("#swap-reminder-modal").count() == 0
    assert page.locator("#second-console-banner").count() == 0
    assert "→" not in page.locator(".page-subtitle").first.text_content()

    # Play to the race-3 boundary — still no swap chrome on a pure cup.
    _play_one_race(page, next_race=2)
    _play_one_race(page, next_race=3)
    assert page.locator("#swap-reminder-modal").count() == 0
    assert page.locator("#second-console-banner").count() == 0


@requires_in_process_server
def test_mixed_cup_block_photo_fills_only_its_own_half(page, base_url, monkeypatch):
    """A mixed cup gets ONE PHOTO PER CONSOLE, each filling only its own half.

    This is the regression that matters most, restated for the two-photo world:
    a console's screen shows that console's points, so those numbers may only
    ever land in that console's field. The cup TOTAL is calculated from both
    halves and is readonly — nothing can write a half total into it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")

    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    page.goto(f"{base_url}/cup-session/new")
    page.select_option('select[name="game_edition"]', "mixed")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")
    cup_id = page.url.rstrip("/").split("/")[-1]

    page.goto(f"{base_url}/cup-session/{cup_id}/complete")

    # Two panels, one per console — and no single whole-cup panel any more.
    assert page.locator("#photo-score").count() == 0
    assert page.locator("#photo-block-1").count() == 1
    assert page.locator("#photo-block-2").count() == 1
    # The form carries no photo payload: each photo POSTs on its own request.
    assert page.locator('input[name="photo_data"]').count() == 0

    player_ids = page.eval_on_selector_all(
        ".score-row", "rows => rows.map(r => r.dataset.playerId)"
    )
    page.route(
        "**/cups/*/photo/*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "block": 2, "edition": "mk8dx", "label": "Switch"}),
        ),
    )
    page.route(
        "**/extract-scores",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scores": {player_ids[0]: 42, player_ids[1]: 38},
                    "ambiguous": [],
                    "unmatched_players": [],
                    "partial_half": False,
                    "block": 2,
                    "raw_rows": [
                        {"position": 1, "character": "Yoshi", "points": 42, "is_highlighted": True},
                        {"position": 2, "character": "Mario", "points": 38, "is_highlighted": True},
                    ],
                }
            ),
        ),
    )

    page.set_input_files(
        "#photo-pick-2",
        {"name": "standings.png", "mimeType": "image/png", "buffer": TINY_PNG},
    )
    page.locator("#photo-block-2 .photo-attach-status.is-success").wait_for(state="visible")
    page.locator("#photo-block-2 .photo-mapping").wait_for(state="visible")

    # The SECOND block's inputs are filled; the first block's are untouched.
    block2 = page.locator('input[name="block2_scores[]"]')
    block1 = page.locator('input[name="block1_scores[]"]')
    expect(block2.nth(0)).to_have_value("42")
    expect(block2.nth(1)).to_have_value("38")
    assert block1.nth(0).input_value() == ""
    assert block1.nth(1).input_value() == ""

    # The total stays BLANK while a half is missing — a half total must never
    # be able to masquerade as a cup total.
    totals = page.locator('input[name="scores[]"]')
    assert totals.nth(0).input_value() == ""
    assert totals.nth(0).get_attribute("readonly") is not None

    # Typing the other half completes the arithmetic and re-runs placements.
    block1.nth(0).fill("46")
    block1.nth(1).fill("30")
    expect(totals.nth(0)).to_have_value("88")
    expect(totals.nth(1)).to_have_value("68")
    expect(page.locator(".score-place").first).to_contain_text("st")

    # The mapping panel writes into THIS block only.
    selects = page.locator("#photo-block-2 .photo-map-select")
    assert selects.count() == 2
    selects.nth(0).select_option(label="— leave blank —")
    expect(block2.nth(0)).to_have_value("")
    expect(block1.nth(0)).to_have_value("46")   # the other half is untouched
    expect(totals.nth(0)).to_have_value("")     # and the total goes blank again


@requires_in_process_server
def test_swap_photo_uploads_from_the_race_page(page, base_url, monkeypatch):
    """The first console's results screen is captured DURING the cup — at the
    last race of the first block, before "Next Race" — and the swap reminder
    carries the same control while staying skippable."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)

    # Race 1 page: nothing to photograph yet.
    assert page.locator("#photo-block-1").count() == 0

    _play_one_race(page, next_race=2)

    # Race 2 = last race of the first block: the capture control appears.
    panel = page.locator("#photo-block-1")
    expect(panel).to_have_count(1)
    page.set_input_files(
        "#photo-pick-1",
        {"name": "standings.png", "mimeType": "image/png", "buffer": TINY_PNG},
    )
    page.locator("#photo-block-1 .photo-attach-status.is-success").wait_for(state="visible")
    assert "saved" in page.locator("#photo-block-1 .photo-attach-status").text_content()

    # It really is persisted, tagged to block 1.
    cup_id = page.url.rstrip("/").split("/")[-1]
    assert page.request.get(f"{base_url}/cups/{cup_id}/photo/1").status == 200
    assert page.request.get(f"{base_url}/cups/{cup_id}/photo/2").status == 404

    # Reloading shows the saved state instead of an empty control.
    page.reload()
    expect(page.locator("#photo-block-1 .photo-attach-status")).to_be_visible()
    assert "Replace photo" in page.locator(
        '#photo-block-1 [data-photo-input="photo-pick-1"]'
    ).text_content()

    # The swap reminder (race 3) offers the same control and is skippable.
    _play_one_race(page, next_race=3)
    page.locator("#swap-reminder-modal.active").wait_for(timeout=5000)
    assert page.locator(
        '#swap-reminder-modal [data-photo-input="photo-take-1"]'
    ).count() == 1
    page.click("#swap-reminder-ok-btn")
    page.locator("#swap-reminder-modal.active").wait_for(state="hidden", timeout=5000)
    # Skipping changes nothing about the cup — the race controls are usable.
    expect(page.locator("#spin-btn")).to_be_enabled()


@requires_in_process_server
def test_read_scores_from_the_saved_swap_photo(page, base_url, monkeypatch):
    """A photo taken at the swap is read for scores later, on the completion
    page, with no image round-tripping through the browser."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)
    _play_one_race(page, next_race=2)

    page.set_input_files(
        "#photo-pick-1",
        {"name": "standings.png", "mimeType": "image/png", "buffer": TINY_PNG},
    )
    page.locator("#photo-block-1 .photo-attach-status.is-success").wait_for(state="visible")

    cup_id = page.url.rstrip("/").split("/")[-1]
    page.goto(f"{base_url}/cup-session/{cup_id}/complete")

    # The saved photo is previewed and offers a "read scores" action.
    expect(page.locator("#photo-block-1 .photo-preview")).to_be_visible()
    read_btn = page.locator("#photo-block-1 .photo-read-btn")
    expect(read_btn).to_be_visible()

    player_ids = page.eval_on_selector_all(
        ".score-row", "rows => rows.map(r => r.dataset.playerId)"
    )
    bodies = []

    def _capture(route):
        bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scores": {player_ids[0]: 46},
                    "ambiguous": [],
                    "unmatched_players": ["Bob"],
                    "partial_half": False,
                    "block": 1,
                    "raw_rows": [
                        {"position": 1, "character": "Funky Kong", "points": 46, "is_highlighted": True},
                    ],
                }
            ),
        )

    page.route("**/extract-scores", _capture)
    read_btn.click()
    expect(page.locator('input[name="block1_scores[]"]').nth(0)).to_have_value("46")

    # No image in the request — the server re-reads the photo it already holds.
    assert bodies and "image" not in bodies[0]
    assert bodies[0]["block"] == 1



@requires_in_process_server
def test_pure_cup_photo_still_autofills(page, base_url, monkeypatch):
    """Regression control: suppressing auto-fill is mixed-only."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")

    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    page.goto(f"{base_url}/cup-session/new")
    page.select_option('select[name="game_edition"]', "wii")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")
    cup_id = page.url.rstrip("/").split("/")[-1]

    page.goto(f"{base_url}/cup-session/{cup_id}/complete")
    assert page.locator("#photo-score .photo-half-warning").count() == 0

    player_ids = page.eval_on_selector_all(
        ".score-row", "rows => rows.map(r => r.dataset.playerId)"
    )
    page.route(
        "**/extract-scores",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scores": {player_ids[0]: 60},
                    "ambiguous": [],
                    "unmatched_players": ["Bob"],
                    "partial_half": False,
                    "raw_rows": [
                        {"position": 1, "character": "Funky Kong", "points": 60, "is_highlighted": True},
                    ],
                }
            ),
        ),
    )

    page.set_input_files(
        "#photo-pick",
        {"name": "standings.png", "mimeType": "image/png", "buffer": TINY_PNG},
    )
    page.locator(".photo-attach-status.is-success").wait_for(state="visible")
    page.locator(".photo-mapping").wait_for(state="visible")

    assert page.locator(".score-input").nth(0).input_value() == "60"
    assert "Filled 1 score" in page.locator(".photo-status").text_content()

    # A pure cup keeps the full interactive panel — dropdowns, original title,
    # and a live write path into the score inputs (the read-only reference list
    # is mixed-only). Alice's dropdown is pre-selected to the row that filled
    # her; clearing it clears her score, which the reference list can't do.
    selects = page.locator(".photo-map-select")
    assert selects.count() == 2
    assert page.locator(".photo-map-readonly").count() == 0
    assert "Map each player" in page.locator(".photo-mapping-title").text_content()
    selects.nth(0).select_option(label="— leave blank —")
    assert page.locator(".score-input").nth(0).input_value() == ""


def test_swap_reminder_does_not_reopen_on_refresh(page, base_url):
    """The reminder is gated server-side purely on "2 races played", which is
    true for EVERY load of the race-3 page — so a refresh must not re-block the
    controls. The persistent banner still carries the message."""
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)
    _play_one_race(page, next_race=2)
    _play_one_race(page, next_race=3)

    page.locator("#swap-reminder-modal.active").wait_for(timeout=5000)
    page.click("#swap-reminder-ok-btn")
    page.locator("#swap-reminder-modal.active").wait_for(state="hidden", timeout=5000)

    page.reload()
    expect(page.locator("#second-console-banner")).to_be_visible(timeout=15000)
    expect(page.locator("#swap-reminder-modal.active")).to_have_count(0)
    # Controls are usable, not covered by a re-opened modal.
    expect(page.locator("#spin-btn")).to_be_enabled()


def test_stale_manual_override_recovers_instead_of_dead_ending(page, base_url):
    """Two devices on one cup: this page still shows the FIRST console's wheel
    after another device recorded race 2. A manual override picks an off-half
    course, the server correctly 400s — and the page must reload onto the right
    console rather than sit there failing forever."""
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)
    _play_one_race(page, next_race=2)

    stale_courses = page.evaluate("COURSES")
    stale_edition = page.evaluate("RACE_EDITION")
    cup_id = page.url.rstrip("/").split("/")[-1]

    # Simulate the other device finishing race 2 (this page never learns of it).
    page.request.post(
        f"{base_url}/cup-session/{cup_id}/next-race",
        data=json.dumps({"map": stale_courses[1]}),
        headers={"Content-Type": "application/json"},
    )

    page.on("dialog", lambda d: d.accept())
    # Manual override off this page's stale (first-console) list.
    page.select_option("#manual-override", "0")
    page.locator("#wheel-label.visible").wait_for(timeout=15000)
    page.locator("#next-race-btn, #complete-btn").first.click()
    page.locator("#next-race-confirm-btn").click()

    # The page recovers onto race 3 with the OTHER console's wheel. "Race 3 of
    # 4" only exists on the post-reload document (this page said "Race 2"), so
    # it's a real navigation anchor.
    expect(page.locator("text=Race 3 of 4")).to_be_visible(timeout=15000)
    assert page.evaluate("RACE_EDITION") != stale_edition


def test_next_race_non_json_response_recovers(page, base_url):
    """A 500 HTML page (or a dropped connection) rejects r.json(). Without a
    .catch() the page silently does nothing — the same dead-end class as a
    stale manual override, reached by a different error path."""
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)

    # One non-JSON response, then let the route fall through on the reload.
    served = []

    def handle(route):
        if served:
            route.continue_()
            return
        served.append(True)
        route.fulfill(
            status=500,
            content_type="text/html",
            body="<html><body>Internal Server Error</body></html>",
        )

    page.route("**/next-race", handle)

    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=15000)
    page.locator("#next-race-btn, #complete-btn").first.click()
    page.locator("#next-race-confirm-btn").click()

    # The page reloads and re-syncs rather than hanging on a dead confirm modal.
    #
    # These MUST be auto-retrying expect() assertions, not bare asserts. The
    # reload is triggered by the page's own .catch(), so there is nothing to
    # wait_for beforehand — and #spin-btn exists (disabled, because the spin
    # click disabled it) on the PRE-reload document too, so waiting for it to
    # be "visible" returns instantly against the old page and the assertions
    # race the navigation. to_be_enabled() retries until the fresh page lands,
    # which is also the exact state the fix produces.
    expect(page.locator("#spin-btn")).to_be_enabled(timeout=15000)
    expect(page.locator("#next-race-modal.active")).to_have_count(0)
