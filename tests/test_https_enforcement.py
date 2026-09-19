"""HTTPS enforcement at the origin: http->https redirect + HSTS (issue #85).

Defence in depth behind Cloudflare. The load-bearing rules under test:

  * redirect ONLY when X-Forwarded-Proto is present and (case-insensitively)
    exactly "http" — an ABSENT header must pass straight through, which is what
    keeps the in-network health probe, CI smoke checks and this very test suite
    working;
  * the target is built from the pinned APP_HOST, never from the request's own
    Host header (that would be an open redirect);
  * APP_HOST unset *or not a bare hostname* -> fail open (no redirect), so local
    dev keeps working and a typo'd pin can never emit a hostile Location;
  * path + query survive byte-for-byte, percent-encoding included;
  * the redirect is 307 + `Cache-Control: no-store` + `Vary: X-Forwarded-Proto`,
    never a cacheable 301.
"""

import pytest

import app as app_module

HOST = "km.graham-williams.com"
HSTS = "max-age=31536000"


@pytest.fixture
def pinned(monkeypatch):
    """Pin APP_HOST the way production does."""
    monkeypatch.setattr(app_module, "APP_HOST", HOST)
    monkeypatch.setattr(app_module, "HTTPS_REDIRECT_HOST", HOST)


def _http(client, path, **kwargs):
    """Request `path` as a visitor arriving over plain http via the tunnel."""
    headers = dict(kwargs.pop("headers", {}))
    headers["X-Forwarded-Proto"] = "http"
    return client.get(path, headers=headers, **kwargs)


# --- The redirect itself ---------------------------------------------------


def test_plain_http_is_redirected_to_https(client, pinned):
    resp = _http(client, "/")
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/"


def test_redirect_preserves_path_and_query(client, pinned):
    resp = _http(client, "/line-finder?edition=wii&half_life=90")
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/line-finder?edition=wii&half_life=90"


def test_redirect_preserves_percent_encoding(client, pinned):
    # request.path / request.full_path are already URL-DECODED, so a naive
    # f-string would emit "/cups/a b?c/x" here and mangle the target. The
    # redirect must round-trip the raw request line byte-for-byte.
    resp = _http(client, "/cups/a%20b%3Fc/x?q=1%262&z=caf%C3%A9")
    assert resp.status_code == 307
    assert resp.headers["Location"] == (
        f"https://{HOST}/cups/a%20b%3Fc/x?q=1%262&z=caf%C3%A9"
    )


def test_redirect_is_307_not_301(client, pinned):
    # ⚠️ NOT 301. A 301 is heuristically cacheable *indefinitely* under RFC 9111
    # with no Cache-Control, so a misconfigured APP_HOST (e.g. staging deployed
    # without docker-compose.staging.yml, inheriting prod's default) would burn a
    # permanent staging -> prod redirect into every visitor's browser, unfixable
    # by any later deploy. 307 also preserves the method.
    assert _http(client, "/cups").status_code == 307


def test_redirect_is_not_cacheable(client, pinned):
    resp = _http(client, "/cups")
    assert resp.headers["Cache-Control"] == "no-store"
    # The decision depends entirely on a request header shared caches don't key
    # on by default, so say so explicitly.
    assert resp.headers["Vary"] == "X-Forwarded-Proto"


def test_post_over_http_keeps_its_method(client, pinned):
    # 307 (not 301/302) is what stops a plain-http POST being silently
    # downgraded to a bodiless GET by the browser on the retry.
    resp = client.post(
        "/players", data={"name": "Toad"}, headers={"X-Forwarded-Proto": "http"}
    )
    assert resp.status_code == 307


def test_post_over_http_is_redirected_too(client, pinned):
    # The hook is registered FIRST, so it beats the CSRF check as well.
    resp = client.post(
        "/players", data={"name": "Toad"}, headers={"X-Forwarded-Proto": "http"}
    )
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/players"


# --- No host reflection (open-redirect guard) ------------------------------


def test_crafted_host_header_is_not_reflected(client, pinned):
    resp = _http(client, "/", headers={"Host": "evil.example.com"})
    assert resp.status_code == 307
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
    assert resp.status_code == 307
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
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/cups"
    assert "X-Injected" not in resp.headers


def test_fallback_rebuild_re_encodes_a_literal_percent(client, pinned):
    # FALLBACK branch ONLY (no usable raw target — here forced with "*"):
    # PATH_INFO arrives percent-DECODED, so a literal '%' must come back out as
    # '%25', not as a bare '%'.
    #
    # ⚠️ This is NOT what production does. Under gunicorn RAW_URI is always set,
    # so the passthrough branch below runs instead and echoes the raw '%'.
    resp = _http(
        client,
        "/cups/100%25",
        environ_overrides={"RAW_URI": "*", "REQUEST_URI": "*"},
    )
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/cups/100%25"


def test_raw_uri_passthrough_echoes_the_request_line_verbatim(client, pinned):
    # THE production branch (gunicorn always sets RAW_URI). It is a byte-for-byte
    # echo of the request line, so a literal '%' stays a bare '%' — no re-encoding
    # happens here, and that is correct: it is exactly what the client sent.
    resp = _http(
        client, "/cups", environ_overrides={"RAW_URI": "/cups/100%"}
    )
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/cups/100%"


# --- X-Forwarded-Proto is matched case-insensitively -----------------------


@pytest.mark.parametrize("value", ["http", "HTTP", "Http", " http ", "hTTp"])
def test_forwarded_proto_is_matched_case_insensitively(client, pinned, value):
    # Schemes are case-insensitive (RFC 9110). An exact `!= "http"` comparison
    # failed in the DANGEROUS direction: `X-Forwarded-Proto: HTTP` was served
    # 200 over plain http, silently.
    resp = client.get("/cups", headers={"X-Forwarded-Proto": value})
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/cups"


@pytest.mark.parametrize("value", ["http, https", "https, http", "HTTP, HTTPS"])
def test_multi_hop_forwarded_proto_does_not_redirect(client, pinned, value):
    # A comma-joined multi-hop value is NOT a lone "http": the left-most entry is
    # the client's scheme, but we only trust the single-hop form cloudflared
    # sends. Anything else falls through rather than guessing.
    resp = client.get("/healthz", headers={"X-Forwarded-Proto": value})
    assert resp.status_code == 200


# --- APP_HOST validation (it is spliced into a Location header) ------------


@pytest.mark.parametrize(
    "value",
    [
        "km.graham-williams.com",
        "staging-km.graham-williams.com",
        "localhost",
    ],
)
def test_valid_app_host_values_are_accepted(value):
    assert app_module._validated_redirect_host(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "km.graham-williams.com@evil.com",  # userinfo trick -> browser goes to evil.com
        "https://km.graham-williams.com",   # plausible typo (APP_ORIGIN DOES take a scheme)
        "http://km.graham-williams.com",
        "km.graham-williams.com\n",         # ⚠️ "$" would match before a trailing newline
        "km.graham-williams.com\r\nX-Injected: yes",
        "km.graham-williams.com/cups",      # path
        "km.graham-williams.com:8080",      # port (siblings take bare hostnames only)
        "km.graham-williams.com ",          # trailing space
        " km.graham-williams.com",          # leading space
        "km graham-williams.com",           # interior space
        "-km.graham-williams.com",          # leading hyphen
        "",
        None,
    ],
)
def test_hostile_or_malformed_app_host_is_rejected(value):
    # NB whitespace is REJECTED, not stripped: APP_HOST is already stripped where
    # it is read, so this layer stays strict as a second line of defence.
    assert app_module._validated_redirect_host(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "km.graham-williams.com@evil.com",
        "https://km.graham-williams.com",
        "km.graham-williams.com\n",
    ],
)
def test_invalid_app_host_disables_the_redirect_rather_than_emitting_it(
    client, monkeypatch, value
):
    # Fail open, matching the unset-APP_HOST posture: no redirect at all beats a
    # Location that points at evil.com or is syntactically broken.
    monkeypatch.setattr(app_module, "APP_HOST", value)
    monkeypatch.setattr(
        app_module, "HTTPS_REDIRECT_HOST", app_module._validated_redirect_host(value)
    )
    resp = _http(client, "/healthz")
    assert resp.status_code == 200
    assert "Location" not in resp.headers


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
    monkeypatch.setattr(app_module, "HTTPS_REDIRECT_HOST", "")
    resp = _http(client, "/healthz")
    assert resp.status_code == 200


def test_redirect_runs_before_the_password_gate(client, pinned, monkeypatch):
    # A plain-http visitor must never be handed the login page in the clear.
    monkeypatch.setattr(app_module, "APP_PASSWORD", "hunter2")
    monkeypatch.setattr(app_module, "PASSWORD_GATE_ENABLED", True)
    resp = _http(client, "/cups")
    assert resp.status_code == 307
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


def test_session_cookie_secure_default_actually_reaches_the_config(tmp_path):
    """The DEFAULT resolution must land in app.config, not just in _env_flag.

    Mutation-proven gap: replacing
        app.config["SESSION_COOKIE_SECURE"] = _env_flag("SESSION_COOKIE_SECURE", True)
    with a hard-coded `= False` left the whole suite green — the other tests
    exercise `_env_flag` in isolation, and tests/test_password_gate.py forces the
    config to True before asserting the Secure cookie. Nothing asserted the wire
    between them.

    Import happens in a FRESH interpreter with SESSION_COOKIE_SECURE unset,
    because app.py resolves this once at import time and the in-process
    `app_module.app` has already been mutated by other tests.
    """
    import os
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SESSION_COOKIE_SECURE", "DB_PATH", "APP_HOST", "APP_ENV")
    }
    env["PYTHONPATH"] = repo_root
    # cwd is a throwaway dir so load_dotenv() can't pick up a stray .env.
    proc = subprocess.run(
        [sys.executable, "-c",
         "import app; print(app.app.config['SESSION_COOKIE_SECURE'])"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True", proc.stdout + proc.stderr


def _import_app_with_env(tmp_path, **overrides):
    """Import app.py in a FRESH interpreter; return (stdout, stderr).

    Import-time behaviour (the APP_HOST validation + its warnings) can't be
    observed in-process: app.py resolves it once, long before the test runs.
    cwd is a throwaway dir so load_dotenv() can't pick up a stray .env.
    """
    import os
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("APP_HOST", "APP_ENV", "DB_PATH")
    }
    env["PYTHONPATH"] = repo_root
    env.update({k: v for k, v in overrides.items() if v is not None})
    proc = subprocess.run(
        [sys.executable, "-c",
         "import app; print('REDIRECT_HOST=%r' % app.HTTPS_REDIRECT_HOST)"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, proc.stderr


def test_blank_app_host_in_production_warns_loudly(tmp_path):
    # Fail-open used to be completely SILENT, and HSTS keeps being sent — so an
    # external `curl -I` still looks enforced while the redirect is off.
    stdout, stderr = _import_app_with_env(tmp_path, APP_ENV="production")
    assert "REDIRECT_HOST=''" in stdout
    assert "APP_HOST is not set" in stderr
    assert "DISABLED" in stderr


def test_blank_app_host_is_quiet_when_app_env_was_never_set(tmp_path):
    # APP_ENV *defaults* to "production", so testing it would print the warning
    # above on every local `import app`, pytest run and dev-server start, where
    # it means nothing — and noise trains people to ignore the same warning on
    # the box, where it means the redirect is off. The gate reads the RAW env
    # var; both compose files set APP_ENV explicitly, so prod/staging still warn.
    stdout, stderr = _import_app_with_env(tmp_path)
    assert "REDIRECT_HOST=''" in stdout  # still fails open, just silently
    assert "APP_HOST is not set" not in stderr


def test_invalid_app_host_warns_even_without_app_env(tmp_path):
    # The invalid-value warning is deliberately NOT gated: a typo'd APP_HOST is
    # worth shouting about wherever it happens.
    stdout, stderr = _import_app_with_env(
        tmp_path, APP_HOST="km.graham-williams.com@evil.com"
    )
    assert "REDIRECT_HOST=''" in stdout
    assert "not a bare hostname" in stderr


def test_invalid_app_host_warns_and_disables_the_redirect_at_import(tmp_path):
    stdout, stderr = _import_app_with_env(
        tmp_path, APP_ENV="production", APP_HOST="km.graham-williams.com@evil.com"
    )
    assert "REDIRECT_HOST=''" in stdout
    assert "not a bare hostname" in stderr
    assert "evil.com" not in stdout  # never reaches a usable redirect target


def test_valid_app_host_resolves_at_import_without_warning(tmp_path):
    stdout, stderr = _import_app_with_env(
        tmp_path, APP_ENV="production", APP_HOST="km.graham-williams.com"
    )
    assert "REDIRECT_HOST='km.graham-williams.com'" in stdout
    assert "http->https redirect is DISABLED" not in stderr
