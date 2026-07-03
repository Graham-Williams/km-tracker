"""E2E coverage for the race-page sound effects (static/js/sfx.js).

Web Audio output can't be asserted acoustically in headless tests, so these
cover what can be: the SFX module loads on the race page, the mute toggle
flips + persists state, and the full spin -> half-veto coin-flip flow (which
exercises every sound path: tick, whoosh, success/fail) runs without any JS
console or page errors.
"""


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


# --- SFX module presence ---


def test_race_page_loads_sfx_module(page, base_url):
    _start_session(page, base_url)
    assert page.evaluate("typeof window.SFX") == "object"
    for fn in ["tick", "whoosh", "success", "fail", "toggleMute"]:
        assert page.evaluate(f"typeof window.SFX.{fn}") == "function"
    assert page.evaluate("window.SFX.muted") is False


# --- Mute toggle ---


def test_mute_toggle_flips_state_and_persists(page, base_url):
    _start_session(page, base_url)
    mute_btn = page.locator("#mute-btn")
    assert mute_btn.get_attribute("aria-pressed") == "false"
    assert mute_btn.text_content() == "🔊"

    mute_btn.click()
    assert mute_btn.get_attribute("aria-pressed") == "true"
    assert mute_btn.text_content() == "🔇"
    assert page.evaluate("window.SFX.muted") is True
    assert page.evaluate("localStorage.getItem('km-muted')") == "1"

    # Persists across reload
    page.reload()
    mute_btn = page.locator("#mute-btn")
    assert mute_btn.get_attribute("aria-pressed") == "true"
    assert mute_btn.text_content() == "🔇"
    assert page.evaluate("window.SFX.muted") is True

    # Toggle back off
    mute_btn.click()
    assert mute_btn.get_attribute("aria-pressed") == "false"
    assert page.evaluate("localStorage.getItem('km-muted')") == "0"


# --- No JS errors across every sound path ---


def test_spin_and_coin_flip_produce_no_js_errors(page, base_url):
    errors = []

    def on_console(msg):
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", on_console)
    page.on("pageerror", lambda err: errors.append(str(err)))

    _start_session(page, base_url)

    # Spin (tick path) and wait for the wheel to stop
    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=10000)

    # Half veto -> coin flip (whoosh + success/fail path)
    page.click("#half-veto-btn")
    page.locator("#player-picker li").first.click()
    page.locator("#coin-modal.active").wait_for(timeout=2000)
    page.locator("#coin-ok-btn").wait_for(state="visible", timeout=3000)
    page.click("#coin-ok-btn")

    assert errors == []
