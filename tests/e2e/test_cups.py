def _create_player(page, base_url, name, has_line=False):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', name)
    if has_line:
        page.check('input[name="has_line"]')
    page.click('button[type="submit"]')


def _setup_players(page, base_url):
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")


def test_cups_page_empty_state(page, base_url):
    page.goto(f"{base_url}/cups")
    assert page.locator("text=No cups yet").is_visible()


def test_create_cup_with_scores(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    # Fill in scores for default players
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    # Should redirect to cups list
    assert page.locator("text=Alice").is_visible()
    assert page.locator("text=Bob").is_visible()


def test_create_cup_with_notes(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    page.fill('textarea[name="notes"]', "Game night")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    assert page.locator("text=Game night").is_visible()


def test_create_cup_shows_placements(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    # Cup list should show placements
    assert page.locator("text=1st").is_visible()
    assert page.locator("text=2nd").is_visible()


def test_create_cup_with_tiebreaker(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("100")
    # Tiebreaker checkbox should become enabled for tied scores
    page.locator('.score-row:not(.removed) input[name="tiebreakers[]"]').first.check()
    page.click('button[type="submit"]')
    # Should show the asterisk for tiebreaker winner
    assert page.locator("text=*").is_visible()


def test_edit_cup(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    # Edit the cup
    page.click("text=Edit")
    assert page.locator("text=Edit Cup").is_visible()
    page.fill('textarea[name="notes"]', "Updated notes")
    page.click('button[type="submit"]')
    assert page.locator("text=Updated notes").is_visible()


def test_edit_cup_shows_existing_scores(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    page.click("text=Edit")
    # Score inputs should have existing values
    alice_score = page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(0)
    assert alice_score.input_value() == "100"


def test_delete_cup(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    # Handle the confirm dialog
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("button.delete")
    assert page.locator("text=No cups yet").is_visible()


def test_new_cup_shows_default_players(page, base_url):
    _create_player(page, base_url, "Alice")
    # Create Bob as non-default
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Bob")
    page.uncheck('input[name="default_cup"]')
    page.click('button[type="submit"]')
    page.goto(f"{base_url}/cups/new")
    # Only Alice should appear in the score rows
    score_names = page.locator('.score-row:not(.removed) .score-name').all_text_contents()
    assert "Alice" in score_names
    assert "Bob" not in score_names


def test_new_cup_add_player(page, base_url):
    _create_player(page, base_url, "Alice")
    # Create Bob as non-default
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Bob")
    page.uncheck('input[name="default_cup"]')
    page.click('button[type="submit"]')
    page.goto(f"{base_url}/cups/new")
    # Add Bob via dropdown
    page.select_option("#add-player-select", label="Bob")
    page.click("#add-player-btn")
    score_names = page.locator('.score-row:not(.removed) .score-name').all_text_contents()
    assert "Bob" in score_names


def test_new_cup_remove_player(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    # Remove the first player
    page.locator(".remove-btn").first.click()
    visible_names = page.locator('.score-row:not(.removed) .score-name').all_text_contents()
    assert len(visible_names) == 1


def test_new_cup_shows_recalculation_note_on_edit(page, base_url):
    _setup_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    page.click('button[type="submit"]')
    page.click("text=Edit")
    assert page.locator("text=will not recalculate line adjustments").is_visible()


def test_cups_sorted_descending(page, base_url):
    _setup_players(page, base_url)
    # Create two cups with different dates
    page.goto(f"{base_url}/cups/new")
    page.fill('input[name="date"]', "2026-01-01T12:00")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(0).fill("100")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(1).fill("80")
    page.click('button[type="submit"]')
    page.goto(f"{base_url}/cups/new")
    page.fill('input[name="date"]', "2026-06-01T12:00")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(0).fill("90")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(1).fill("70")
    page.click('button[type="submit"]')
    # The page renders dates which get converted to local time via JS,
    # but the raw datetime attrs should be in order
    times = page.locator("time").all()
    datetimes = [t.get_attribute("datetime") for t in times]
    assert datetimes[0] > datetimes[1]  # Most recent first
