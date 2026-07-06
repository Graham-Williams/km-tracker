"""E2E coverage for the skip-animations ("instant mode") toggle on the race
page (templates/cup_session_race.html).

With skip-anim ON, the wheel result and the half-veto coin-flip result render
immediately (no 4-5.5s spin, no ~1.1s coin swap); OFF keeps today's animated
behavior. State is persisted in localStorage under "km-skip-anim" (same
pattern as the mute button's "km-muted").

Timing margins are deliberately generous: the animated spin takes >= 4s and
the animated coin flip shows its OK button at ~1.3s, so asserting the instant
path completes within a couple of seconds (and the animated path has NOT
completed after 2s) can't flake on ordinary test-machine slowness.
"""

import time


def _create_player(page, base_url, name):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', name)
    page.click('button[type="submit"]')


def _start_session(page, base_url):
    """Create players and start a cup session, landing on the race page."""
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    page.goto(f"{base_url}/cup-session/new")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")


def _enable_skip_anim(page):
    page.click("#skip-anim-btn")
    assert page.locator("#skip-anim-btn").get_attribute("aria-pressed") == "true"


# --- Toggle state + persistence (mirrors the mute-button test) ---


def test_skip_anim_toggle_flips_state_and_persists(page, base_url):
    _start_session(page, base_url)
    skip_btn = page.locator("#skip-anim-btn")
    assert skip_btn.get_attribute("aria-pressed") == "false"
    assert skip_btn.text_content() == "🎬"

    skip_btn.click()
    assert skip_btn.get_attribute("aria-pressed") == "true"
    assert skip_btn.text_content() == "⚡"
    assert page.evaluate("localStorage.getItem('km-skip-anim')") == "1"

    # Persists across reload
    page.reload()
    skip_btn = page.locator("#skip-anim-btn")
    assert skip_btn.get_attribute("aria-pressed") == "true"
    assert skip_btn.text_content() == "⚡"

    # Toggle back off
    skip_btn.click()
    assert skip_btn.get_attribute("aria-pressed") == "false"
    assert skip_btn.text_content() == "🎬"
    assert page.evaluate("localStorage.getItem('km-skip-anim')") == "0"


# --- Instant wheel spin ---


def test_spin_with_skip_anim_shows_result_instantly(page, base_url):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: errors.append(str(err)))

    _start_session(page, base_url)
    _enable_skip_anim(page)

    # The animated spin takes >= 4s; the instant path must show the result
    # label well before that (2s covers the local fetch with room to spare).
    page.click("#spin-btn")
    label = page.locator("#wheel-label.visible")
    label.wait_for(timeout=2000)
    assert label.text_content() != ""

    # Controls unlock immediately too, and the instant landing carries the
    # result through to the next race exactly like an animated spin.
    assert page.locator("#half-veto-btn").is_enabled()
    page.click("#next-race-btn")
    page.locator("#next-race-modal.active").wait_for(timeout=2000)
    page.click("#next-race-confirm-btn")
    page.locator(".page-subtitle", has_text="Race 2").wait_for(timeout=10000)

    assert errors == []


def test_manual_override_with_skip_anim_is_instant(page, base_url):
    _start_session(page, base_url)
    _enable_skip_anim(page)

    page.select_option("#manual-override", "0")
    label = page.locator("#wheel-label.visible")
    label.wait_for(timeout=2000)
    assert "(1)" in label.text_content()


# --- Instant half-veto coin flip ---


def test_half_veto_with_skip_anim_resolves_instantly(page, base_url):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: errors.append(str(err)))

    _start_session(page, base_url)
    _enable_skip_anim(page)

    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=2000)

    page.click("#half-veto-btn")
    page.locator("#player-picker li").first.click()
    # In instant mode the result is rendered synchronously with the modal
    # opening — by the time the modal is active, the coin face, message, and
    # OK button are already there (the animated path takes ~1.3s to get here).
    page.locator("#coin-modal.active").wait_for(timeout=2000)
    assert page.locator("#coin-ok-btn").is_visible()
    coin_class = page.locator("#coin").get_attribute("class")
    assert "flipping" not in coin_class
    assert "success" in coin_class or "fail" in coin_class
    msg = page.locator("#coin-message").text_content()
    assert msg in ["So skipped", "Veto failed. Course stands."]
    page.click("#coin-ok-btn")

    assert errors == []


# --- OFF keeps today's animated behavior ---


def test_spin_without_skip_anim_still_animates(page, base_url):
    _start_session(page, base_url)
    assert page.locator("#skip-anim-btn").get_attribute("aria-pressed") == "false"

    page.click("#spin-btn")
    # The animated spin runs >= 4s; 2s in, the result must NOT be up yet.
    page.wait_for_timeout(2000)
    assert not page.locator("#wheel-label.visible").is_visible()
    # ...and it still lands eventually.
    page.locator("#wheel-label.visible").wait_for(timeout=10000)


def test_half_veto_without_skip_anim_still_animates(page, base_url):
    _start_session(page, base_url)

    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=10000)

    page.click("#half-veto-btn")
    page.locator("#player-picker li").first.click()
    page.locator("#coin-modal.active").wait_for(timeout=2000)
    # The animated reveal lands at ~1.1s (face) / ~1.3s (OK button) — right
    # after the modal opens, the coin must still be flipping with no result.
    assert not page.locator("#coin-ok-btn").is_visible()
    assert page.locator("#coin").text_content() == "?"
    # ...and the result still arrives after the animation.
    page.locator("#coin-ok-btn").wait_for(state="visible", timeout=3000)
    page.click("#coin-ok-btn")
