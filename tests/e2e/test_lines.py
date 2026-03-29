def _create_player(page, base_url, name, has_line=True):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', name)
    if has_line:
        page.check('input[name="has_line"]')
    page.click('button[type="submit"]')


def _setup_3_players(page, base_url):
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    _create_player(page, base_url, "Carol")


def _create_cup_with_scores(page, base_url, scores):
    """Create a cup and fill in scores for the existing default players.

    scores: list of score values in player order (alphabetical).
    """
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    for i, score in enumerate(scores):
        score_inputs.nth(i).fill(str(score))
    page.click('button[type="submit"]')


def test_line_adjustment_flash_message(page, base_url):
    _setup_3_players(page, base_url)
    _create_cup_with_scores(page, base_url, [100, 80, 60])
    assert page.locator("text=Lines adjusted").is_visible()


def test_line_adjustment_updates_player_lines(page, base_url):
    _setup_3_players(page, base_url)
    _create_cup_with_scores(page, base_url, [100, 80, 60])
    page.goto(f"{base_url}/players")
    # Alice: 1st → -3, Bob: 2nd → 0, Carol: 3rd → +3
    assert page.locator("text=(-3)").is_visible()
    assert page.locator("text=(+3)").is_visible()


def test_line_changes_shown_on_cup_list(page, base_url):
    _setup_3_players(page, base_url)
    _create_cup_with_scores(page, base_url, [100, 80, 60])
    assert page.locator("text=Lines:").is_visible()


def test_no_line_adjustment_2_player_cup(page, base_url):
    _create_player(page, base_url, "Alice")
    _create_player(page, base_url, "Bob")
    _create_cup_with_scores(page, base_url, [100, 80])
    page.goto(f"{base_url}/players")
    # No line adjustments — lines should still be +0
    alice_item = page.locator(".player-item", has_text="Alice")
    assert alice_item.locator("text=(+0)").is_visible()


def test_cumulative_line_adjustments(page, base_url):
    _setup_3_players(page, base_url)
    _create_cup_with_scores(page, base_url, [100, 80, 60])
    # Create second cup — Alice still winning, Carol still losing
    page.goto(f"{base_url}/cups/new")
    page.fill('input[name="date"]', "2026-03-16T20:00")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    score_inputs.nth(2).fill("60")
    page.click('button[type="submit"]')
    page.goto(f"{base_url}/players")
    # Alice: -3 + -3 = -6, Carol: +3 + +3 = +6
    assert page.locator("text=(-6)").is_visible()
    assert page.locator("text=(+6)").is_visible()


def test_new_cup_live_placement_display(page, base_url):
    """Score inputs should show live placement as you type."""
    _setup_3_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("80")
    score_inputs.nth(2).fill("60")
    # Placements should update in real time
    places = page.locator('.score-row:not(.removed) .score-place').all_text_contents()
    assert "1st" in places
    assert "2nd" in places
    assert "3rd" in places


def test_new_cup_tied_score_shows_t_prefix(page, base_url):
    """Tied scores should show T- prefix in live placements."""
    _setup_3_players(page, base_url)
    page.goto(f"{base_url}/cups/new")
    score_inputs = page.locator('.score-row:not(.removed) input[name="scores[]"]')
    score_inputs.nth(0).fill("100")
    score_inputs.nth(1).fill("100")
    score_inputs.nth(2).fill("60")
    places = page.locator('.score-row:not(.removed) .score-place').all_text_contents()
    tied_places = [p for p in places if "T-" in p]
    assert len(tied_places) == 2


def test_new_cup_line_score_syncs(page, base_url):
    """For players with lines, entering raw score should sync the line score field."""
    _create_player(page, base_url, "Alice", has_line=True)
    # Set Alice's line to +10
    page.click("text=Edit")
    page.fill('input[name="line"]', "10")
    page.click('button[type="submit"]')
    page.goto(f"{base_url}/cups/new")
    # Enter raw score
    page.locator('.score-row:not(.removed) input[name="scores[]"]').first.fill("50")
    # Line score field should show 60 (50 + 10)
    line_score = page.locator('.score-row:not(.removed) .line-score-input').first
    assert line_score.input_value() == "60"


def test_delete_cup_preserves_lines(page, base_url):
    """Deleting a cup should NOT revert the line adjustments."""
    _setup_3_players(page, base_url)
    _create_cup_with_scores(page, base_url, [100, 80, 60])
    # Verify lines changed
    page.goto(f"{base_url}/players")
    assert page.locator("text=(-3)").is_visible()
    # Delete the cup
    page.goto(f"{base_url}/cups")
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("button.delete")
    # Lines should still be adjusted
    page.goto(f"{base_url}/players")
    assert page.locator("text=(-3)").is_visible()
    assert page.locator("text=(+3)").is_visible()
