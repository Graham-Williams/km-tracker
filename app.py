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
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from db import get_connection, get_db_path, init_db
from maps import (
    DEFAULT_EDITION,
    EDITION_LABELS,
    TRACK_SETS,
    courses_for,
    edition_label,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Cap request body size to blunt memory-exhaustion DoS. Every legit form here is
# tiny (a cup has a handful of players); 256 KB is far more than any real submit
# yet small enough that a flood of oversized bodies can't exhaust memory. Flask
# returns 413 Request Entity Too Large automatically when this is exceeded.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # 256 KB


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


@app.route("/players")
def players():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, default_cup, line, has_line FROM players ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("players.html", players=rows)


@app.route("/players", methods=["POST"])
def create_player():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name cannot be empty.")
        return redirect(url_for("players"))
    default_cup = request.form.get("default_cup") == "on"
    has_line = request.form.get("has_line") == "on"
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO players (name, default_cup, has_line) VALUES (?, ?, ?)",
            (name, default_cup, has_line),
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
        "SELECT id, name, default_cup, line, has_line FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    conn.close()
    if player is None:
        abort(404)
    return render_template("player_edit.html", player=player)


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
        conn.execute(
            "UPDATE players SET name = ?, default_cup = ?, line = ?, has_line = ? WHERE id = ?",
            (name, default_cup, line, has_line, player_id),
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
    conn.execute("DELETE FROM line_changes WHERE player_id = ?", (player_id,))
    conn.execute("DELETE FROM scores WHERE player_id = ?", (player_id,))
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
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
    else:
        lc_rows = []
    line_changes = {}
    for lc in lc_rows:
        line_changes.setdefault(lc["cup_id"], []).append(lc)
    conn.close()
    return render_template("cups.html", cups=rows, results=results, line_changes=line_changes)


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

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO cups (date, notes) VALUES (?, ?)", (date_utc, notes)
        )
        cup_id = cursor.lastrowid
        save_scores(conn, cup_id, scores_data)
        changes = apply_line_adjustments(conn, cup_id, scores_data)
        conn.commit()
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
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line, line_score, won_tiebreaker) VALUES (?, ?, ?, ?, ?, ?)",
            (cup_id, player_id, score, player_line, line_score_val, won_tiebreaker or None),
        )
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
        cursor = conn.execute(
            "INSERT INTO cups (date, status, game_edition) VALUES (?, 'in_progress', ?)",
            (date_utc, edition),
        )
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

    conn.close()
    return {"cup": cup, "players": players, "races": races}


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
        races=session["races"],
        played_maps=played_maps,
        current_race=current_race,
        max_races=MAX_RACES,
        max_half_vetoes=MAX_HALF_VETOES,
        max_votoes=MAX_VOTOES,
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
        "SELECT id, status FROM cups WHERE id = ? AND status = 'in_progress'",
        (cup_id,),
    ).fetchone()
    if cup is None:
        conn.close()
        abort(404)

    # Update races if edited
    for i in range(1, MAX_RACES + 1):
        new_map = request.form.get(f"race_{i}")
        if new_map:
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
        conn.commit()
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
