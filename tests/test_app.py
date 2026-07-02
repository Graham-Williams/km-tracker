import app as app_module


def test_index_returns_200(client):
    response = client.get("/")
    assert response.status_code == 200


def test_index_contains_title(client):
    response = client.get("/")
    assert b"KM Tracker" in response.data


def test_app_env_defaults_to_production():
    # APP_ENV isn't set in the test environment, so the safe default applies.
    assert app_module.APP_ENV == "production"
    assert app_module.IS_STAGING is False


def test_title_plain_in_production(client):
    response = client.get("/")
    assert b"<title>KM Tracker</title>" in response.data
    assert b"[STG]" not in response.data


def test_title_prefixed_in_staging(client, monkeypatch):
    monkeypatch.setattr(app_module, "APP_ENV", "staging")
    monkeypatch.setattr(app_module, "IS_STAGING", True)
    response = client.get("/")
    assert b"<title>[STG] KM Tracker</title>" in response.data


def test_staging_prefix_applies_to_overridden_titles(client, monkeypatch):
    monkeypatch.setattr(app_module, "APP_ENV", "staging")
    monkeypatch.setattr(app_module, "IS_STAGING", True)
    response = client.get("/cups")
    assert "<title>[STG] Cups — KM Tracker</title>" in response.get_data(as_text=True)
