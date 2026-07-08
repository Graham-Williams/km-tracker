import json


def _create_player(page, base_url, name, has_line=False):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', name)
    if has_line:
        page.check('input[name="has_line"]')
    page.click('button[type="submit"]')


def _setup_players(page, base_url):
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")


def _start_session(page, base_url):
    """Start a cup session with default players. Returns cup session page."""
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cup-session/new")
    page.click('button[type="submit"]')
    page.wait_for_url("**/cup-session/*")


def _spin_and_confirm(page, base_url):
    """Spin the wheel and confirm the race via Next Race button."""
    page.click("#spin-btn")
    # Wait for spin to complete (wheel label becomes visible)
    page.locator("#wheel-label.visible").wait_for(timeout=10000)
    # Click Next Race or Complete Cup
    btn = page.locator("#next-race-btn, #complete-btn").first
    btn.click()
    # Confirm in modal
    page.locator("#next-race-confirm-btn").click()


# --- Index page ---


def test_index_shows_begin_new_cup(page, base_url):
    page.goto(base_url)
    assert page.locator("text=Begin New Cup").is_visible()
    assert not page.locator("text=Continue Cup").is_visible()


def test_index_shows_continue_cup_when_in_progress(page, base_url):
    _start_session(page, base_url)
    page.goto(base_url)
    assert page.locator("text=Continue Cup").is_visible()
    assert not page.locator("text=Begin New Cup").is_visible()


# --- Session setup ---


def test_session_new_page_loads(page, base_url):
    page.goto(f"{base_url}/cup-session/new")
    assert page.locator("text=Start Cup").is_visible()


def test_session_new_shows_default_players(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cup-session/new")
    assert page.locator("text=Alice").is_visible()
    assert page.locator("text=Bob").is_visible()


def test_start_session_redirects_to_race_page(page, base_url):
    _start_session(page, base_url)
    assert "cup-session" in page.url
    assert page.locator("text=Race 1 of 4").is_visible()


def test_session_new_redirects_if_in_progress(page, base_url):
    _start_session(page, base_url)
    page.goto(f"{base_url}/cup-session/new")
    # Should redirect to existing session
    assert "cup-session" in page.url
    assert page.locator("text=Race 1 of 4").is_visible()


# --- Race page ---


def test_race_page_shows_dashboard(page, base_url):
    _start_session(page, base_url)
    assert page.locator("text=Votoes remaining").is_visible()
    assert page.locator("text=Half Vetoes").is_visible()


def test_spin_button_works(page, base_url):
    _start_session(page, base_url)
    page.click("#spin-btn")
    # Wait for wheel label to appear
    label = page.locator("#wheel-label.visible")
    label.wait_for(timeout=10000)
    assert label.text_content() != ""


def test_spin_then_next_race(page, base_url):
    _start_session(page, base_url)
    _spin_and_confirm(page, base_url)
    # After confirming, the JS reloads the page to race 2. Wait explicitly for
    # the race-2 heading instead of a timing-sensitive networkidle wait (which
    # flakes when the reload's network settles before/after the poll).
    page.locator("text=Race 2 of 4").wait_for(timeout=10000)
    assert "Race 2 of 4" in page.content()


def test_full_session_flow(page, base_url):
    """Full flow: setup -> 4 races -> complete -> submit scores."""
    _start_session(page, base_url)

    for i in range(4):
        page.click("#spin-btn")
        page.locator("#wheel-label.visible").wait_for(timeout=10000)
        btn = page.locator("#next-race-btn, #complete-btn").first
        btn.click()
        page.locator("#next-race-confirm-btn").click()
        if i < 3:
            # Races 1-3: page reloads
            page.wait_for_load_state("networkidle")
        else:
            # Race 4: redirects to complete page
            page.wait_for_url("**/complete", timeout=10000)

    # Should be on completion page
    assert "complete" in page.url
    assert page.locator("text=Complete Cup").is_visible()

    # Fill in scores
    score_inputs = page.locator('input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')

    # Should redirect to cups list with the new cup
    page.wait_for_url("**/cups")
    assert page.locator("text=Alice").is_visible()


# --- Cancel ---


def test_cancel_cup_session(page, base_url):
    _start_session(page, base_url)
    # Click cancel link
    page.locator(".cancel-link").click()
    # Confirm in modal
    page.locator("#cancel-modal .danger").click()
    # Should redirect to index
    page.wait_for_url(f"{base_url}/")
    assert page.locator("text=Begin New Cup").is_visible()


# --- Voto ---


def test_voto_flow(page, base_url):
    _start_session(page, base_url)
    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=10000)

    # Click Voto
    page.click("#voto-btn")
    assert page.locator("#voto-modal.active").is_visible()

    # Confirm
    page.click("#voto-confirm-btn")
    # Should reset for respin
    page.wait_for_timeout(500)
    remaining = page.locator("#voto-remaining").text_content()
    assert remaining == "3"


# --- Half Veto ---


def test_half_veto_flow(page, base_url):
    _start_session(page, base_url)
    page.click("#spin-btn")
    page.locator("#wheel-label.visible").wait_for(timeout=10000)

    # Click Half Veto
    page.click("#half-veto-btn")
    assert page.locator("#half-veto-modal.active").is_visible()

    # Pick first player
    page.locator("#player-picker li").first.click()

    # Coin flip modal should appear with animation
    page.locator("#coin-modal.active").wait_for(timeout=2000)
    # Wait for OK button to appear after coin flip animation
    page.locator("#coin-ok-btn").wait_for(state="visible", timeout=3000)
    # Result message should be shown
    msg = page.locator("#coin-message").text_content()
    assert msg in ["So skipped", "Veto failed. Course stands."]
    page.click("#coin-ok-btn")
