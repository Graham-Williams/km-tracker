import base64
import binascii
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from collections import Counter

import jwt
from jwt.algorithms import RSAAlgorithm
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from werkzeug.routing import IntegerConverter, ValidationError

from db import get_connection, get_db_path, init_db
from extraction import ExtractionError, extract_standings, extraction_enabled
from maps import (
    DEFAULT_EDITION,
    EDITION_LABELS,
    TRACK_SETS,
    characters_for,
    courses_for,
    edition_label,
)

load_dotenv()

app = Flask(__name__)
# The session cookie is signed (itsdangerous HMAC) with this key. SECRET_KEY is
# the app's existing signing key and is REUSED for the login-gate session — so
# the shared-password gate needs no new signing secret. SESSION_SECRET is
# accepted only as an explicit alias/override if someone prefers a dedicated
# name; SECRET_KEY wins when both are set. Neither is ever hardcoded, and a
# random per-process key is the last resort (dev only — logins won't survive a
# restart, which is fine locally).
app.secret_key = (
    os.environ.get("SECRET_KEY")
    or os.environ.get("SESSION_SECRET")
    or secrets.token_hex(32)
)

# Session cookie hardening for the shared-password login gate.
#   - HttpOnly: JS can't read the auth cookie (default True, set explicit).
#   - SameSite=Lax: not sent on cross-site POSTs (defense-in-depth w/ CSRF pin).
#   - Secure: only sent over HTTPS. On by default (prod is HTTPS at the edge);
#     read from an env flag so the test suite / plain-HTTP local dev can turn it
#     off and let the cookie round-trip. Never store the password in the cookie
#     — only a signed "authenticated" marker (session["kmauth"]).
#   - Lifetime: 30 days so friends rarely have to re-enter the password.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure defaults ON; only an explicit falsey env value ('0'/'false'/'no'/'off'/'')
# turns it off (mirrors _env_flag, inlined here because _env_flag is defined
# further down and this runs at import time).
_secure_env = os.environ.get("SESSION_COOKIE_SECURE")
app.config["SESSION_COOKIE_SECURE"] = (
    True
    if _secure_env is None
    else _secure_env.strip().lower() not in ("0", "false", "no", "off", "")
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# Werkzeug's default <int:...> converter accepts arbitrarily large integers.
# Binding one that exceeds SQLite's signed 64-bit INTEGER range raises
# OverflowError deep in the query -> uncaught 500 (issue #46). Cap the converter
# so an oversized path id simply fails to match the route -> automatic 404.
# Overriding the "int" converter key makes every existing <int:...> route
# inherit the cap without per-route changes.
class BoundedIntConverter(IntegerConverter):
    def to_python(self, value):
        result = super().to_python(value)
        if not (SQLITE_MIN_INT <= result <= SQLITE_MAX_INT):
            raise ValidationError()
        return result


app.url_map.converters["int"] = BoundedIntConverter

# Cap request body size to blunt memory-exhaustion DoS. The largest legit
# request is a score form / extraction call carrying a client-downscaled
# standings photo as base64 (~200-400 KB); 1 MB leaves comfortable headroom
# while still keeping a flood of oversized bodies from exhausting memory.
# Flask returns 413 Request Entity Too Large automatically when exceeded.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
# Environment awareness (staging vs production)
#
# APP_ENV is read once at startup. Unset or unrecognized -> "production", the
# safe default (the prod container doesn't need to set it). The staging
# container sets APP_ENV=staging (docker-compose.staging.yml). Both values are
# exposed to every template so environment-specific tweaks (titles, banners,
# etc.) stay easy.
# ---------------------------------------------------------------------------

APP_ENV = (os.environ.get("APP_ENV") or "production").strip().lower() or "production"
IS_STAGING = APP_ENV == "staging"


@app.context_processor
def inject_app_env():
    """Make the deployment environment available in all templates."""
    return {"app_env": APP_ENV, "is_staging": IS_STAGING}


@app.context_processor
def inject_photo_extraction():
    """Whether photo score extraction is available (ANTHROPIC_API_KEY set).

    When False, templates hide the extraction behavior but the photo-attach
    controls still render — attaching a photo to a cup never needs the API.
    """
    return {"photo_extraction_enabled": extraction_enabled()}


# ---------------------------------------------------------------------------
# Security hardening: CSRF (Origin/Referer) + Cloudflare Access JWT verification
#
# The app is single-origin and sits behind Cloudflare Access. The Access auth
# cookie IS sent on cross-site requests, so a malicious page could otherwise
# forge state-changing requests. These two before_request hooks defend against
# that (CSRF) and add defense-in-depth in case the Access policy is bypassed.
# ---------------------------------------------------------------------------


def _env_flag(name, default):
    """Read a boolean-ish env var. Absent -> default; '0'/'false'/'no'/'off'/'' -> False."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


# --- 1. CSRF protection via Origin/Referer host matching ---

CSRF_PROTECTION = _env_flag("CSRF_PROTECTION", True)  # default ON; set 0 to disable for local dev
APP_HOST = os.environ.get("APP_HOST", "").strip()
APP_ORIGIN = os.environ.get("APP_ORIGIN", "").strip()

STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _expected_host():
    """The host this app considers 'itself'. Prefer explicit config, else the request Host."""
    if APP_HOST:
        return APP_HOST
    if APP_ORIGIN:
        return urlsplit(APP_ORIGIN).netloc
    return request.host


@app.before_request
def csrf_origin_check():
    """Block cross-origin state-changing requests (CSRF).

    Browsers always send an Origin header on cross-origin POST/PUT/PATCH/DELETE
    (and on same-origin ones for these methods too), so matching its host against
    our own host blocks real CSRF. Non-browser clients (curl, the Flask test
    client) send neither Origin nor Referer -> allowed, so this doesn't break the
    test suite or API-style callers.
    """
    if not CSRF_PROTECTION:
        return
    if request.method not in STATE_CHANGING_METHODS:
        return
    expected = _expected_host()
    origin = request.headers.get("Origin")
    if origin:
        parts = urlsplit(origin)
        # Exact host match is the CSRF defense. Scheme is NOT required: the app is
        # served over http internally / on the tailnet (and https via Cloudflare),
        # so requiring https here would 403 legitimate http requests.
        if parts.netloc != expected:
            abort(403, description="Cross-origin request blocked.")
        return
    referer = request.headers.get("Referer")
    if referer:
        if urlsplit(referer).netloc != expected:
            abort(403, description="Cross-origin request blocked.")
        return
    # Neither header present -> non-browser client -> allow.
    return


# --- 2. Cloudflare Access JWT verification (defense-in-depth) ---

CF_ACCESS_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip()
CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
# Only enforced in production, i.e. when BOTH are configured. Unset -> skip
# entirely so local dev and the tailnet keep working.
CF_ACCESS_ENABLED = bool(CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD)

_jwks_lock = threading.Lock()
_jwks_keys = {}  # kid -> public key object, cached in-process
_jwks_last_fetch = None  # monotonic timestamp of the last JWKS fetch *attempt* (None = never)
# Minimum seconds between JWKS fetch attempts. An unknown `kid` is attacker-
# controllable (read from the unverified JWT header), so without this throttle a
# flood of bogus-kid requests would trigger an uncached outbound fetch each time
# -> trivial DoS + thundering herd against the two sync gunicorn workers. This
# bounds fetches to at most one per interval while still picking up legitimate
# Cloudflare key rotation within ~REFETCH_MIN_INTERVAL seconds.
REFETCH_MIN_INTERVAL = 60
_JWKS_MAX_BYTES = 1024 * 1024  # cap the response body read (hostile/huge response)


def _fetch_cf_jwks():
    """Fetch the team's JWKS (stdlib urllib, no extra deps). Body read is capped."""
    url = f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (fixed https host)
        raw = resp.read(_JWKS_MAX_BYTES + 1)
    if len(raw) > _JWKS_MAX_BYTES:
        raise ValueError("JWKS response too large")
    return json.loads(raw.decode("utf-8"))


def _cf_signing_key(kid):
    """Return the cached public key for `kid`, refreshing the JWKS on an unknown kid.

    Fail-closed: any error or uncertainty returns None so the caller aborts 403.
    Refetches are single-flighted (under the lock) and throttled to at most one
    attempt per REFETCH_MIN_INTERVAL seconds, so an attacker spraying unknown
    kids can't force an outbound fetch per request.
    """
    global _jwks_last_fetch, _jwks_keys
    with _jwks_lock:
        key = _jwks_keys.get(kid)
        if key is not None:
            return key
        # Unknown kid: only fetch if we're outside the throttle window. Update the
        # last-attempt timestamp even on failure so failures don't bypass it.
        now = time.monotonic()
        if _jwks_last_fetch is not None and now - _jwks_last_fetch < REFETCH_MIN_INTERVAL:
            return None
        _jwks_last_fetch = now
        try:
            jwks = _fetch_cf_jwks()
            # Replace the cache wholesale so rotated-out keys are evicted, not merged.
            new_keys = {}
            for jwk in jwks.get("keys", []):
                k = jwk.get("kid")
                if k:
                    new_keys[k] = RSAAlgorithm.from_jwk(json.dumps(jwk))
            _jwks_keys = new_keys
        except Exception:
            app.logger.exception("Cloudflare Access JWKS fetch/parse failed")
            return None
        return _jwks_keys.get(kid)


def _verify_cf_access_token(token):
    """Verify an RS256 CF Access JWT: signature, aud, issuer, expiry. Raises on failure."""
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("missing kid")
    key = _cf_signing_key(kid)
    if key is None:
        raise jwt.InvalidTokenError("unknown signing key")
    return jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        audience=CF_ACCESS_AUD,
        issuer=f"https://{CF_ACCESS_TEAM_DOMAIN}",
        leeway=10,  # small tolerance for clock skew
        options={"require": ["exp", "iat", "aud", "iss"]},
    )


@app.before_request
def cloudflare_access_check():
    """Require a valid Cloudflare Access identity token in production.

    Enforced only when CF_ACCESS_TEAM_DOMAIN + CF_ACCESS_AUD are both set. Static
    assets are exempt so CSS/JS still load. The token comes from the
    Cf-Access-Jwt-Assertion header (Cloudflare injects it) or the CF_Authorization
    cookie as a fallback.
    """
    if not CF_ACCESS_ENABLED:
        return
    if request.path.startswith("/static/"):
        return
    if request.path == "/healthz":
        # Unauthenticated liveness probe — exempt from BOTH gates.
        return
    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
    if not token:
        abort(403, description="Cloudflare Access token missing.")
    try:
        _verify_cf_access_token(token)
    except Exception:
        # Log server-side (no token contents) so a real outage — e.g. a JWKS
        # fetch failure — is distinguishable from a merely invalid token in the
        # logs. Still fail closed with a 403.
        app.logger.exception("Cloudflare Access token verification failed")
        abort(403, description="Cloudflare Access token invalid.")


# ---------------------------------------------------------------------------
# 3. App-level shared-password login gate
#
# A single shared password (APP_PASSWORD) that Graham hands to friends. Entering
# it once grants a long-lived (30-day) signed-cookie session so they rarely have
# to re-auth. Designed to REPLACE the Cloudflare-Access emailed-PIN flow.
#
# Env-gated: when APP_PASSWORD is set the gate is ACTIVE; when unset/empty the
# gate is OFF and the app behaves exactly as before. This lets the gate ship
# dormant (while Cloudflare Access is still in front) and be switched on during
# cutover just by setting APP_PASSWORD. The two mechanisms are independent and
# meant to run one-at-a-time (Access in front, THEN the password gate after
# cutover); neither interferes with the other's before_request hook.
#
# Security properties:
#   - Constant-time password compare (hmac.compare_digest) — no timing oracle.
#   - The cookie only carries a signed "authenticated" marker (session["kmauth"]),
#     never the password; itsdangerous signs it with SECRET_KEY so it can't be
#     forged. Cookie flags: HttpOnly + Secure + SameSite=Lax (configured above).
#   - Open-redirect-safe `next` (local paths only).
#   - Per-IP brute-force rate limiting (in-memory; single container).
# ---------------------------------------------------------------------------

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PASSWORD_GATE_ENABLED = bool(APP_PASSWORD)

# Paths always reachable without a session when the gate is active: the login
# and logout routes themselves, and an unauthenticated health/liveness probe.
# (Static assets are handled separately via the /static/ prefix check.)
GATE_EXEMPT_PATHS = frozenset({"/login", "/logout", "/healthz"})

# Brute-force throttle: after RATE_LIMIT_MAX failed attempts from one client IP
# within RATE_LIMIT_WINDOW seconds, further attempts are refused (429) until the
# burst ages out of the window (~15 min). In-memory + per-process is acceptable
# per the single-container deployment.
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 15 * 60  # 15 minutes, in seconds
# Memory / global-abuse backstop: cap the number of distinct client IPs tracked
# at once. Normal traffic tracks a handful of failing IPs; only mass IP/header
# spoofing (which requires bypassing the Cloudflare tunnel — the app publishes
# no host port and CF overwrites CF-Connecting-IP at the edge) could reach this.
# When saturated, a brand-new IP is refused rather than growing the dict without
# bound, keeping the failure table at ~this many entries (concurrent inserts can
# transiently exceed it by at most the worker-thread count — still bounded).
# Reaching saturation would also lock out further new IPs until the process
# recycles; that only bites under the mass-spoof precondition above, an accepted
# tradeoff for a low-stakes single-tunnel app.
RATE_LIMIT_MAX_TRACKED_IPS = 10000
_login_fail_lock = threading.Lock()
_login_failures = {}  # client_ip -> list[timestamps of failures within the window]


def _client_ip():
    """Rate-limiting key for the client. Prefer Cloudflare's CF-Connecting-IP,
    falling back to the socket peer.

    TRUST NOTE: the app is only reachable through the Cloudflare tunnel (no host
    port is published — see docker-compose.yml), and Cloudflare *overwrites*
    CF-Connecting-IP at the edge, so a client cannot forge it on the real path.
    This is the same trust boundary the CF-Access check relies on. If the app
    were ever exposed directly (a published host port / tailnet), this header
    would become client-controllable and the per-IP throttle spoofable — the
    RATE_LIMIT_MAX_TRACKED_IPS cap bounds the blast radius (memory + a global
    ceiling) even in that case.
    """
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"


def _prune_failures(ip, now):
    """Return this IP's failure timestamps within the window (caller holds lock)."""
    kept = [t for t in _login_failures.get(ip, ()) if now - t < RATE_LIMIT_WINDOW]
    if kept:
        _login_failures[ip] = kept
    else:
        _login_failures.pop(ip, None)
    return kept


def _is_rate_limited(ip):
    now = time.time()
    with _login_fail_lock:
        if len(_prune_failures(ip, now)) >= RATE_LIMIT_MAX:
            return True
        # Backstop: if we're already tracking a pathological number of distinct
        # failing IPs, refuse a brand-new one instead of growing unbounded. This
        # both caps memory and imposes a very high global ceiling on a spoofing
        # flood. Normal traffic never approaches the cap (only *failing* IPs are
        # tracked and a success clears them), so this is inert in practice.
        if (
            len(_login_failures) >= RATE_LIMIT_MAX_TRACKED_IPS
            and ip not in _login_failures
        ):
            return True
        return False


def _record_login_failure(ip):
    now = time.time()
    with _login_fail_lock:
        kept = _prune_failures(ip, now)
        kept.append(now)
        _login_failures[ip] = kept


def _clear_login_failures(ip):
    with _login_fail_lock:
        _login_failures.pop(ip, None)


def _safe_next(target):
    """Validate a post-login redirect target: local same-site paths only.

    Rejects absolute URLs, protocol-relative (`//host`), and backslash tricks
    (`/\\host`) to prevent open redirects. Returns the safe path or None.
    """
    if not target or not target.startswith("/"):
        return None
    lowered = target.lower()
    # Reject protocol-relative (`//host`), backslash (`/\host`), and encoded-slash
    # (`/%2fhost`, any case) tricks that browsers may resolve to an off-site host.
    if lowered.startswith("//") or lowered.startswith("/\\") or lowered.startswith("/%2f"):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


def _is_authenticated():
    return session.get("kmauth") is True


@app.before_request
def password_gate_check():
    """Redirect unauthenticated requests to the login page when the gate is on.

    Exempt: static assets, the login/logout/health routes. Everything else
    requires session["kmauth"]; otherwise 302 -> /login?next=<original-path>.
    Runs AFTER the CSRF and CF-Access hooks (registration order) so those still
    apply to the login POST (same-origin) as normal.
    """
    if not PASSWORD_GATE_ENABLED:
        return
    if request.path.startswith("/static/"):
        return
    if request.path in GATE_EXEMPT_PATHS:
        return
    if _is_authenticated():
        return
    # full_path preserves the query string; werkzeug appends a trailing '?'
    # even when there's no query — strip it so `next` stays clean.
    original = request.full_path
    if original.endswith("?"):
        original = original[:-1]
    return redirect(url_for("login", next=original))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Shared-password login. GET renders the form; POST verifies the password."""
    # Gate off -> there's nothing to log into; send them to the app.
    if not PASSWORD_GATE_ENABLED:
        return redirect(url_for("index"))

    next_target = _safe_next(request.values.get("next"))

    # Already authenticated -> straight through.
    if _is_authenticated():
        return redirect(next_target or url_for("index"))

    if request.method == "POST":
        ip = _client_ip()
        if _is_rate_limited(ip):
            # Don't even check the password while throttled.
            return (
                render_template(
                    "login.html",
                    error="Too many attempts. Please wait a few minutes and try again.",
                    next=next_target or "",
                ),
                429,
            )
        submitted = request.form.get("password", "")
        # Constant-time compare; encode to bytes so non-ASCII passwords work.
        if hmac.compare_digest(submitted.encode("utf-8"), APP_PASSWORD.encode("utf-8")):
            session.clear()
            session["kmauth"] = True
            session.permanent = True  # honor PERMANENT_SESSION_LIFETIME (30d)
            _clear_login_failures(ip)
            return redirect(next_target or url_for("index"))
        # Failure — never log the submitted value.
        _record_login_failure(ip)
        return (
            render_template(
                "login.html",
                error="Incorrect password.",
                next=next_target or "",
            ),
            401,
        )

    return render_template("login.html", error=None, next=next_target or "")


@app.route("/logout")
def logout():
    """Clear the session cookie and return to the login page (or the app)."""
    session.clear()
    return redirect(url_for("login") if PASSWORD_GATE_ENABLED else url_for("index"))


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe — always reachable, even with the gate on."""
    return Response("ok\n", mimetype="text/plain")


# ---------------------------------------------------------------------------
# SQLite lock-contention backstop (issue #42)
#
# db.py sets busy_timeout + WAL so writers queue rather than failing instantly,
# which resolves nearly all contention. This handler is the last line of
# defence: if a writer still can't get the lock within busy_timeout, SQLite
# raises "database is locked". Rather than let that surface as a raw HTTP 500,
# we map it to a controlled response — a friendly flash + redirect for page
# navigations, or a 503 JSON body for the fetch()-driven action endpoints.
#
# Scoped narrowly to the lock message: any other OperationalError is a real
# bug and is re-raised so it still 500s (we never mask genuine failures).
# ---------------------------------------------------------------------------


@app.errorhandler(sqlite3.OperationalError)
def handle_sqlite_locked(error):
    if "locked" not in str(error).lower():
        # Not lock contention — a genuine error. Don't swallow it.
        raise error
    app.logger.warning(
        "SQLite lock contention on %s %s: %s", request.method, request.path, error
    )
    message = "The database was busy — please try again."
    # fetch()/XHR callers send Accept: */* (best_match ties to the first entry);
    # browser page navigations send Accept: text/html. Give the former a JSON
    # 503 they can handle, the latter a flash + redirect back.
    wants_json = (
        request.accept_mimetypes.best_match(["application/json", "text/html"])
        == "application/json"
    )
    if wants_json:
        return jsonify({"error": message}), 503
    flash(message, "error")
    # Only honor a same-host referrer (open-redirect guard) — the Referer is
    # browser-supplied. Compare against the same expected host the CSRF check
    # uses; anything cross-host or absent falls back to the index.
    referrer = request.referrer
    if referrer and urlsplit(referrer).netloc == _expected_host():
        return redirect(referrer)
    return redirect(url_for("index"))


@app.errorhandler(OverflowError)
def handle_overflow(error):
    # Belt-and-suspenders for issue #46: an oversized integer that slips past
    # the bounded path converter (e.g. an int form field bound into a query)
    # raises OverflowError. Treat it as a not-found/bad-request rather than 500.
    abort(404)


def resolve_db_path(staging, env):
    if staging:
        staging_path = env.get("STAGING_DB_PATH")
        if not staging_path:
            raise SystemExit(
                "--staging was passed but STAGING_DB_PATH is not set. "
                "Add STAGING_DB_PATH=/path/to/staging.db to your .env file."
            )
        return staging_path
    return env.get("DB_PATH")


@app.template_filter("format_line")
def format_line(value):
    """Format a line value with a sign: +0, +3, -5."""
    n = int(value)
    return f"+{n}" if n >= 0 else str(n)


@app.template_filter("edition_label")
def edition_label_filter(value):
    """Render a stored edition string (e.g. 'wii') as a display label."""
    return edition_label(value)


def get_in_progress_cup():
    conn = get_connection()
    cup = conn.execute(
        "SELECT id FROM cups WHERE status = 'in_progress' LIMIT 1"
    ).fetchone()
    conn.close()
    return cup


@app.route("/")
def index():
    in_progress = get_in_progress_cup()
    return render_template("index.html", in_progress_cup=in_progress)


def parse_default_character(form, field, edition):
    """Return the submitted default character for an edition, or None if blank.

    Raises InvalidInput if the value isn't in the edition's roster — the picker
    is a <select>, so anything else is a crafted POST.
    """
    value = (form.get(field) or "").strip()
    if not value:
        return None
    if value not in characters_for(edition):
        raise InvalidInput("Unknown character selection.")
    return value


@app.route("/players")
def players():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, default_cup, line, has_line FROM players ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template(
        "players.html",
        players=rows,
        wii_characters=characters_for("wii"),
        switch_characters=characters_for("mk8dx"),
    )


@app.route("/players", methods=["POST"])
def create_player():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name cannot be empty.")
        return redirect(url_for("players"))
    default_cup = request.form.get("default_cup") == "on"
    has_line = request.form.get("has_line") == "on"
    try:
        character_wii = parse_default_character(request.form, "default_character_wii", "wii")
        character_switch = parse_default_character(
            request.form, "default_character_switch", "mk8dx"
        )
    except InvalidInput as e:
        flash(str(e))
        return redirect(url_for("players"))
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO players (name, default_cup, has_line, default_character_wii, default_character_switch) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, default_cup, has_line, character_wii, character_switch),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        flash(f"A player named \"{name}\" already exists.")
    finally:
        conn.close()
    return redirect(url_for("players"))


@app.route("/players/<int:player_id>/edit")
def edit_player(player_id):
    conn = get_connection()
    player = conn.execute(
        "SELECT id, name, default_cup, line, has_line, default_character_wii, default_character_switch "
        "FROM players WHERE id = ?",
        (player_id,),
    ).fetchone()
    conn.close()
    if player is None:
        abort(404)
    return render_template(
        "player_edit.html",
        player=player,
        wii_characters=characters_for("wii"),
        switch_characters=characters_for("mk8dx"),
    )


@app.route("/players/<int:player_id>/edit", methods=["POST"])
def update_player(player_id):
    conn = get_connection()
    player = conn.execute(
        "SELECT id FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    if player is None:
        conn.close()
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        conn.close()
        flash("Name cannot be empty.")
        return redirect(url_for("edit_player", player_id=player_id))
    default_cup = request.form.get("default_cup") == "on"
    has_line = request.form.get("has_line") == "on"
    line = request.form.get("line", "0").strip()
    try:
        line = parse_int_field(line)
    except ValueError:
        conn.close()
        flash("Line must be a number.")
        return redirect(url_for("edit_player", player_id=player_id))
    if not has_line:
        line = 0
    try:
        character_wii = parse_default_character(request.form, "default_character_wii", "wii")
        character_switch = parse_default_character(
            request.form, "default_character_switch", "mk8dx"
        )
    except InvalidInput as e:
        conn.close()
        flash(str(e))
        return redirect(url_for("edit_player", player_id=player_id))
    try:
        conn.execute(
            "UPDATE players SET name = ?, default_cup = ?, line = ?, has_line = ?, "
            "default_character_wii = ?, default_character_switch = ? WHERE id = ?",
            (name, default_cup, line, has_line, character_wii, character_switch, player_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        flash(f"A player named \"{name}\" already exists.")
        return redirect(url_for("edit_player", player_id=player_id))
    finally:
        conn.close()
    return redirect(url_for("players"))


@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id):
    conn = get_connection()
    has_scores = conn.execute(
        "SELECT 1 FROM scores s JOIN cups c ON s.cup_id = c.id "
        "WHERE s.player_id = ? AND c.deleted_at IS NULL LIMIT 1",
        (player_id,),
    ).fetchone()
    if has_scores:
        conn.close()
        flash("Cannot delete a player who has scores recorded.")
        return redirect(url_for("players"))
    # A player attached to a cup (including an in-progress session) has a
    # cup_players row. With foreign_keys=ON and no CASCADE, deleting the player
    # would raise IntegrityError -> uncaught 500. Reject cleanly instead.
    in_cup = conn.execute(
        "SELECT 1 FROM cup_players WHERE player_id = ? LIMIT 1", (player_id,)
    ).fetchone()
    if in_cup:
        conn.close()
        flash("Cannot delete a player who is in a cup.")
        return redirect(url_for("players"))
    try:
        conn.execute("DELETE FROM line_changes WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM scores WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        flash("Cannot delete a player who is referenced elsewhere.")
    finally:
        conn.close()
    return redirect(url_for("players"))


@app.route("/cups")
def cups():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, date, notes, game_edition FROM cups WHERE deleted_at IS NULL AND status = 'completed' ORDER BY date DESC"
    ).fetchall()
    cup_ids = [r["id"] for r in rows]
    results = {}
    if cup_ids:
        placeholders = ",".join("?" * len(cup_ids))
        score_rows = conn.execute(
            f"SELECT s.cup_id, s.score, s.line_score, s.won_tiebreaker, p.name "
            f"FROM scores s JOIN players p ON s.player_id = p.id "
            f"WHERE s.cup_id IN ({placeholders}) "
            f"ORDER BY s.line_score DESC, p.name",
            cup_ids,
        ).fetchall()
        for s in score_rows:
            results.setdefault(s["cup_id"], []).append(s)
        lc_rows = conn.execute(
            f"SELECT lc.cup_id, lc.line_before, lc.line_after, p.name "
            f"FROM line_changes lc JOIN players p ON lc.player_id = p.id "
            f"WHERE lc.cup_id IN ({placeholders}) "
            f"ORDER BY p.name",
            cup_ids,
        ).fetchall()
        photo_cup_ids = {
            r["cup_id"]
            for r in conn.execute(
                f"SELECT DISTINCT cup_id FROM cup_photos WHERE cup_id IN ({placeholders})",
                cup_ids,
            ).fetchall()
        }
    else:
        lc_rows = []
        photo_cup_ids = set()
    line_changes = {}
    for lc in lc_rows:
        line_changes.setdefault(lc["cup_id"], []).append(lc)
    conn.close()
    return render_template(
        "cups.html",
        cups=rows,
        results=results,
        line_changes=line_changes,
        photo_cup_ids=photo_cup_ids,
    )


@app.route("/cups", methods=["POST"])
def create_cup():
    date_str = request.form.get("date", "").strip()
    notes = request.form.get("notes", "").strip() or None
    tz_offset = request.form.get("tz_offset", "")

    if date_str:
        try:
            local_dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
            if tz_offset:
                offset_minutes = int(tz_offset)
                utc_dt = local_dt + timedelta(minutes=offset_minutes)
            else:
                utc_dt = local_dt
            date_utc = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError):
            flash("Invalid date format.")
            return redirect(url_for("cups"))
    else:
        # Second precision (not :00) so same-minute auto-dated cups don't collide
        # on the cups.date UNIQUE constraint (issue #32).
        date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        scores_data = parse_scores_from_form(request.form)
    except InvalidInput as e:
        flash(str(e))
        return redirect(url_for("new_cup"))
    if not scores_data:
        flash("A cup must have at least one player with a score.")
        return redirect(url_for("new_cup"))

    for s in scores_data:
        s["line_score"] = s["score"] + s["line"]
    lines_by_id = {s["player_id"]: s["line"] for s in scores_data}
    error = validate_scores(scores_data, lines_by_id)
    if error:
        flash(error)
        return redirect(url_for("new_cup"))

    try:
        photo = parse_photo_from_form(request.form)
    except InvalidInput as e:
        flash(str(e))
        return redirect(url_for("new_cup"))

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO cups (date, notes) VALUES (?, ?)", (date_utc, notes)
        )
        cup_id = cursor.lastrowid
        save_scores(conn, cup_id, scores_data)
        changes = apply_line_adjustments(conn, cup_id, scores_data)
        if photo:
            save_cup_photo(conn, cup_id, photo)
        conn.commit()
        if photo:
            # Confirm the photo made it — the attach is async client-side, so
            # an explicit "photo saved" closes the loop on silent drops.
            flash("Cup recorded — photo saved.", "success")
        if changes:
            player_names = {
                r["id"]: r["name"]
                for r in conn.execute("SELECT id, name FROM players").fetchall()
            }
            parts = []
            for c in changes:
                if c["line_before"] != c["line_after"]:
                    name = player_names[c["player_id"]]
                    parts.append(
                        f"{name}: {format_line(c['line_before'])} → {format_line(c['line_after'])}"
                    )
            if parts:
                flash("Lines adjusted: " + ", ".join(parts), "info")
    except sqlite3.IntegrityError:
        flash("A cup already exists at that time.")
    finally:
        conn.close()
    return redirect(url_for("cups"))


@app.route("/cups/<int:cup_id>/edit")
def edit_cup(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id, date, notes, game_edition FROM cups WHERE id = ? AND deleted_at IS NULL",
        (cup_id,),
    ).fetchone()
    if cup is None:
        conn.close()
        abort(404)
    existing_scores = conn.execute(
        "SELECT s.player_id, s.score, s.line AS score_line, s.won_tiebreaker, p.name, p.line, p.has_line "
        "FROM scores s JOIN players p ON s.player_id = p.id "
        "WHERE s.cup_id = ? ORDER BY p.name",
        (cup_id,),
    ).fetchall()
    all_players = conn.execute(
        "SELECT id, name, line, has_line FROM players ORDER BY name"
    ).fetchall()
    has_photo = (
        conn.execute(
            "SELECT 1 FROM cup_photos WHERE cup_id = ? LIMIT 1", (cup_id,)
        ).fetchone()
        is not None
    )
    conn.close()
    scores_by_player = {s["player_id"]: s for s in existing_scores}
    cup_players = [{"id": s["player_id"], "name": s["name"], "line": s["line"], "has_line": s["has_line"]} for s in existing_scores]
    lines_by_id = {p["id"]: p["line"] for p in all_players}
    return render_template(
        "cup_edit.html",
        cup=cup,
        players=cup_players,
        all_players=all_players,
        scores_by_player=scores_by_player,
        lines_by_id=lines_by_id,
        has_photo=has_photo,
    )


@app.route("/cups/<int:cup_id>/edit", methods=["POST"])
def update_cup(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id FROM cups WHERE id = ? AND deleted_at IS NULL", (cup_id,)
    ).fetchone()
    if cup is None:
        conn.close()
        abort(404)

    date_str = request.form.get("date", "").strip()
    notes = request.form.get("notes", "").strip() or None
    tz_offset = request.form.get("tz_offset", "")

    if not date_str:
        conn.close()
        flash("Date cannot be empty.")
        return redirect(url_for("edit_cup", cup_id=cup_id))

    try:
        local_dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
        if tz_offset:
            offset_minutes = int(tz_offset)
            utc_dt = local_dt + timedelta(minutes=offset_minutes)
        else:
            utc_dt = local_dt
        date_utc = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError):
        conn.close()
        flash("Invalid date format.")
        return redirect(url_for("edit_cup", cup_id=cup_id))

    try:
        scores_data = parse_scores_from_form(request.form)
    except InvalidInput as e:
        conn.close()
        flash(str(e))
        return redirect(url_for("edit_cup", cup_id=cup_id))
    if scores_data:
        # Switch (mk8dx) cups are lineless — drop any submitted line (incl. from
        # the add-player path) before validation or storage.
        zero_lines_if_lineless(conn, cup_id, scores_data)
        for s in scores_data:
            s["line_score"] = s["score"] + s["line"]
        lines_by_id = {s["player_id"]: s["line"] for s in scores_data}
        error = validate_scores(scores_data, lines_by_id)
        if error:
            conn.close()
            flash(error)
            return redirect(url_for("edit_cup", cup_id=cup_id))

    try:
        conn.execute(
            "UPDATE cups SET date = ?, notes = ? WHERE id = ?",
            (date_utc, notes, cup_id),
        )
        save_scores(conn, cup_id, scores_data)
        conn.commit()
    except sqlite3.IntegrityError:
        flash("A cup already exists at that time.")
        return redirect(url_for("edit_cup", cup_id=cup_id))
    finally:
        conn.close()
    return redirect(url_for("cups"))


@app.route("/cups/<int:cup_id>/delete", methods=["POST"])
def delete_cup(cup_id):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute(
        "UPDATE cups SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (now_utc, cup_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("cups"))


def validate_scores(scores_data, lines_by_id=None):
    """Validate tiebreaker rules for a set of scores.

    scores_data is a list of dicts with keys: player_id, score, won_tiebreaker.
    lines_by_id is an optional dict of player_id -> line value. When provided,
    ties are checked against line-adjusted scores (raw + line) instead of raw scores.
    Returns an error message string or None.
    """
    def effective_score(s):
        if lines_by_id is not None:
            return s["score"] + lines_by_id.get(s["player_id"], 0)
        return s["score"]

    score_counts = Counter(effective_score(s) for s in scores_data)
    tiebreaker_winners = [s for s in scores_data if s["won_tiebreaker"]]
    # Each winner must be in a tie group
    for w in tiebreaker_winners:
        if score_counts[effective_score(w)] < 2:
            return "Tiebreaker winner must share their score with at least one other player."
    # At most one winner per score value
    winner_scores = Counter(effective_score(w) for w in tiebreaker_winners)
    for score, count in winner_scores.items():
        if count > 1:
            return "Only one player can win the tiebreaker per tie group."
    return None


# SQLite stores INTEGER as a signed 64-bit value. Anything outside this range
# raises OverflowError when bound as a query parameter, so we reject it up front.
SQLITE_MIN_INT = -(2**63)
SQLITE_MAX_INT = 2**63 - 1


class InvalidInput(Exception):
    """Raised when hostile/malformed form input can't be parsed into valid data.

    Views catch this and turn it into a flash + redirect (a clean 4xx-style
    response) instead of letting a ValueError/OverflowError bubble up as a 500.
    """


def parse_int_field(raw):
    """Parse a form value into an int that fits SQLite's signed 64-bit range.

    Raises ValueError on non-numeric input (from int()) or on a value outside
    the range SQLite's INTEGER column can hold (which would otherwise raise
    OverflowError only later, mid-transaction, at parameter-binding time).
    """
    value = int(raw)
    if not (SQLITE_MIN_INT <= value <= SQLITE_MAX_INT):
        raise ValueError("integer out of range for SQLite INTEGER")
    return value


def checked_line_score(score, line):
    """Return score + line, validated to fit SQLite's signed 64-bit range.

    parse_int_field bounds each operand individually, but the value actually
    bound to the DB is their SUM (line_score). Two in-range operands can still
    add up past ±2^63, which would raise OverflowError at bind time (a 500).
    Reject up front with InvalidInput so callers turn it into a clean 4xx.
    """
    total = score + line
    if not (SQLITE_MIN_INT <= total <= SQLITE_MAX_INT):
        raise InvalidInput("Score plus line adjustment is out of range.")
    return total


# A cup has only a handful of players; anything beyond this is an abusive /
# malformed submission, not a real one. Cap the parsed list length so a caller
# can't force us to iterate/parse an enormous number of rows.
MAX_SCORE_ROWS = 50


def parse_scores_from_form(form):
    """Extract score data from form submission.

    Expects form fields: player_ids[], scores[], lines[] (optional), tiebreakers[] (list of player_ids).
    Returns list of dicts with keys: player_id, score, line, won_tiebreaker.
    Skips players with empty score fields.

    Raises InvalidInput if a player_id or score is non-numeric or out of the
    SQLite INTEGER range, so callers can reject the request cleanly.
    """
    player_ids = form.getlist("player_ids[]")
    raw_scores = form.getlist("scores[]")
    lines = form.getlist("lines[]")
    tiebreaker_ids = set(form.getlist("tiebreakers[]"))
    # Reject absurdly long lists up front (DoS flank) — a real cup has a
    # handful of players, never dozens.
    if (
        len(player_ids) > MAX_SCORE_ROWS
        or len(raw_scores) > MAX_SCORE_ROWS
        or len(lines) > MAX_SCORE_ROWS
    ):
        raise InvalidInput("Too many players/scores submitted.")
    # player_ids[] and scores[] are paired positionally (zip). If their counts
    # differ, the pairing is ambiguous and would silently misattribute scores to
    # the wrong player (issue #45 — e.g. a removed middle player whose hidden
    # player_ids[] input still submitted while its scores[] input did not).
    # Reject rather than guess.
    if len(player_ids) != len(raw_scores):
        raise InvalidInput("Player and score fields are misaligned.")
    scores_data = []
    for i, (pid, raw) in enumerate(zip(player_ids, raw_scores)):
        raw = raw.strip()
        if raw == "":
            continue
        line_val = 0
        if i < len(lines) and lines[i].strip() != "":
            # Lenient: an unparseable / out-of-range line is treated as no line.
            try:
                line_val = parse_int_field(lines[i].strip())
            except ValueError:
                line_val = 0
        try:
            player_id = parse_int_field(pid)
            score = parse_int_field(raw)
        except ValueError:
            raise InvalidInput("Player IDs and scores must be valid whole numbers.")
        # Validate the SUM (score + line) up front; it's what gets bound to the
        # line_score column and can overflow even when both operands are in range.
        checked_line_score(score, line_val)
        scores_data.append({
            "player_id": player_id,
            "score": score,
            "line": line_val,
            "won_tiebreaker": str(pid) in tiebreaker_ids,
        })
    return scores_data


def save_scores(conn, cup_id, scores_data):
    """Insert or replace scores for a cup."""
    conn.execute("DELETE FROM scores WHERE cup_id = ?", (cup_id,))
    for s in scores_data:
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line, line_score, won_tiebreaker) VALUES (?, ?, ?, ?, ?, ?)",
            (cup_id, s["player_id"], s["score"], s["line"], s["line_score"], s["won_tiebreaker"] or None),
        )


def calculate_placements(scores_with_lines):
    """Calculate placements from line-adjusted scores.

    scores_with_lines: list of dicts with keys: player_id, score, line, won_tiebreaker.
    Returns the list sorted by placement, with line_score and placement added to each dict.
    """
    for s in scores_with_lines:
        s["line_score"] = s["score"] + s["line"]

    sorted_scores = sorted(
        scores_with_lines,
        key=lambda s: (-s["line_score"], -(1 if s["won_tiebreaker"] else 0)),
    )

    for i, s in enumerate(sorted_scores):
        if i == 0:
            s["placement"] = 1
        elif s["line_score"] != sorted_scores[i - 1]["line_score"]:
            s["placement"] = i + 1
        elif sorted_scores[i - 1]["won_tiebreaker"] and not s["won_tiebreaker"]:
            s["placement"] = i + 1
        else:
            s["placement"] = sorted_scores[i - 1]["placement"]

    return sorted_scores


def cup_uses_lines(conn, cup_id):
    """Whether a cup uses the line handicap. Lines are a Wii-only mechanic;
    Switch (mk8dx) — and any non-Wii edition — cups are lineless."""
    row = conn.execute(
        "SELECT game_edition FROM cups WHERE id = ?", (cup_id,)
    ).fetchone()
    return row is not None and row["game_edition"] == "wii"


def zero_lines_if_lineless(conn, cup_id, scores_data):
    """For a lineless (non-Wii) cup, force every score's line to 0 so line_score
    equals the raw score. This is the authoritative server-side guard behind the
    hidden line UI: a crafted POST carrying non-zero lines[] on a Switch cup must
    not persist a handicap in scores.line / line_score."""
    if cup_uses_lines(conn, cup_id):
        return
    for s in scores_data:
        s["line"] = 0
        s["line_score"] = s["score"]


def apply_line_adjustments(conn, cup_id, scores_data):
    """Apply line adjustments for a 3-player Wii cup.

    Only applies if exactly 3 players AND the cup is a Wii cup. Lines are a
    Wii-only mechanic — Switch (mk8dx) cups stay lineless, so they never create
    line_changes or adjust players.line. Returns list of changes for display, or
    empty list if no adjustments were made.
    """
    if len(scores_data) != 3:
        return []

    # Lines apply to Wii only; Switch cups are lineless.
    if not cup_uses_lines(conn, cup_id):
        return []

    # Fetch has_line flag and current player line for each player
    player_ids = [s["player_id"] for s in scores_data]
    placeholders = ",".join("?" * len(player_ids))
    rows = conn.execute(
        f"SELECT id, line, has_line FROM players WHERE id IN ({placeholders})", player_ids
    ).fetchall()
    player_line_by_id = {r["id"]: r["line"] for r in rows}
    has_line_by_id = {r["id"]: r["has_line"] for r in rows}

    # Use the per-score line (from the form) for placement calculation
    scores_with_lines = [
        {
            "player_id": s["player_id"],
            "score": s["score"],
            "line": s["line"],
            "won_tiebreaker": s["won_tiebreaker"],
        }
        for s in scores_data
    ]

    placements = calculate_placements(scores_with_lines)

    # Skip adjustments if any unresolved ties exist
    placement_counts = Counter(s["placement"] for s in placements)
    if any(count > 1 for count in placement_counts.values()):
        return []

    changes = []
    for s in placements:
        # Only adjust lines for players who play with a line
        if not has_line_by_id[s["player_id"]]:
            continue

        if s["placement"] == 1:
            delta = -3
        elif s["placement"] == 2:
            delta = 0
        else:
            delta = 3

        line_before = player_line_by_id[s["player_id"]]
        line_after = line_before + delta
        conn.execute("UPDATE players SET line = ? WHERE id = ?", (line_after, s["player_id"]))
        conn.execute(
            "INSERT INTO line_changes (cup_id, player_id, line_before, line_after) VALUES (?, ?, ?, ?)",
            (cup_id, s["player_id"], line_before, line_after),
        )
        changes.append({
            "player_id": s["player_id"],
            "line_before": line_before,
            "line_after": line_after,
        })

    return changes


# --- Cup create with scores ---


@app.route("/cups/new")
def new_cup():
    conn = get_connection()
    default_players = conn.execute(
        "SELECT id, name, line, has_line FROM players WHERE default_cup = 1 ORDER BY name"
    ).fetchall()
    all_players = conn.execute(
        "SELECT id, name, line, has_line FROM players ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template(
        "cup_new.html", players=default_players, all_players=all_players
    )


# --- Cup edit with scores (extended) ---
# edit_cup and update_cup are above; we replace them here to add score handling.


# --- Standalone score routes ---


@app.route("/scores")
def scores():
    conn = get_connection()
    rows = conn.execute(
        "SELECT s.id, s.score, s.line_score, s.won_tiebreaker, p.name AS player_name, c.date AS cup_date "
        "FROM scores s "
        "JOIN players p ON s.player_id = p.id "
        "JOIN cups c ON s.cup_id = c.id "
        "ORDER BY c.date DESC, p.name"
    ).fetchall()
    conn.close()
    return render_template("scores.html", scores=rows)


@app.route("/scores", methods=["POST"])
def create_score():
    cup_id = request.form.get("cup_id", "").strip()
    player_id = request.form.get("player_id", "").strip()
    score = request.form.get("score", "").strip()
    won_tiebreaker = request.form.get("won_tiebreaker") == "on"

    if not cup_id or not player_id or not score:
        flash("Cup, player, and score are required.")
        return redirect(url_for("scores"))

    try:
        cup_id = parse_int_field(cup_id)
        player_id = parse_int_field(player_id)
        score = parse_int_field(score)
    except ValueError:
        flash("Cup, player, and score must be valid whole numbers.")
        return redirect(url_for("scores"))

    conn = get_connection()
    try:
        # Guard the target cup's state (issue #40): a standalone score may only
        # be written to a cup that is actively in progress and not soft-deleted.
        # Writing into a completed/cancelled cup corrupts its finalized standings
        # (placements/lines are computed at completion, not on ad-hoc inserts),
        # and a soft-deleted cup should accept nothing. Reject cleanly, not 500.
        cup = conn.execute(
            "SELECT status, deleted_at FROM cups WHERE id = ?", (cup_id,)
        ).fetchone()
        if cup is None or cup["deleted_at"] is not None or cup["status"] != "in_progress":
            flash("Scores can only be added to a cup that is currently in progress.")
            return redirect(url_for("scores"))
        player = conn.execute(
            "SELECT line FROM players WHERE id = ?", (player_id,)
        ).fetchone()
        player_line = player["line"] if player else 0
        try:
            line_score_val = checked_line_score(score, player_line)
        except InvalidInput as e:
            flash(str(e))
            return redirect(url_for("scores"))
        # Fold the in-progress guard into the INSERT (issue #52) to close the
        # TOCTOU between the SELECT above and this write: if the cup is
        # completed/cancelled/deleted in that window, EXISTS is false and no
        # row is inserted. The SELECT above stays for the friendly common-path
        # message; this conditional INSERT is the authoritative guard.
        cursor = conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line, line_score, won_tiebreaker) "
            "SELECT ?, ?, ?, ?, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM cups WHERE id = ? AND deleted_at IS NULL AND status = 'in_progress')",
            (cup_id, player_id, score, player_line, line_score_val, won_tiebreaker or None, cup_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            flash("Scores can only be added to a cup that is currently in progress.")
            return redirect(url_for("scores"))
        conn.commit()
    except sqlite3.IntegrityError:
        flash("A score for that player in that cup already exists.")
    finally:
        conn.close()
    return redirect(url_for("scores"))


@app.route("/scores/<int:score_id>/edit")
def edit_score(score_id):
    conn = get_connection()
    score = conn.execute(
        "SELECT id, cup_id, player_id, score, won_tiebreaker FROM scores WHERE id = ?",
        (score_id,),
    ).fetchone()
    conn.close()
    if score is None:
        abort(404)
    return render_template("score_edit.html", score=score)


@app.route("/scores/<int:score_id>/edit", methods=["POST"])
def update_score(score_id):
    conn = get_connection()
    existing = conn.execute(
        "SELECT id, player_id FROM scores WHERE id = ?", (score_id,)
    ).fetchone()
    if existing is None:
        conn.close()
        abort(404)

    score_val = request.form.get("score", "").strip()
    won_tiebreaker = request.form.get("won_tiebreaker") == "on"

    if not score_val:
        conn.close()
        flash("Score cannot be empty.")
        return redirect(url_for("edit_score", score_id=score_id))

    try:
        score_int = parse_int_field(score_val)
    except ValueError:
        conn.close()
        flash("Score must be a valid whole number.")
        return redirect(url_for("edit_score", score_id=score_id))

    try:
        player = conn.execute(
            "SELECT line FROM players WHERE id = ?", (existing["player_id"],)
        ).fetchone()
        player_line = player["line"] if player else 0
        try:
            line_score_val = checked_line_score(score_int, player_line)
        except InvalidInput as e:
            flash(str(e))
            return redirect(url_for("edit_score", score_id=score_id))
        conn.execute(
            "UPDATE scores SET score = ?, line = ?, line_score = ?, won_tiebreaker = ? WHERE id = ?",
            (score_int, player_line, line_score_val, won_tiebreaker or None, score_id),
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("scores"))


@app.route("/scores/<int:score_id>/delete", methods=["POST"])
def delete_score(score_id):
    conn = get_connection()
    conn.execute("DELETE FROM scores WHERE id = ?", (score_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("scores"))


# --- Cup Session routes ---

MAX_RACES = 4
MAX_HALF_VETOES = 3
MAX_VOTOES = 4
# "Use it or lose it": a one-time check applied when entering this race. A player
# who still holds ALL their half-vetoes at this point forfeits one.
STALE_VETO_CHECK_RACE = 3
# Minimum roster size for an in-progress cup. Mirrors cup creation, which requires
# "at least one player" (cup_session_create rejects an empty player_ids[]), so a
# mid-cup removal must never drop the roster below this — a cup can never be
# emptied of players.
MIN_ROSTER_SIZE = 1


def increment_voto(conn, cup_id):
    """Atomically bump a cup's shared voto counter if it's below the cap.

    The cap check lives INSIDE the UPDATE's WHERE clause (issue #36), so a
    read-check-write race can't push the counter past MAX_VOTOES: concurrent
    callers serialize on the write, and only the ones that still see
    voto_count < cap actually increment. Returns the new count, or None if the
    increment was rejected (cup not in progress, or already at the cap). Does
    not commit — the caller owns the transaction.
    """
    result = conn.execute(
        "UPDATE cups SET voto_count = voto_count + 1 "
        "WHERE id = ? AND status = 'in_progress' AND voto_count < ?",
        (cup_id, MAX_VOTOES),
    )
    if result.rowcount == 0:
        return None
    return conn.execute(
        "SELECT voto_count FROM cups WHERE id = ?", (cup_id,)
    ).fetchone()["voto_count"]


def increment_half_veto(conn, cup_id, player_id):
    """Atomically bump a player's half-veto counter if it's below the cap.

    Same atomic cap guard as increment_voto (issue #36): the cap check is in the
    UPDATE's WHERE clause so concurrent increments can't exceed MAX_HALF_VETOES.
    Returns the new count, or None if rejected (player not in this cup, or
    already at the cap). Does not commit — the caller owns the transaction.
    """
    result = conn.execute(
        "UPDATE cup_players SET half_veto_count = half_veto_count + 1 "
        "WHERE cup_id = ? AND player_id = ? AND half_veto_count < ?",
        (cup_id, player_id, MAX_HALF_VETOES),
    )
    if result.rowcount == 0:
        return None
    return conn.execute(
        "SELECT half_veto_count FROM cup_players WHERE cup_id = ? AND player_id = ?",
        (cup_id, player_id),
    ).fetchone()["half_veto_count"]


def apply_stale_veto_forfeit(conn, cup_id):
    """One-time half-veto forfeit when entering race STALE_VETO_CHECK_RACE.

    Each player still holding all their half-vetoes (half_veto_count == 0, i.e.
    used none in the earlier races) forfeits one. Players who have used at least
    one are untouched, and votoes are never affected. Commits and returns the
    names of players who forfeited (for a flash message), or []. Idempotent: a
    player already at count >= 1 is skipped, so re-running never forfeits twice.
    """
    rows = conn.execute(
        "SELECT p.name FROM cup_players cp JOIN players p ON cp.player_id = p.id "
        "WHERE cp.cup_id = ? AND cp.half_veto_count = 0 ORDER BY p.name",
        (cup_id,),
    ).fetchall()
    if not rows:
        return []
    conn.execute(
        "UPDATE cup_players SET half_veto_count = half_veto_count + 1 "
        "WHERE cup_id = ? AND half_veto_count = 0",
        (cup_id,),
    )
    conn.commit()
    return [r["name"] for r in rows]


@app.route("/cup-session/new")
def cup_session_new():
    in_progress = get_in_progress_cup()
    if in_progress:
        return redirect(url_for("cup_session_race", cup_id=in_progress["id"]))
    conn = get_connection()
    default_players = conn.execute(
        "SELECT id, name, line, has_line FROM players WHERE default_cup = 1 ORDER BY name"
    ).fetchall()
    all_players = conn.execute(
        "SELECT id, name, line, has_line FROM players ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template(
        "cup_session_new.html",
        players=default_players,
        all_players=all_players,
        editions=EDITION_LABELS,
        default_edition=DEFAULT_EDITION,
    )


@app.route("/cup-session/new", methods=["POST"])
def cup_session_create():
    in_progress = get_in_progress_cup()
    if in_progress:
        flash("A cup is already in progress.")
        return redirect(url_for("cup_session_race", cup_id=in_progress["id"]))

    player_ids = request.form.getlist("player_ids[]")
    if not player_ids:
        flash("Select at least one player.")
        return redirect(url_for("cup_session_new"))

    # Validate player ids BEFORE opening a write transaction. Doing the int()
    # parse inside the transaction (after the cup INSERT) meant a bad id threw
    # mid-transaction and leaked an open, uncommitted connection -> later writes
    # on the worker hit "database is locked".
    try:
        player_id_ints = [parse_int_field(pid) for pid in player_ids]
    except ValueError:
        flash("Invalid player selection.")
        return redirect(url_for("cup_session_new"))

    edition = request.form.get("game_edition", DEFAULT_EDITION)
    if edition not in TRACK_SETS:
        edition = DEFAULT_EDITION

    # Use second precision (not :00) so two sessions started in the same minute
    # don't collide on the cups.date UNIQUE constraint (issue #32).
    date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        # Atomic guard against a concurrent double-start (issue #55). The early
        # get_in_progress_cup() check above handles the friendly common path,
        # but two requests can both pass it before either inserts. This
        # conditional INSERT only creates the cup if no in-progress cup exists,
        # so at most one wins the race; the loser sees rowcount == 0.
        cursor = conn.execute(
            "INSERT INTO cups (date, status, game_edition) "
            "SELECT ?, 'in_progress', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM cups WHERE status = 'in_progress')",
            (date_utc, edition),
        )
        if cursor.rowcount == 0:
            # Lost the race: another request started a cup in the gap. Redirect
            # to the existing in-progress cup rather than creating a second one.
            existing = conn.execute(
                "SELECT id FROM cups WHERE status = 'in_progress' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.rollback()
            flash("A cup is already in progress.")
            if existing:
                return redirect(url_for("cup_session_race", cup_id=existing["id"]))
            return redirect(url_for("cup_session_new"))
        cup_id = cursor.lastrowid
        for pid in player_id_ints:
            conn.execute(
                "INSERT INTO cup_players (cup_id, player_id) VALUES (?, ?)",
                (cup_id, pid),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        flash("Could not create cup session.")
        return redirect(url_for("cup_session_new"))
    finally:
        conn.close()
    return redirect(url_for("cup_session_race", cup_id=cup_id))


def _get_cup_session(cup_id):
    """Fetch a cup session with its players, races, and veto state. Returns None if not found."""
    conn = get_connection()
    cup = conn.execute(
        "SELECT id, date, notes, status, voto_count, game_edition FROM cups WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    ).fetchone()
    if cup is None:
        conn.close()
        return None

    players = conn.execute(
        "SELECT cp.player_id, cp.half_veto_count, p.name, p.line, p.has_line "
        "FROM cup_players cp JOIN players p ON cp.player_id = p.id "
        "WHERE cp.cup_id = ? ORDER BY p.name",
        (cup_id,),
    ).fetchall()

    races = conn.execute(
        "SELECT race_number, map FROM races WHERE cup_id = ? ORDER BY race_number",
        (cup_id,),
    ).fetchall()

    # Players NOT already in this cup — candidates for the mid-cup "add player"
    # control. A player can only be added once (UNIQUE(cup_id, player_id)).
    available_players = conn.execute(
        "SELECT id, name FROM players "
        "WHERE id NOT IN (SELECT player_id FROM cup_players WHERE cup_id = ?) "
        "ORDER BY name",
        (cup_id,),
    ).fetchall()

    conn.close()
    return {
        "cup": cup,
        "players": players,
        "races": races,
        "available_players": available_players,
    }


@app.route("/cup-session/<int:cup_id>")
def cup_session_race(cup_id):
    session = _get_cup_session(cup_id)
    if session is None:
        abort(404)
    played_maps = [r["map"] for r in session["races"]]
    current_race = len(session["races"]) + 1
    edition = session["cup"]["game_edition"]
    return render_template(
        "cup_session_race.html",
        cup=session["cup"],
        players=session["players"],
        available_players=session["available_players"],
        races=session["races"],
        played_maps=played_maps,
        current_race=current_race,
        max_races=MAX_RACES,
        max_half_vetoes=MAX_HALF_VETOES,
        max_votoes=MAX_VOTOES,
        min_roster_size=MIN_ROSTER_SIZE,
        all_courses=courses_for(edition),
        edition_label=edition_label(edition),
    )


@app.route("/cup-session/<int:cup_id>/spin", methods=["POST"])
def cup_session_spin(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id, status, game_edition FROM cups WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    ).fetchone()
    if cup is None:
        conn.close()
        return jsonify({"error": "Cup not found or not in progress"}), 404

    races = conn.execute(
        "SELECT map FROM races WHERE cup_id = ?", (cup_id,)
    ).fetchall()
    conn.close()

    if len(races) >= MAX_RACES:
        return jsonify({"error": "All races completed"}), 400

    courses = courses_for(cup["game_edition"])
    played_maps = {r["map"] for r in races}
    valid = [c for c in courses if c not in played_maps]
    if not valid:
        return jsonify({"error": "No valid maps remaining"}), 400

    chosen = random.choice(valid)
    return jsonify({"map": chosen, "index": courses.index(chosen)})


@app.route("/cup-session/<int:cup_id>/half-veto", methods=["POST"])
def cup_session_half_veto(cup_id):
    data = request.get_json()
    player_id = data.get("player_id") if data else None

    conn = get_connection()
    try:
        cup = conn.execute(
            "SELECT id FROM cups WHERE id = ? AND status = 'in_progress'", (cup_id,)
        ).fetchone()
        if cup is None:
            return jsonify({"error": "Cup not found"}), 404

        if player_id is not None:
            # Only a scalar can be bound as a SQL parameter; a JSON list/dict
            # would raise sqlite3.InterfaceError. An out-of-range or non-numeric
            # int/str would raise OverflowError/ValueError at bind time — so
            # validate TYPE (reject bool/non-scalar) and RANGE (parse_int_field)
            # up front and turn either failure into a clean 400.
            if isinstance(player_id, bool) or not isinstance(player_id, (int, str)):
                return jsonify({"error": "Invalid player_id"}), 400
            try:
                player_id = parse_int_field(player_id)
            except ValueError:
                return jsonify({"error": "Invalid player_id"}), 400
            cp = conn.execute(
                "SELECT half_veto_count FROM cup_players WHERE cup_id = ? AND player_id = ?",
                (cup_id, player_id),
            ).fetchone()
            if cp is None:
                return jsonify({"error": "Player not in this cup"}), 400
            # Atomic cap-guarded increment (issue #36): returns None if already
            # at the cap (or lost a concurrent race), so the counter can never
            # exceed MAX_HALF_VETOES.
            new_count = increment_half_veto(conn, cup_id, player_id)
            if new_count is None:
                return jsonify({"error": "No half vetoes remaining"}), 400
            conn.commit()
            remaining = MAX_HALF_VETOES - new_count
        else:
            remaining = None
    finally:
        conn.close()

    success = random.choice([True, False])
    return jsonify({"success": success, "remaining": remaining})


@app.route("/cup-session/<int:cup_id>/voto", methods=["POST"])
def cup_session_voto(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id FROM cups WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    ).fetchone()
    if cup is None:
        conn.close()
        return jsonify({"error": "Cup not found"}), 404

    # Atomic cap-guarded increment (issue #36): the cap check is inside the
    # UPDATE, so concurrent votoes can never push voto_count past MAX_VOTOES.
    new_count = increment_voto(conn, cup_id)
    if new_count is None:
        conn.close()
        return jsonify({"error": "No votoes remaining"}), 400
    conn.commit()
    remaining = MAX_VOTOES - new_count
    conn.close()
    return jsonify({"remaining": remaining})


@app.route("/cup-session/<int:cup_id>/next-race", methods=["POST"])
def cup_session_next_race(cup_id):
    data = request.get_json()
    map_name = data.get("map") if data else None
    # Must be a non-empty string: a non-scalar (list/dict) would raise
    # sqlite3.InterfaceError when bound as a parameter.
    if not isinstance(map_name, str) or not map_name.strip():
        return jsonify({"error": "Map name required"}), 400

    conn = get_connection()
    cup = conn.execute(
        "SELECT id, game_edition FROM cups WHERE id = ? AND status = 'in_progress'", (cup_id,)
    ).fetchone()
    if cup is None:
        conn.close()
        return jsonify({"error": "Cup not found"}), 404

    # Validate the course against this cup's edition (issue #41). Without this an
    # arbitrary or off-edition course name gets persisted into races/history,
    # polluting stats. Only names in the edition's track set are accepted.
    if map_name not in courses_for(cup["game_edition"]):
        conn.close()
        return jsonify({"error": "Invalid course for this edition"}), 400

    race_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM races WHERE cup_id = ?", (cup_id,)
    ).fetchone()["cnt"]

    if race_count >= MAX_RACES:
        conn.close()
        return jsonify({"error": "All races completed"}), 400

    race_number = race_count + 1
    try:
        conn.execute(
            "INSERT INTO races (cup_id, race_number, map) VALUES (?, ?, ?)",
            (cup_id, race_number, map_name),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Race already recorded"}), 400

    # Recording race N-1 means the player is now entering race N. When that's the
    # stale-veto check race, forfeit a half-veto from anyone who's hoarded them
    # all. The flash renders on the page reload the client does after this call.
    # try/finally so an unexpected error here can't leak the connection.
    try:
        if race_number == STALE_VETO_CHECK_RACE - 1:
            forfeited = apply_stale_veto_forfeit(conn, cup_id)
            if forfeited:
                flash(
                    "Stale veto forfeit — "
                    + ", ".join(forfeited)
                    + f" hadn't used a half-veto by race {STALE_VETO_CHECK_RACE} and lost one.",
                    "info",
                )
    finally:
        conn.close()
    complete = race_number >= MAX_RACES
    return jsonify({"race_number": race_number, "complete": complete})


@app.route("/cup-session/<int:cup_id>/complete")
def cup_session_complete(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id, date, notes, status, voto_count, game_edition FROM cups WHERE id = ?",
        (cup_id,),
    ).fetchone()
    if cup is None:
        abort(404)
    if cup["status"] not in ("in_progress", "completed"):
        abort(404)

    players = conn.execute(
        "SELECT cp.player_id, p.name, p.line, p.has_line "
        "FROM cup_players cp JOIN players p ON cp.player_id = p.id "
        "WHERE cp.cup_id = ? ORDER BY p.name",
        (cup_id,),
    ).fetchall()

    races = conn.execute(
        "SELECT race_number, map FROM races WHERE cup_id = ? ORDER BY race_number",
        (cup_id,),
    ).fetchall()

    # If already completed, load existing scores for display
    existing_scores = {}
    if cup["status"] == "completed":
        score_rows = conn.execute(
            "SELECT player_id, score, line, won_tiebreaker FROM scores WHERE cup_id = ?",
            (cup_id,),
        ).fetchall()
        existing_scores = {s["player_id"]: s for s in score_rows}

    conn.close()
    return render_template(
        "cup_session_complete.html",
        cup=cup,
        players=players,
        races=races,
        all_courses=courses_for(cup["game_edition"]),
        edition_label=edition_label(cup["game_edition"]),
        existing_scores=existing_scores,
    )


@app.route("/cup-session/<int:cup_id>/complete", methods=["POST"])
def cup_session_submit(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id, status, game_edition FROM cups WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    ).fetchone()
    if cup is None:
        conn.close()
        abort(404)

    # Update races if edited. Validate each edited map against this cup's edition
    # (issue #41 — second door): the completion form lets you override race maps,
    # and a crafted/stale POST could otherwise persist an arbitrary or off-edition
    # course name straight into history, polluting stats. Only submitted (non-empty)
    # values are checked; unchanged/empty fields are skipped as before.
    edition_courses = courses_for(cup["game_edition"])
    for i in range(1, MAX_RACES + 1):
        new_map = request.form.get(f"race_{i}")
        if new_map:
            if new_map not in edition_courses:
                conn.close()
                flash("Invalid course for this edition.")
                return redirect(url_for("cup_session_complete", cup_id=cup_id))
            conn.execute(
                "UPDATE races SET map = ? WHERE cup_id = ? AND race_number = ?",
                (new_map, cup_id, i),
            )

    notes = request.form.get("notes", "").strip() or None
    tz_offset = request.form.get("tz_offset", "")

    # Parse date or keep existing
    date_str = request.form.get("date", "").strip()
    if date_str:
        try:
            local_dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
            if tz_offset:
                offset_minutes = int(tz_offset)
                utc_dt = local_dt + timedelta(minutes=offset_minutes)
            else:
                utc_dt = local_dt
            date_utc = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError):
            date_utc = None
    else:
        date_utc = None

    try:
        scores_data = parse_scores_from_form(request.form)
    except InvalidInput as e:
        conn.close()
        flash(str(e))
        return redirect(url_for("cup_session_complete", cup_id=cup_id))
    if not scores_data:
        flash("At least one player must have a score.")
        conn.close()
        return redirect(url_for("cup_session_complete", cup_id=cup_id))

    # Switch (mk8dx) cups are lineless — drop any submitted line before it can
    # reach validation or storage.
    zero_lines_if_lineless(conn, cup_id, scores_data)
    for s in scores_data:
        s["line_score"] = s["score"] + s["line"]
    lines_by_id = {s["player_id"]: s["line"] for s in scores_data}
    error = validate_scores(scores_data, lines_by_id)
    if error:
        flash(error)
        conn.close()
        return redirect(url_for("cup_session_complete", cup_id=cup_id))

    # A malformed photo rejects the whole submit (flash + redirect) so the
    # attached photo is never silently dropped. Saving the photo does NOT
    # depend on extraction — fully manual scores with a photo work the same.
    try:
        photo = parse_photo_from_form(request.form)
    except InvalidInput as e:
        conn.close()
        flash(str(e))
        return redirect(url_for("cup_session_complete", cup_id=cup_id))

    try:
        # Complete atomically (issue #39). The status transition is guarded by
        # `WHERE status = 'in_progress'` so that under two concurrent submits
        # only ONE wins — the loser's UPDATE affects 0 rows. Scores and (the
        # non-idempotent) line adjustments are applied ONLY if we won, so lines
        # can never be shifted twice and line_changes can't be duplicated.
        if date_utc:
            result = conn.execute(
                "UPDATE cups SET notes = ?, date = ?, status = 'completed' WHERE id = ? AND status = 'in_progress'",
                (notes, date_utc, cup_id),
            )
        else:
            result = conn.execute(
                "UPDATE cups SET notes = ?, status = 'completed' WHERE id = ? AND status = 'in_progress'",
                (notes, cup_id),
            )
        if result.rowcount == 0:
            # Lost the race: another request completed/cancelled this cup after
            # our initial status check but before this write. Apply nothing.
            conn.rollback()
            conn.close()
            abort(409)
        save_scores(conn, cup_id, scores_data)
        changes = apply_line_adjustments(conn, cup_id, scores_data)
        if photo:
            save_cup_photo(conn, cup_id, photo)
        conn.commit()
        if photo:
            # Confirm the photo made it — the attach is async client-side, so
            # an explicit "photo saved" closes the loop on silent drops.
            flash("Cup recorded — photo saved.", "success")
        if changes:
            player_names = {
                r["id"]: r["name"]
                for r in conn.execute("SELECT id, name FROM players").fetchall()
            }
            parts = []
            for c in changes:
                if c["line_before"] != c["line_after"]:
                    name = player_names[c["player_id"]]
                    parts.append(
                        f"{name}: {format_line(c['line_before'])} → {format_line(c['line_after'])}"
                    )
            if parts:
                flash("Lines adjusted: " + ", ".join(parts), "info")
    except sqlite3.IntegrityError:
        flash("Could not save cup.")
    finally:
        conn.close()
    return redirect(url_for("cups"))


@app.route("/cup-session/<int:cup_id>/cancel", methods=["POST"])
def cup_session_cancel(cup_id):
    conn = get_connection()
    conn.execute(
        "UPDATE cups SET status = 'cancelled' WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


# --- Mid-cup roster editing (add / remove a player on an in-progress cup) ---
#
# The roster is otherwise fixed at cup creation. These two routes let the user
# adjust it while a cup is in progress. No scores exist mid-cup (scores are only
# written at completion), so there is nothing to reconcile here — only the
# cup_players join table changes. Both guards mirror create_score / PR #58:
# the cup must be status='in_progress' AND deleted_at IS NULL, verified inside
# the write itself so a completed/cancelled/deleted/nonexistent cup is rejected
# with a friendly flash (never a 500). The <int:cup_id> path uses the bounded
# converter, so an oversized cup_id 404s before we run.


@app.route("/cup-session/<int:cup_id>/players/add", methods=["POST"])
def cup_session_add_player(cup_id):
    """Add an existing player to an in-progress cup.

    New players start with half_veto_count=0. UNIQUE(cup_id, player_id) makes a
    duplicate add a friendly no-op, not a 500. A player added AFTER the race-3
    stale-veto-forfeit check is intentionally NOT retroactively forfeited: the
    forfeit fires once, in cup_session_next_race when entering race 3, over the
    roster present at that moment. A late-add keeps half_veto_count=0 (all vetoes
    intact) — it simply wasn't subject to that one-time event.
    """
    try:
        player_id = parse_int_field(request.form.get("player_id", "").strip())
    except ValueError:
        flash("Invalid player selection.")
        return redirect(url_for("cup_session_race", cup_id=cup_id))

    conn = get_connection()
    try:
        # Friendly common-path checks (nice messages); the conditional INSERT
        # below is the authoritative, race-safe guard.
        cup = conn.execute(
            "SELECT status, deleted_at FROM cups WHERE id = ?", (cup_id,)
        ).fetchone()
        if cup is None or cup["deleted_at"] is not None or cup["status"] != "in_progress":
            flash("Players can only be edited while a cup is in progress.")
            return redirect(url_for("cups"))
        player = conn.execute(
            "SELECT id, name FROM players WHERE id = ?", (player_id,)
        ).fetchone()
        if player is None:
            flash("That player doesn't exist.")
            return redirect(url_for("cup_session_race", cup_id=cup_id))
        # Fold the in-progress guard into the INSERT to close the TOCTOU between
        # the SELECT above and this write (mirrors create_score, issue #52): if
        # the cup is completed/cancelled/deleted in that window, EXISTS is false
        # and no row is inserted. UNIQUE(cup_id, player_id) blocks a duplicate.
        cursor = conn.execute(
            "INSERT INTO cup_players (cup_id, player_id, half_veto_count) "
            "SELECT ?, ?, 0 "
            "WHERE EXISTS (SELECT 1 FROM cups WHERE id = ? AND deleted_at IS NULL AND status = 'in_progress')",
            (cup_id, player_id, cup_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            flash("Players can only be edited while a cup is in progress.")
            return redirect(url_for("cups"))
        conn.commit()
        flash(f"Added {player['name']} to the cup.", "success")
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That player is already in this cup.")
    finally:
        conn.close()
    return redirect(url_for("cup_session_race", cup_id=cup_id))


@app.route("/cup-session/<int:cup_id>/players/remove", methods=["POST"])
def cup_session_remove_player(cup_id):
    """Remove a player from an in-progress cup.

    Refuses to drop the roster below MIN_ROSTER_SIZE (a cup can never be emptied
    of players). The removed player's half-veto history is dropped with the row
    (acceptable — vetoes are per-cup state). Completion uses the LIVE roster
    (cup_session_complete renders players from cup_players, and
    apply_line_adjustments keys off the submitted scores for that roster), so a
    removal here is reflected correctly at completion — e.g. a 3-player Wii cup
    dropped to 2 no longer triggers the 3-player line adjustment.
    """
    try:
        player_id = parse_int_field(request.form.get("player_id", "").strip())
    except ValueError:
        flash("Invalid player selection.")
        return redirect(url_for("cup_session_race", cup_id=cup_id))

    conn = get_connection()
    try:
        # Friendly common-path checks; the conditional DELETE below is the
        # authoritative, race-safe guard.
        cup = conn.execute(
            "SELECT status, deleted_at FROM cups WHERE id = ?", (cup_id,)
        ).fetchone()
        if cup is None or cup["deleted_at"] is not None or cup["status"] != "in_progress":
            flash("Players can only be edited while a cup is in progress.")
            return redirect(url_for("cups"))
        in_cup = conn.execute(
            "SELECT 1 FROM cup_players WHERE cup_id = ? AND player_id = ?",
            (cup_id, player_id),
        ).fetchone()
        if in_cup is None:
            flash("That player isn't in this cup.")
            return redirect(url_for("cup_session_race", cup_id=cup_id))
        roster_size = conn.execute(
            "SELECT COUNT(*) AS n FROM cup_players WHERE cup_id = ?", (cup_id,)
        ).fetchone()["n"]
        if roster_size <= MIN_ROSTER_SIZE:
            flash(
                f"A cup must keep at least {MIN_ROSTER_SIZE} player"
                f"{'s' if MIN_ROSTER_SIZE != 1 else ''}."
            )
            return redirect(url_for("cup_session_race", cup_id=cup_id))
        # Authoritative atomic DELETE. The min-roster guard lives INSIDE the
        # statement (COUNT > MIN, evaluated against the pre-delete table state)
        # so two concurrent removes can't both pass the read-check above and
        # drop the roster below the minimum — SQLite serializes writers, and a
        # remove only fires while count is still strictly above MIN. The cup
        # in-progress guard is folded in too (TOCTOU-safe).
        cursor = conn.execute(
            "DELETE FROM cup_players "
            "WHERE cup_id = ? AND player_id = ? "
            "AND EXISTS (SELECT 1 FROM cups WHERE id = ? AND deleted_at IS NULL AND status = 'in_progress') "
            "AND (SELECT COUNT(*) FROM cup_players WHERE cup_id = ?) > ?",
            (cup_id, player_id, cup_id, cup_id, MIN_ROSTER_SIZE),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            flash("Could not remove that player.")
            return redirect(url_for("cup_session_race", cup_id=cup_id))
        conn.commit()
        flash("Player removed from the cup.", "success")
    finally:
        conn.close()
    return redirect(url_for("cup_session_race", cup_id=cup_id))


# --- Photo score entry (extraction + photo persistence) ---

PHOTO_ALLOWED_MIMES = ("image/jpeg", "image/png")
# Decoded photo size cap. The client downscales to ~150-300 KB JPEG and the
# whole request body is already capped at 1 MB (MAX_CONTENT_LENGTH), so
# anything near this is a hostile payload, not a real photo.
MAX_PHOTO_BYTES = 900 * 1024


def decode_photo(image_b64, mime_type):
    """Validate and decode a base64 photo payload. Returns the raw bytes.

    Raises InvalidInput (with a user-presentable message) on a non-string /
    non-base64 / empty / oversized payload or an unsupported mime type.
    """
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise InvalidInput("Photo data is missing.")
    if mime_type not in PHOTO_ALLOWED_MIMES:
        raise InvalidInput("Photo must be a JPEG or PNG image.")
    try:
        decoded = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidInput("Photo data is not valid base64.")
    if not decoded:
        raise InvalidInput("Photo data is empty.")
    if len(decoded) > MAX_PHOTO_BYTES:
        raise InvalidInput("Photo is too large.")
    return decoded


def parse_photo_from_form(form):
    """Return (bytes, mime_type) for a photo attached to a score form, or None
    when no photo was attached. Raises InvalidInput on a malformed payload —
    callers reject the whole submit with a flash (the photo is part of the
    record; silently dropping it would lose data without telling anyone).
    """
    photo_data = (form.get("photo_data") or "").strip()
    if not photo_data:
        return None
    mime_type = (form.get("photo_mime") or "").strip()
    return decode_photo(photo_data, mime_type), mime_type


def save_cup_photo(conn, cup_id, photo):
    """Insert an attached photo for a cup (photo = (bytes, mime_type))."""
    image, mime_type = photo
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO cup_photos (cup_id, image, mime_type, created_at) VALUES (?, ?, ?, ?)",
        (cup_id, image, mime_type, created_at),
    )


@app.route("/cups/<int:cup_id>/photo")
def cup_photo(cup_id):
    """Serve the newest photo attached to a cup. 404 when there is none (or
    the cup is soft-deleted). Sends ETag + Cache-Control and honors
    If-None-Match with a 304, so /cups doesn't re-download every photo on
    every visit.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT p.image, p.mime_type FROM cup_photos p"
        " JOIN cups c ON c.id = p.cup_id"
        " WHERE p.cup_id = ? AND c.deleted_at IS NULL"
        " ORDER BY p.id DESC LIMIT 1",
        (cup_id,),
    ).fetchone()
    conn.close()
    if row is None:
        abort(404)
    response = Response(row["image"], mimetype=row["mime_type"])
    response.set_etag(hashlib.sha1(bytes(row["image"])).hexdigest())
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response.make_conditional(request)


def default_character_field(edition):
    """Which players column holds the default character for an edition."""
    return "default_character_wii" if edition == "wii" else "default_character_switch"


def match_standings_to_players(rows, players, edition):
    """Match extracted standings rows to players via their default characters.

    rows: extracted StandingsRow objects (position/character/points).
    players: dicts with player_id, name, and the default-character columns.

    Returns (scores, ambiguous, unmatched):
      scores    — {player_id: points} for exactly-one-player/exactly-one-row
                  character matches.
      ambiguous — player names left unfilled because their character maps to
                  2+ rows, or 2+ players claim the same character.
      unmatched — player names with no default character for this edition, or
                  whose character isn't on the screen.
    Extracted rows claimed by no player (CPU racers) are ignored.
    """
    char_field = default_character_field(edition)

    def norm(value):
        return value.strip().casefold()

    # Highlight is a SAFE HINT, not a hard filter (revised 2026-07 after
    # real-photo validation). The human-vs-CPU highlight cue is real but the
    # model misses it under dark/glare photos and across Wii's two different
    # results screens, so we NEVER *require* a highlight to fill — we only use
    # highlight to protect against auto-filling a CPU's score. The human's
    # dropdown (client-side) is the actual guarantee; auto-fill just gets the
    # common case right without ever grabbing a wrong (CPU) row.
    #
    # Build character -> rows over ALL rows (keep the highlight flag on each so
    # we can prefer highlighted matches per character below).
    rows_by_char = {}  # normalized character -> [rows]
    for row in rows:
        rows_by_char.setdefault(norm(row.character), []).append(row)

    # Did the model flag ANY row as highlighted anywhere in the photo? If not,
    # detection clearly failed (or it's an old 3-tuple read) — we then fall
    # back to pure character matching so we never regress below pre-highlight
    # behavior (the "cup-52 under-detection" safety). NOTE: Switch (mk8dx) has
    # no reliable human-vs-CPU cue, so its prompt never sets is_highlighted —
    # every Switch photo therefore flows through this zero-highlight fallback to
    # character-only matching by design (Switch highlight auto-fill is a future
    # follow-up). Highlight-aware safe auto-fill is effectively Wii-only today.
    any_highlighted = any(getattr(r, "is_highlighted", False) for r in rows)

    claims = {}  # normalized character -> [player dicts]
    unmatched = []
    for player in players:
        character = player[char_field]
        # A player with no default character for this edition can't be matched.
        if not character or not character.strip():
            unmatched.append(player["name"])
            continue
        claims.setdefault(norm(character), []).append(player)

    scores = {}
    ambiguous = []
    for key, claimants in claims.items():
        # Unchanged rule: if 2+ players main the same character we can't tell
        # their rows apart -> everyone on that character is ambiguous.
        if len(claimants) != 1:
            ambiguous.extend(p["name"] for p in claimants)
            continue
        player = claimants[0]

        matching = rows_by_char.get(key, [])
        hl_matching = [r for r in matching if getattr(r, "is_highlighted", False)]

        if len(hl_matching) == 1:
            # Confident human match: exactly one highlighted row wears this
            # character. Auto-fill it.
            scores[player["player_id"]] = hl_matching[0].points
        elif len(hl_matching) >= 2:
            # Two human rows share the character (rare) -> let the human decide.
            ambiguous.append(player["name"])
        else:
            # No HIGHLIGHTED row carries this character.
            if any_highlighted:
                # The character appears ONLY on non-highlighted (CPU) rows (or
                # not at all). Do NOT auto-fill a CPU score — leave the player
                # blank so the human picks the right row. This is the
                # off-character protection: e.g. a player defaults to Toad but
                # Toad is a CPU here while they actually played a highlighted
                # Dry Bowser -> we must not hand them the CPU Toad's points.
                unmatched.append(player["name"])
            else:
                # Zero highlights detected anywhere -> highlight detection
                # failed; fall back to pre-highlight character matching so we
                # never do worse than before this feature existed.
                if len(matching) == 1:
                    scores[player["player_id"]] = matching[0].points
                elif len(matching) >= 2:
                    ambiguous.append(player["name"])
                else:
                    unmatched.append(player["name"])
    return scores, sorted(ambiguous), sorted(unmatched)


def _players_for_extraction(data):
    """Resolve (edition, players) for /extract-scores from cup_id OR
    edition + player_ids. Returns (edition, players, error_response)."""
    player_select = (
        "SELECT p.id AS player_id, p.name, p.default_character_wii, "
        "p.default_character_switch FROM players p"
    )
    cup_id = data.get("cup_id")
    if cup_id is not None:
        if isinstance(cup_id, bool) or not isinstance(cup_id, (int, str)):
            return None, None, (jsonify({"error": "Invalid cup_id"}), 400)
        try:
            cup_id = parse_int_field(cup_id)
        except ValueError:
            return None, None, (jsonify({"error": "Invalid cup_id"}), 400)
        conn = get_connection()
        cup = conn.execute(
            "SELECT id, game_edition FROM cups WHERE id = ? AND deleted_at IS NULL",
            (cup_id,),
        ).fetchone()
        if cup is None:
            conn.close()
            return None, None, (jsonify({"error": "Cup not found"}), 404)
        players = conn.execute(
            player_select + " JOIN cup_players cp ON cp.player_id = p.id WHERE cp.cup_id = ?",
            (cup_id,),
        ).fetchall()
        conn.close()
        if not players:
            return None, None, (jsonify({"error": "Cup has no players"}), 400)
        return cup["game_edition"], [dict(p) for p in players], None

    edition = data.get("edition")
    if edition not in TRACK_SETS:
        return None, None, (jsonify({"error": "Unknown edition"}), 400)
    player_ids = data.get("player_ids")
    if (
        not isinstance(player_ids, list)
        or not player_ids
        or len(player_ids) > MAX_SCORE_ROWS
    ):
        return None, None, (jsonify({"error": "player_ids must be a non-empty list"}), 400)
    parsed_ids = []
    for pid in player_ids:
        if isinstance(pid, bool) or not isinstance(pid, (int, str)):
            return None, None, (jsonify({"error": "Invalid player id"}), 400)
        try:
            parsed_ids.append(parse_int_field(pid))
        except ValueError:
            return None, None, (jsonify({"error": "Invalid player id"}), 400)
    placeholders = ",".join("?" * len(parsed_ids))
    conn = get_connection()
    players = conn.execute(
        player_select + f" WHERE p.id IN ({placeholders})", parsed_ids
    ).fetchall()
    conn.close()
    if len(players) != len(set(parsed_ids)):
        return None, None, (jsonify({"error": "Unknown player id"}), 400)
    return edition, [dict(p) for p in players], None


@app.route("/extract-scores", methods=["POST"])
def extract_scores():
    """Extract {position, character, points} rows from a standings photo and
    match them to this cup's players. JSON in, JSON out; never auto-submits.

    Request: {image: <base64>, mime_type: image/jpeg|image/png,
              cup_id: <id>}  — live session form, players/edition from the cup
           or {edition, player_ids: [...]} — manual /cups/new form.
    Response: {scores: {player_id: points}, ambiguous: [names],
               unmatched_players: [names], raw_rows: [...]}.
    """
    if not extraction_enabled():
        return jsonify({"error": "extraction not configured"}), 503

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body required"}), 400

    mime_type = data.get("mime_type")
    try:
        decode_photo(data.get("image"), mime_type)
    except InvalidInput as e:
        return jsonify({"error": str(e)}), 400

    edition, players, error = _players_for_extraction(data)
    if error:
        return error

    try:
        standings = extract_standings(data["image"], mime_type, edition=edition)
    except ExtractionError:
        app.logger.exception("Photo score extraction failed")
        return (
            jsonify({"error": "Could not read the photo. Try another shot or enter scores manually."}),
            502,
        )

    scores, ambiguous, unmatched = match_standings_to_players(
        standings.rows, players, edition
    )
    return jsonify(
        {
            "scores": scores,
            "ambiguous": ambiguous,
            "unmatched_players": unmatched,
            "raw_rows": [
                {
                    "position": r.position,
                    "character": r.character,
                    "points": r.points,
                    "is_highlighted": bool(r.is_highlighted),
                }
                for r in standings.rows
            ],
        }
    )


if __name__ == "__main__":
    staging = "--staging" in sys.argv
    db_path_override = resolve_db_path(staging, os.environ)
    if db_path_override is not None:
        os.environ["DB_PATH"] = db_path_override
    mode = "STAGING" if staging else "prod"
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # In debug mode, Flask's reloader runs this file twice — once in the
    # parent (watcher) and once in the child (actual server). Only init the
    # DB in the child to avoid double backups.
    is_reloader_parent = debug and os.environ.get("WERKZEUG_RUN_MAIN") is None
    if not is_reloader_parent:
        print(f"[{mode}] Using DB at {get_db_path()}", flush=True)
        init_db()
    app.run(host="0.0.0.0", port=8080, debug=debug)
