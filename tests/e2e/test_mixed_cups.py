"""End-to-end: a full 4-race MIXED cup (2 races on one console, then 2 on the
other) through a real browser.

Covers the pieces that only exist client-side: the console coin-flip modal, the
wheel swapping to the other console's track list at race 3, and the one-shot
swap photo reminder.
"""


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
