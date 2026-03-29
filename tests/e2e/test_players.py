def test_players_page_empty_state(page, base_url):
    page.goto(f"{base_url}/players")
    assert page.locator("text=No players yet").is_visible()


def test_create_player(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    assert page.locator("text=Alice").is_visible()
    assert not page.locator("text=No players yet").is_visible()


def test_create_player_with_line(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.check('input[name="has_line"]')
    page.click('button[type="submit"]')
    # Player created with has_line — line shows as (+0)
    assert page.locator("text=(+0)").is_visible()


def test_create_player_without_default_cup(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.uncheck('input[name="default_cup"]')
    page.click('button[type="submit"]')
    assert page.locator("text=Alice").is_visible()
    assert not page.locator("text=(default)").is_visible()


def test_create_duplicate_player_shows_error(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    assert page.locator("text=already exists").is_visible()


def test_edit_player_name(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    page.click("text=Edit")
    page.fill('input[name="name"]', "Alicia")
    page.click('button[type="submit"]')
    assert page.locator("text=Alicia").is_visible()


def test_edit_player_toggle_has_line(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    page.click("text=Edit")
    # Enable line
    page.check('input[name="has_line"]')
    page.fill('input[name="line"]', "7")
    page.click('button[type="submit"]')
    assert page.locator("text=(+7)").is_visible()
    # Now disable line
    page.click("text=Edit")
    page.uncheck('input[name="has_line"]')
    page.click('button[type="submit"]')
    assert not page.locator("text=(+7)").is_visible()


def test_delete_player(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    assert page.locator("text=Alice").is_visible()
    page.click("button.delete")
    assert page.locator("text=No players yet").is_visible()


def test_delete_player_after_cup_deleted(page, base_url):
    """Player can be deleted once all their cups are soft-deleted."""
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    # Create a cup with a score
    page.goto(f"{base_url}/cups/new")
    page.locator('.score-row:not(.removed) input[name="scores[]"]').first.fill("100")
    page.click('button[type="submit"]')
    # Delete the cup
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("button.delete")
    # Now delete the player
    page.goto(f"{base_url}/players")
    page.click("button.delete")
    assert page.locator("text=No players yet").is_visible()


def test_create_multiple_players_sorted(page, base_url):
    page.goto(f"{base_url}/players")
    page.fill('input[name="name"]', "Charlie")
    page.click('button[type="submit"]')
    page.fill('input[name="name"]', "Alice")
    page.click('button[type="submit"]')
    page.fill('input[name="name"]', "Bob")
    page.click('button[type="submit"]')
    # Players should be sorted alphabetically
    names = page.locator(".player-name").all_text_contents()
    assert names == ["Alice", "Bob", "Charlie"]
