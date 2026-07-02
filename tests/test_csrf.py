"""CSRF Origin/Referer host-pinning (csrf_origin_check + APP_HOST/APP_ORIGIN).

CSRF_PROTECTION defaults to ON, so these run against the real production code
path. The Flask test client's own host is "localhost", which is what
_expected_host() falls back to when APP_HOST/APP_ORIGIN are unset.
"""

import pytest

import app as app_module
from db import get_connection
from helpers import create_player

GOOD_ORIGIN = "http://localhost"
EVIL_ORIGIN = "https://evil.example"


def _player_count():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    return count


def test_csrf_protection_on_by_default():
    assert app_module.CSRF_PROTECTION is True


# --- No Origin/Referer (non-browser clients) ---


def test_post_without_origin_or_referer_allowed(client):
    # curl / test-client style requests carry neither header and must work.
    response = client.post("/players", data={"name": "Alice"}, follow_redirects=True)
    assert response.status_code == 200
    assert _player_count() == 1


# --- Origin header ---


def test_post_with_matching_origin_allowed(client):
    response = client.post(
        "/players", data={"name": "Alice"}, headers={"Origin": GOOD_ORIGIN}
    )
    assert response.status_code == 302
    assert _player_count() == 1


def test_post_with_cross_origin_blocked(client):
    response = client.post(
        "/players", data={"name": "Mallory"}, headers={"Origin": EVIL_ORIGIN}
    )
    assert response.status_code == 403
    assert _player_count() == 0


def test_post_with_null_origin_blocked(client):
    # Sandboxed iframes / privacy-redacted requests send the literal "null".
    response = client.post(
        "/players", data={"name": "Mallory"}, headers={"Origin": "null"}
    )
    assert response.status_code == 403
    assert _player_count() == 0


def test_origin_port_mismatch_blocked(client):
    # netloc match is exact, so a differing explicit port is cross-origin.
    response = client.post(
        "/players", data={"name": "Mallory"}, headers={"Origin": "http://localhost:8080"}
    )
    assert response.status_code == 403
    assert _player_count() == 0


# --- Referer fallback ---


def test_post_with_matching_referer_allowed(client):
    response = client.post(
        "/players", data={"name": "Alice"}, headers={"Referer": f"{GOOD_ORIGIN}/players"}
    )
    assert response.status_code == 302
    assert _player_count() == 1


def test_post_with_cross_referer_blocked(client):
    response = client.post(
        "/players", data={"name": "Mallory"}, headers={"Referer": f"{EVIL_ORIGIN}/attack"}
    )
    assert response.status_code == 403
    assert _player_count() == 0


def test_origin_takes_precedence_over_referer(client):
    # A matching Origin decides the request even if Referer looks foreign.
    response = client.post(
        "/players",
        data={"name": "Alice"},
        headers={"Origin": GOOD_ORIGIN, "Referer": f"{EVIL_ORIGIN}/attack"},
    )
    assert response.status_code == 302
    assert _player_count() == 1


def test_evil_origin_not_rescued_by_good_referer(client):
    response = client.post(
        "/players",
        data={"name": "Mallory"},
        headers={"Origin": EVIL_ORIGIN, "Referer": f"{GOOD_ORIGIN}/players"},
    )
    assert response.status_code == 403
    assert _player_count() == 0


# --- Safe methods unaffected ---


def test_get_with_cross_origin_allowed(client):
    response = client.get("/", headers={"Origin": EVIL_ORIGIN})
    assert response.status_code == 200


def test_get_with_cross_referer_allowed(client):
    response = client.get("/players", headers={"Referer": f"{EVIL_ORIGIN}/x"})
    assert response.status_code == 200


# --- APP_HOST / APP_ORIGIN pinning ---


def test_app_host_pinning_allows_configured_host(client, monkeypatch):
    monkeypatch.setattr(app_module, "APP_HOST", "km.example.com")
    response = client.post(
        "/players", data={"name": "Alice"}, headers={"Origin": "https://km.example.com"}
    )
    assert response.status_code == 302
    assert _player_count() == 1


def test_app_host_pinning_blocks_other_hosts(client, monkeypatch):
    # With APP_HOST pinned, even the request's own Host no longer matches —
    # this is the defense against Host-header confusion.
    monkeypatch.setattr(app_module, "APP_HOST", "km.example.com")
    response = client.post(
        "/players", data={"name": "Mallory"}, headers={"Origin": GOOD_ORIGIN}
    )
    assert response.status_code == 403
    assert _player_count() == 0


def test_app_origin_fallback_used_when_no_app_host(client, monkeypatch):
    monkeypatch.setattr(app_module, "APP_HOST", "")
    monkeypatch.setattr(app_module, "APP_ORIGIN", "https://km.example.com")
    allowed = client.post(
        "/players", data={"name": "Alice"}, headers={"Origin": "https://km.example.com"}
    )
    assert allowed.status_code == 302
    blocked = client.post(
        "/players", data={"name": "Mallory"}, headers={"Origin": GOOD_ORIGIN}
    )
    assert blocked.status_code == 403
    assert _player_count() == 1


def test_scheme_not_required_to_match(client, monkeypatch):
    # The app is served over http internally and https at the edge; only the
    # host is pinned, so an https Origin for the same host is fine.
    monkeypatch.setattr(app_module, "APP_HOST", "localhost")
    response = client.post(
        "/players", data={"name": "Alice"}, headers={"Origin": "https://localhost"}
    )
    assert response.status_code == 302
    assert _player_count() == 1


# --- JSON endpoints and other state-changing routes are covered too ---


def test_json_endpoint_blocked_cross_origin(client):
    create_player(client, "Alice")
    client.post("/cup-session/new", data={"player_ids[]": ["1"]})
    response = client.post(
        "/cup-session/1/spin", headers={"Origin": EVIL_ORIGIN}
    )
    assert response.status_code == 403


def test_delete_route_blocked_cross_origin(client):
    create_player(client, "Alice")
    response = client.post(
        "/players/1/delete", headers={"Origin": EVIL_ORIGIN}
    )
    assert response.status_code == 403
    assert _player_count() == 1  # nothing deleted


def test_csrf_check_runs_before_routing(client):
    # Even a would-be-404 URL is rejected first: no probing via CSRF requests.
    response = client.post("/players/999999/delete", headers={"Origin": EVIL_ORIGIN})
    assert response.status_code == 403


# --- Kill switch ---


def test_csrf_disabled_flag_allows_cross_origin(client, monkeypatch):
    monkeypatch.setattr(app_module, "CSRF_PROTECTION", False)
    response = client.post(
        "/players", data={"name": "Alice"}, headers={"Origin": EVIL_ORIGIN}
    )
    assert response.status_code == 302
    assert _player_count() == 1
