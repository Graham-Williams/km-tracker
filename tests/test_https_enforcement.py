"""HTTPS enforcement at the origin: http->https redirect + HSTS (issue #85).

Defence in depth behind Cloudflare. The load-bearing rules under test:

  * redirect ONLY when X-Forwarded-Proto is present and exactly "http" — an
    ABSENT header must pass straight through, which is what keeps the
    in-network health probe, CI smoke checks and this very test suite working;
  * the target is built from the pinned APP_HOST, never from the request's own
    Host header (that would be an open redirect);
  * APP_HOST unset -> fail open (no redirect), so local dev keeps working;
  * path + query survive byte-for-byte, percent-encoding included.
"""

import pytest

import app as app_module

HOST = "km.graham-williams.com"
HSTS = "max-age=31536000"


@pytest.fixture
def pinned(monkeypatch):
    """Pin APP_HOST the way production does."""
    monkeypatch.setattr(app_module, "APP_HOST", HOST)


def _http(client, path, **kwargs):
    """Request `path` as a visitor arriving over plain http via the tunnel."""
    headers = dict(kwargs.pop("headers", {}))
    headers["X-Forwarded-Proto"] = "http"
    return client.get(path, headers=headers, **kwargs)


# --- The redirect itself ---------------------------------------------------


def test_plain_http_is_redirected_to_https(client, pinned):
    resp = _http(client, "/")
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/"


def test_redirect_preserves_path_and_query(client, pinned):
    resp = _http(client, "/line-finder?edition=wii&half_life=90")
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/line-finder?edition=wii&half_life=90"


def test_redirect_preserves_percent_encoding(client, pinned):
    # request.path / request.full_path are already URL-DECODED, so a naive
    # f-string would emit "/cups/a b?c/x" here and mangle the target. The
    # redirect must round-trip the raw request line byte-for-byte.
    resp = _http(client, "/cups/a%20b%3Fc/x?q=1%262&z=caf%C3%A9")
    assert resp.status_code == 301
    assert resp.headers["Location"] == (
        f"https://{HOST}/cups/a%20b%3Fc/x?q=1%262&z=caf%C3%A9"
    )


def test_redirect_is_301_not_302(client, pinned):
    assert _http(client, "/cups").status_code == 301


def test_post_over_http_is_redirected_too(client, pinned):
    # The hook is registered FIRST, so it beats the CSRF check as well.
    resp = client.post(
        "/players", data={"name": "Toad"}, headers={"X-Forwarded-Proto": "http"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/players"


# --- No host reflection (open-redirect guard) ------------------------------


def test_crafted_host_header_is_not_reflected(client, pinned):
    resp = _http(client, "/", headers={"Host": "evil.example.com"})
    assert resp.status_code == 301
    location = resp.headers["Location"]
    assert location == f"https://{HOST}/"
    assert "evil.example.com" not in location


def test_absolute_form_request_uri_cannot_smuggle_a_host(client, pinned):
    # A raw request target that isn't origin-form is ignored; the path is
    # rebuilt from PATH_INFO instead, so no attacker host reaches Location.
    resp = _http(
        client,
        "/cups",
        environ_overrides={
            "RAW_URI": "http://evil.example.com/x",
            "REQUEST_URI": "http://evil.example.com/x",
        },
    )
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/cups"


def test_control_characters_in_raw_uri_cannot_split_the_header(client, pinned):
    resp = _http(
        client,
        "/cups",
        environ_overrides={
            "RAW_URI": "/x\r\nX-Injected: yes",
            "REQUEST_URI": "/x\r\nX-Injected: yes",
        },
    )
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/cups"
    assert "X-Injected" not in resp.headers


def test_rebuilt_target_re_encodes_a_literal_percent(client, pinned):
    # Fallback path (no usable raw target): PATH_INFO arrives percent-DECODED,
    # so a literal '%' must come back out as '%25', not as a bare '%'.
    resp = _http(
        client,
        "/cups/100%25",
        environ_overrides={"RAW_URI": "*", "REQUEST_URI": "*"},
    )
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/cups/100%25"


# --- When NOT to redirect --------------------------------------------------


def test_https_requests_are_not_redirected(client, pinned):
    resp = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    assert resp.data == b"ok\n"


def test_missing_forwarded_proto_is_not_redirected(client, pinned):
    # THE health-probe case: `docker exec ... urlopen('http://localhost:8080/
    # healthz')` sends no X-Forwarded-Proto at all. It must reach the app.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.data == b"ok\n"


def test_index_without_forwarded_proto_is_not_redirected(client, pinned):
    resp = client.get("/")
    assert resp.status_code == 200


def test_no_redirect_when_app_host_is_unset(client, monkeypatch):
    # Fail open: an empty pin would produce "https:///..." — a broken loop.
    monkeypatch.setattr(app_module, "APP_HOST", "")
    resp = _http(client, "/healthz")
    assert resp.status_code == 200


def test_redirect_runs_before_the_password_gate(client, pinned, monkeypatch):
    # A plain-http visitor must never be handed the login page in the clear.
    monkeypatch.setattr(app_module, "APP_PASSWORD", "hunter2")
    monkeypatch.setattr(app_module, "PASSWORD_GATE_ENABLED", True)
    resp = _http(client, "/cups")
    assert resp.status_code == 301
    assert resp.headers["Location"] == f"https://{HOST}/cups"


# --- HSTS ------------------------------------------------------------------


def test_hsts_header_on_a_normal_response(client):
    resp = client.get("/")
    assert resp.headers["Strict-Transport-Security"] == HSTS


def test_hsts_header_value_has_no_subdomains_or_preload(client):
    # Each host under graham-williams.com owns its own policy.
    value = client.get("/healthz").headers["Strict-Transport-Security"]
    assert value == "max-age=31536000"
    assert "includeSubDomains" not in value
    assert "preload" not in value


def test_hsts_header_on_the_redirect_itself(client, pinned):
    assert _http(client, "/").headers["Strict-Transport-Security"] == HSTS


def test_hsts_header_on_a_404(client):
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert resp.headers["Strict-Transport-Security"] == HSTS


# --- Session cookie flags --------------------------------------------------


def test_session_cookie_is_httponly_and_samesite():
    # Asserted at the config level here; tests/test_password_gate.py asserts the
    # flags on a real Set-Cookie header from a successful login.
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_session_cookie_secure_defaults_on_when_env_absent(monkeypatch):
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    assert app_module._env_flag("SESSION_COOKIE_SECURE", True) is True


def test_session_cookie_secure_can_only_be_disabled_explicitly(monkeypatch):
    # Local dev / the test client speak plain http, so the flag exists — but it
    # must take an explicit falsey value, never an accidental one.
    for value in ("1", "true", "yes", "on", "anything"):
        monkeypatch.setenv("SESSION_COOKIE_SECURE", value)
        assert app_module._env_flag("SESSION_COOKIE_SECURE", True) is True
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SESSION_COOKIE_SECURE", value)
        assert app_module._env_flag("SESSION_COOKIE_SECURE", True) is False
