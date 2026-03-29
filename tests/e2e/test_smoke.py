def test_index_loads(page, base_url):
    page.goto(base_url)
    assert "KM Tracker" in page.title()
