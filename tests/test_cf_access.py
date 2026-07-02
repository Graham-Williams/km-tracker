"""Cloudflare Access JWT verification (_verify_cf_access_token + before_request hook).

These tests exercise the CF Access path that is normally dormant in tests
(CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD unset). A real RS256 keypair is
generated in-process and the JWKS fetch is monkeypatched, so no network is
ever touched.
"""

import json
import time
import urllib.request

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import app as app_module
from db import get_connection

TEAM_DOMAIN = "testteam.cloudflareaccess.com"
AUD = "test-aud-0123456789abcdef"
KID = "test-key-1"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard guarantee: nothing in this module may touch the network."""

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted during CF Access tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture(scope="module")
def signing_key():
    """The 'Cloudflare' keypair whose public half is served via the fake JWKS."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def attacker_key():
    """A different keypair, NOT in the JWKS — used for bad-signature tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_for(private_key, kid=KID):
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    return {"keys": [jwk]}


@pytest.fixture
def cf_access(monkeypatch, signing_key):
    """Enable CF Access enforcement with a fake, counted JWKS fetch.

    Returns a dict: fetch call count + the mutable JWKS payload (tests can
    swap it to simulate key rotation or set 'error' to simulate an outage).
    """
    state = {"fetches": 0, "jwks": _jwks_for(signing_key), "error": None}

    def fake_fetch():
        state["fetches"] += 1
        if state["error"] is not None:
            raise state["error"]
        return state["jwks"]

    monkeypatch.setattr(app_module, "CF_ACCESS_ENABLED", True)
    monkeypatch.setattr(app_module, "CF_ACCESS_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setattr(app_module, "CF_ACCESS_AUD", AUD)
    monkeypatch.setattr(app_module, "_fetch_cf_jwks", fake_fetch)
    # Fresh in-process cache for every test.
    monkeypatch.setattr(app_module, "_jwks_keys", {})
    monkeypatch.setattr(app_module, "_jwks_last_fetch", None)
    return state


def make_token(
    private_key,
    *,
    aud=AUD,
    iss=None,
    kid=KID,
    exp_delta=3600,
    iat_delta=-5,
    drop=(),
    algorithm="RS256",
):
    """Mint a CF-Access-shaped JWT. `drop` removes claims (e.g. 'exp')."""
    now = int(time.time())
    claims = {
        "aud": aud,
        "iss": iss if iss is not None else f"https://{TEAM_DOMAIN}",
        "iat": now + iat_delta,
        "exp": now + exp_delta,
        "email": "graham@example.com",
        "sub": "test-subject",
    }
    for claim in drop:
        claims.pop(claim, None)
    headers = {"kid": kid} if kid else None
    return jwt.encode(claims, private_key, algorithm=algorithm, headers=headers)


# --- Disabled (default) mode ---


def test_disabled_by_default_no_token_needed(client):
    # CF_ACCESS_* are unset in the test env, so enforcement is off entirely.
    assert app_module.CF_ACCESS_ENABLED is False
    assert client.get("/").status_code == 200


# --- Valid tokens ---


def test_valid_token_in_header_accepted(client, cf_access, signing_key):
    token = make_token(signing_key)
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200


def test_valid_token_in_cookie_accepted(client, cf_access, signing_key):
    client.set_cookie("CF_Authorization", make_token(signing_key))
    assert client.get("/").status_code == 200


def test_valid_token_allows_post(client, cf_access, signing_key):
    token = make_token(signing_key)
    response = client.post(
        "/players",
        data={"name": "Alice"},
        headers={"Cf-Access-Jwt-Assertion": token},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Alice" in response.data


# --- Missing / invalid tokens -> 403, fail closed ---


def test_missing_token_rejected(client, cf_access):
    assert client.get("/").status_code == 403


def test_missing_token_blocks_post_and_persists_nothing(client, cf_access):
    response = client.post("/players", data={"name": "Mallory"})
    assert response.status_code == 403
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    assert count == 0


def test_expired_token_rejected(client, cf_access, signing_key):
    # Expired an hour ago — far outside the 10s leeway.
    token = make_token(signing_key, exp_delta=-3600, iat_delta=-7200)
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_wrong_audience_rejected(client, cf_access, signing_key):
    token = make_token(signing_key, aud="some-other-app-aud")
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_wrong_issuer_rejected(client, cf_access, signing_key):
    token = make_token(signing_key, iss="https://evilteam.cloudflareaccess.com")
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_bad_signature_rejected(client, cf_access, attacker_key):
    # Signed by a key that is NOT in the JWKS, but claiming the known kid.
    token = make_token(attacker_key, kid=KID)
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_malformed_token_rejected(client, cf_access):
    for garbage in ("not-a-jwt", "a.b.c", "", "Bearer xyz", "\x00\x01"):
        response = client.get("/", headers={"Cf-Access-Jwt-Assertion": garbage})
        assert response.status_code == 403, f"garbage token accepted: {garbage!r}"


def test_token_without_kid_rejected(client, cf_access, signing_key):
    token = make_token(signing_key, kid=None)
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_hs256_token_rejected(client, cf_access):
    # Algorithm-confusion attempt: HMAC token claiming the RSA kid. The
    # verifier pins algorithms=["RS256"], so this must fail.
    token = make_token("x" * 32, kid=KID, algorithm="HS256")
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_token_missing_exp_claim_rejected(client, cf_access, signing_key):
    # exp/iat/aud/iss are all in options["require"].
    token = make_token(signing_key, drop=("exp",))
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_token_missing_iat_claim_rejected(client, cf_access, signing_key):
    token = make_token(signing_key, drop=("iat",))
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


# --- Static asset exemption ---


def test_static_assets_exempt_from_access_check(client, cf_access):
    # CSS/images must load on the Access login redirect page etc.
    response = client.get("/static/favicon-32x32.png")
    assert response.status_code == 200


# --- JWKS caching / throttling ---


def test_jwks_fetched_once_then_cached(client, cf_access, signing_key):
    token = make_token(signing_key)
    for _ in range(3):
        assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 200
    assert cf_access["fetches"] == 1


def test_unknown_kid_fetch_is_throttled(client, cf_access, signing_key):
    # Attacker-controlled kids must not trigger an outbound fetch per request.
    for _ in range(5):
        token = make_token(signing_key, kid="bogus-kid")
        response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
        assert response.status_code == 403
    assert cf_access["fetches"] == 1


def test_key_rotation_picked_up_and_old_key_evicted(
    client, cf_access, signing_key, attacker_key, monkeypatch
):
    # Prime the cache with the original key.
    token_old = make_token(signing_key)
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token_old}).status_code == 200
    assert cf_access["fetches"] == 1

    # Cloudflare rotates: JWKS now serves only a new key. Disable the throttle
    # so the refetch happens immediately.
    monkeypatch.setattr(app_module, "REFETCH_MIN_INTERVAL", 0)
    new_key = attacker_key  # reuse the second keypair as the rotated-in key
    cf_access["jwks"] = _jwks_for(new_key, kid="rotated-key-2")

    token_new = make_token(new_key, kid="rotated-key-2")
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token_new}).status_code == 200

    # The cache is replaced wholesale, so the rotated-out kid is gone: a token
    # under the old kid now fails even though it once verified.
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token_old})
    assert response.status_code == 403


def test_jwks_fetch_failure_fails_closed(client, cf_access, signing_key):
    cf_access["error"] = OSError("simulated JWKS outage")
    token = make_token(signing_key)
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    # 403, not 500 — and definitely not allowed through.
    assert response.status_code == 403
