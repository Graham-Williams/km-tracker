from db import get_connection


def _setup_cup_with_player(page, base_url):
    """Create players and a cup via the UI so standalone scores can reference them."""
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    page.fill('input[name="name"]', "Bob")
    page.click('button[type="submit"]')
    page.goto(f"{base_url}/cups/new")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(0).fill("100")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').nth(1).fill("80")
    page.click('button[type="submit"]')


def test_scores_page_empty_state(page, base_url):
    page.goto(f"{base_url}/scores")
    assert page.locator("text=No scores yet").is_visible()


def test_scores_page_shows_scores(page, base_url):
    _setup_cup_with_player(page, base_url)
    page.goto(f"{base_url}/scores")
    assert page.locator("text=Alice").is_visible()
    assert page.locator("text=Bob").is_visible()


def test_create_standalone_score(page, base_url):
    """Create a third player, then add their score to an existing cup via standalone form."""
    _setup_cup_with_player(page, base_url)
    # Create a third player
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Carol")
    page.click('button[type="submit"]')
    # Get cup_id and Carol's player_id from the DB
    conn = get_connection()
    cup = conn.execute("SELECT id FROM cups LIMIT 1").fetchone()
    carol = conn.execute("SELECT id FROM players WHERE name = 'Carol'").fetchone()
    conn.close()
    # Create standalone score
    page.goto(f"{base_url}/scores")
    page.fill('input[name="cup_id"]', str(cup["id"]))
    page.fill('input[name="player_id"]', str(carol["id"]))
    page.fill('input[name="score"]', "99")
    page.click('button[type="submit"]')
    assert page.locator(".score-item", has_text="Carol").is_visible()


def test_edit_standalone_score(page, base_url):
    _setup_cup_with_player(page, base_url)
    page.goto(f"{base_url}/scores")
    # Edit Alice's score
    page.locator(".score-item", has_text="Alice").locator("text=Edit").click()
    assert page.locator("text=Edit Score").is_visible()
    page.fill('input[name="score"]', "200")
    page.click('button[type="submit"]')
    # Should redirect back to scores page with updated value
    assert page.locator("text=200").is_visible()


def test_delete_standalone_score(page, base_url):
    _setup_cup_with_player(page, base_url)
    page.goto(f"{base_url}/scores")
    count_before = page.locator(".score-item").count()
    page.locator(".score-item", has_text="Bob").locator("button.delete").click()
    count_after = page.locator(".score-item").count()
    assert count_after == count_before - 1
