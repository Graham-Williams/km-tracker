"""End-to-end: a full 4-race MIXED cup (2 races on one console, then 2 on the
other) through a real browser.

Covers the pieces that only exist client-side: the console coin-flip modal, the
wheel swapping to the other console's track list at race 3, the one-shot swap
photo reminder, and — most importantly — that a `partial_half` extraction
response never auto-fills the score inputs.
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

    # --- Completion page: per-race console labels, combined-total note ---
    assert "complete" in page.url
    body = page.content()
    assert "Race 1 (" in body and "Race 3 (" in body
    assert "combined total" in body

    score_inputs = page.locator('input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cups")

    # History shows the console-order badge.
    assert "→" in page.locator(".badge-info").first.text_content()

    assert errors == [], f"JS errors during a mixed cup: {errors}"


def test_console_flip_does_not_replay_on_refresh(page, base_url):
    """A refresh must never re-run (or re-roll) the console flip — the outcome
    lives in the DB and sessionStorage only suppresses the animation."""
    _start_mixed_session(page, base_url)
    _dismiss_console_flip(page)
    before = page.locator(".page-subtitle").first.text_content()

    page.reload()
    page.wait_for_load_state("networkidle")

    # Modal is still rendered (no races yet) but must not auto-open again.
    assert page.locator("#console-flip-modal.active").count() == 0
    assert page.locator(".page-subtitle").first.text_content() == before


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
def test_mixed_cup_photo_never_autofills_scores(page, base_url, monkeypatch):
    """The regression that matters most: on a mixed cup the completion photo
    shows only the SECOND console's half, so its numbers are half totals.
    A `partial_half` response must leave every score input EMPTY — filling
    plausible-looking half totals would permanently record ~half the true
    points with no signal."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e2e-fake-key")

    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    page.goto(f"{base_url}/cup-session/new")
    page.select_option('select[name="game_edition"]', "mixed")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")
    cup_id = page.url.rstrip("/").split("/")[-1]

    page.goto(f"{base_url}/cup-session/{cup_id}/complete")

    # The warning must be present in the photo panel BEFORE any photo is taken.
    assert page.locator("#photo-score .photo-half-warning").is_visible()

    # Exactly what the server sends for a mixed cup: rows, but no scores.
    page.route(
        "**/extract-scores",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scores": {},
                    "ambiguous": [],
                    "unmatched_players": [],
                    "partial_half": True,
                    "raw_rows": [
                        {"position": 1, "character": "Yoshi", "points": 42, "is_highlighted": True},
                        {"position": 2, "character": "Mario", "points": 38, "is_highlighted": True},
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

    # NOTHING auto-filled.
    scores = page.locator(".score-input")
    for i in range(scores.count()):
        assert scores.nth(i).input_value() == ""

    # And the status says why, instead of "Filled N scores".
    status = page.locator(".photo-status").text_content()
    assert "Filled" not in status
    assert "combined total" in status

    # The panel is a READ-ONLY reference: it shows what was read off the photo
    # but offers NO control that could write a half total into a score input.
    # (The interactive version's own title tells the user to map players to
    # rows — a titled three-tap path to persisting half totals as cup scores.)
    assert page.locator(".photo-map-select").count() == 0
    readonly = page.locator(".photo-map-readonly")
    assert readonly.count() == 2
    assert "42 pts" in readonly.nth(0).text_content()
    title = page.locator(".photo-mapping-title").text_content()
    assert "Map each player" not in title
    assert "this console only" in title

    # Nothing on the page can fill a score: still empty after the panel renders.
    for i in range(scores.count()):
        assert scores.nth(i).input_value() == ""


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
    page.locator("#second-console-banner").wait_for(timeout=5000)
    assert page.locator("#swap-reminder-modal.active").count() == 0
    # Controls are usable, not covered by a re-opened modal.
    assert page.locator("#spin-btn").is_enabled()


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

    # The page recovers onto race 3 with the OTHER console's wheel.
    page.locator("text=Race 3 of 4").wait_for(timeout=15000)
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
    page.locator("#spin-btn").wait_for(state="visible", timeout=15000)
    assert page.locator("#next-race-modal.active").count() == 0
    assert page.locator("#spin-btn").is_enabled()
