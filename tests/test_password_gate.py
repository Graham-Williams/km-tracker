"""App-level shared-password login gate (password_gate_check + /login + /logout).

The gate is dormant by default (APP_PASSWORD unset in the test env). Each test
that exercises the active gate flips PASSWORD_GATE_ENABLED + APP_PASSWORD via
monkeypatch, mirroring how the CF-Access tests toggle CF_ACCESS_ENABLED.

Secure cookies are turned off for the round-trip tests (the test client speaks
plain HTTP, so a Secure cookie would never be resent); a dedicated test asserts
the Secure flag IS emitted on the Set-Cookie header.
"""

import re

import pytest

import app as app_module
from db import get_connection

PASSWORD = "correct horse battery staple"


@pytest.fixture
def gate(monkeypatch):
    """Activate the password gate with a known password.

    SESSION_COOKIE_SECURE is disabled so the signed session cookie round-trips
    over the test client's plain-HTTP requests. A separate test checks the flag.
    """
    monkeypatch.setattr(app_module, "APP_PASSWORD", PASSWORD)
    monkeypatch.setattr(app_module, "PASSWORD_GATE_ENABLED", True)
    app_module.app.config["SESSION_COOKIE_SECURE"] = False
    # Fresh rate-limit state per test.
    monkeypatch.setattr(app_module, "_login_failures", {})
    yield
    app_module.app.config["SESSION_COOKIE_SECURE"] = True


def _login(client, password, next_=None):
    data = {"password": password}
    if next_ is not None:
        data["next"] = next_
    return client.post("/login", data=data)


def _player_count():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    return count


# --- Gate OFF by default (APP_PASSWORD empty) ---


def test_gate_disabled_by_default(client):
    assert app_module.PASSWORD_GATE_ENABLED is False
    # App works with no login at all.
    assert client.get("/").status_code == 200
    assert client.get("/players").status_code == 200


def test_login_page_redirects_to_index_when_gate_off(client):
    # Nothing to log into when the gate is off.
    resp = client.get("/login")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


# --- Gate ON: unauthenticated requests are redirected to /login ---


def test_unauthenticated_protected_route_redirects_to_login(client, gate):
    resp = client.get("/players")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "/login" in loc
    assert "next=%2Fplayers" in loc or "next=/players" in loc


def test_unauthenticated_root_redirects_to_login(client, gate):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_page_itself_is_reachable_when_gate_on(client, gate):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Password" in resp.data


# --- Correct password grants access ---


def test_correct_password_sets_session_and_grants_access(client, gate):
    resp = _login(client, PASSWORD)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    # Session cookie now lets protected routes through.
    assert client.get("/players").status_code == 200
    assert client.get("/").status_code == 200


def test_correct_password_redirects_to_safe_next(client, gate):
    resp = _login(client, PASSWORD, next_="/cups")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/cups")


# --- Wrong password is rejected ---


def test_wrong_password_rejected(client, gate):
    resp = _login(client, "wrong")
    assert resp.status_code == 401
    # Still locked out.
    assert client.get("/players").status_code == 302


def test_empty_password_rejected(client, gate):
    resp = _login(client, "")
    assert resp.status_code == 401
    assert client.get("/").status_code == 302


# --- Logout clears the session ---


def test_logout_clears_session(client, gate):
    _login(client, PASSWORD)
    assert client.get("/players").status_code == 200
    resp = client.get("/logout")
    assert resp.status_code == 302
    # Back to being gated.
    assert client.get("/players").status_code == 302


# --- Exempt routes stay open when the gate is active ---


def test_static_css_open_when_gate_on(client, gate):
    # CI guards that /static/css/app.css returns 200 — must survive the gate.
    resp = client.get("/static/css/app.css")
    assert resp.status_code == 200


def test_healthz_open_when_gate_on(client, gate):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert b"ok" in resp.data


# --- Open-redirect protection on `next` ---


@pytest.mark.parametrize(
    "evil_next",
    [
        "https://evil.example/phish",
        "http://evil.example",
        "//evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "evil.example",
    ],
)
def test_open_redirect_next_is_rejected(client, gate, evil_next):
    resp = _login(client, PASSWORD, next_=evil_next)
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    # Falls back to index; never redirects off-site.
    assert loc.endswith("/")
    assert "evil.example" not in loc
    assert "javascript:" not in loc


def test_local_next_with_query_preserved(client, gate):
    resp = _login(client, PASSWORD, next_="/cups?edition=switch")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/cups?edition=switch")


# --- Rate limiting ---


def test_rate_limit_trips_after_max_failures(client, gate):
    # First RATE_LIMIT_MAX wrong attempts are answered 401...
    for _ in range(app_module.RATE_LIMIT_MAX):
        assert _login(client, "wrong").status_code == 401
    # ...the next attempt is throttled (429), even with the CORRECT password —
    # the throttle short-circuits before the password is checked.
    resp = _login(client, PASSWORD)
    assert resp.status_code == 429
    # And a genuine session is not granted while throttled.
    assert client.get("/players").status_code == 302


def test_rate_limit_is_per_ip(client, gate):
    # Exhaust attempts for one IP via CF-Connecting-IP header.
    for _ in range(app_module.RATE_LIMIT_MAX):
        client.post(
            "/login",
            data={"password": "wrong"},
            headers={"CF-Connecting-IP": "10.0.0.1"},
        )
    blocked = client.post(
        "/login", data={"password": "wrong"}, headers={"CF-Connecting-IP": "10.0.0.1"}
    )
    assert blocked.status_code == 429
    # A different IP is unaffected.
    other = client.post(
        "/login", data={"password": "wrong"}, headers={"CF-Connecting-IP": "10.0.0.2"}
    )
    assert other.status_code == 401


def test_successful_login_clears_failure_counter(client, gate):
    for _ in range(app_module.RATE_LIMIT_MAX - 1):
        assert _login(client, "wrong").status_code == 401
    # A correct login before the cap resets the bucket.
    assert _login(client, PASSWORD).status_code == 302
    client.get("/logout")
    # Fresh allowance — a wrong attempt is 401 (not immediately throttled).
    assert _login(client, "wrong").status_code == 401


# --- Cookie flags ---


def test_session_cookie_flags_are_hardened(client, gate):
    # Assert on the Set-Cookie header directly (no round-trip needed), with the
    # Secure flag forced ON as it is in production.
    app_module.app.config["SESSION_COOKIE_SECURE"] = True
    try:
        resp = _login(client, PASSWORD)
    finally:
        app_module.app.config["SESSION_COOKIE_SECURE"] = False
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert re.search(r"SameSite=Lax", set_cookie)


def test_password_not_stored_in_cookie(client, gate):
    resp = _login(client, PASSWORD)
    set_cookie = resp.headers.get("Set-Cookie", "")
    # The raw shared secret must never appear in the cookie.
    assert PASSWORD not in set_cookie
    assert "correct horse" not in set_cookie


# --- Forged session cannot bypass the gate ---


def test_forged_session_marker_rejected(client, gate, monkeypatch):
    # A cookie not signed by SECRET_KEY must not authenticate. Simulate by
    # setting a bogus session cookie value and confirming it doesn't grant access.
    client.set_cookie("session", "kmauth-true-but-unsigned")
    assert client.get("/players").status_code == 302


# --- CF Access and the password gate don't interfere ---


def test_healthz_open_regardless_of_gates(client):
    # With everything default (both gates off) health still responds.
    assert client.get("/healthz").status_code == 200
