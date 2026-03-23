import os
import sqlite3
from datetime import datetime, timedelta, timezone

from collections import Counter

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, url_for

from db import get_connection, init_db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")


@app.template_filter("format_line")
def format_line(value):
    """Format a line value with a sign: +0, +3, -5."""
    n = int(value)
    return f"+{n}" if n >= 0 else str(n)


@app.route("/")
def index():
    return render_template("index.html")


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
        flash("Name cannot be empty.")
        return redirect(url_for("edit_player", player_id=player_id))
    default_cup = request.form.get("default_cup") == "on"
    has_line = request.form.get("has_line") == "on"
    line = request.form.get("line", "0").strip()
    try:
        line = int(line)
    except ValueError:
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
        "SELECT 1 FROM scores WHERE player_id = ? LIMIT 1", (player_id,)
    ).fetchone()
    if has_scores:
        conn.close()
        flash("Cannot delete a player who has scores recorded.")
        return redirect(url_for("players"))
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("players"))


@app.route("/cups")
def cups():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, date, notes FROM cups WHERE deleted_at IS NULL ORDER BY date DESC"
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
        date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:00")

    scores_data = parse_scores_from_form(request.form)
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
        "SELECT id, date, notes FROM cups WHERE id = ? AND deleted_at IS NULL",
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
        flash("Invalid date format.")
        return redirect(url_for("edit_cup", cup_id=cup_id))

    scores_data = parse_scores_from_form(request.form)
    if scores_data:
        for s in scores_data:
            s["line_score"] = s["score"] + s["line"]
        lines_by_id = {s["player_id"]: s["line"] for s in scores_data}
        error = validate_scores(scores_data, lines_by_id)
        if error:
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


def parse_scores_from_form(form):
    """Extract score data from form submission.

    Expects form fields: player_ids[], scores[], lines[] (optional), tiebreakers[] (list of player_ids).
    Returns list of dicts with keys: player_id, score, line, won_tiebreaker.
    Skips players with empty score fields.
    """
    player_ids = form.getlist("player_ids[]")
    raw_scores = form.getlist("scores[]")
    lines = form.getlist("lines[]")
    tiebreaker_ids = set(form.getlist("tiebreakers[]"))
    scores_data = []
    for i, (pid, raw) in enumerate(zip(player_ids, raw_scores)):
        raw = raw.strip()
        if raw == "":
            continue
        line_val = 0
        if i < len(lines) and lines[i].strip() != "":
            try:
                line_val = int(lines[i].strip())
            except ValueError:
                pass
        scores_data.append({
            "player_id": int(pid),
            "score": int(raw),
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


def apply_line_adjustments(conn, cup_id, scores_data):
    """Apply line adjustments for a 3-player cup.

    Only applies if exactly 3 players. Returns list of changes for display,
    or empty list if no adjustments were made.
    """
    if len(scores_data) != 3:
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

    conn = get_connection()
    try:
        player = conn.execute(
            "SELECT line FROM players WHERE id = ?", (int(player_id),)
        ).fetchone()
        player_line = player["line"] if player else 0
        line_score_val = int(score) + player_line
        conn.execute(
            "INSERT INTO scores (cup_id, player_id, score, line, line_score, won_tiebreaker) VALUES (?, ?, ?, ?, ?, ?)",
            (int(cup_id), int(player_id), int(score), player_line, line_score_val, won_tiebreaker or None),
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
        flash("Score cannot be empty.")
        return redirect(url_for("edit_score", score_id=score_id))

    try:
        player = conn.execute(
            "SELECT line FROM players WHERE id = ?", (existing["player_id"],)
        ).fetchone()
        player_line = player["line"] if player else 0
        line_score_val = int(score_val) + player_line
        conn.execute(
            "UPDATE scores SET score = ?, line = ?, line_score = ?, won_tiebreaker = ? WHERE id = ?",
            (int(score_val), player_line, line_score_val, won_tiebreaker or None, score_id),
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


if __name__ == "__main__":
    debug = True
    # In debug mode, Flask's reloader runs this file twice — once in the
    # parent (watcher) and once in the child (actual server). Only init the
    # DB in the child to avoid double backups.
    is_reloader_parent = debug and os.environ.get("WERKZEUG_RUN_MAIN") is None
    if not is_reloader_parent:
        init_db()
    app.run(host="0.0.0.0", port=8080, debug=debug)
