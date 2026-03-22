import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, url_for

from db import get_connection, init_db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/players")
def players():
    conn = get_connection()
    rows = conn.execute("SELECT id, name FROM players ORDER BY name").fetchall()
    conn.close()
    return render_template("players.html", players=rows)


@app.route("/players", methods=["POST"])
def create_player():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name cannot be empty.")
        return redirect(url_for("players"))
    conn = get_connection()
    try:
        conn.execute("INSERT INTO players (name) VALUES (?)", (name,))
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
        "SELECT id, name FROM players WHERE id = ?", (player_id,)
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
    try:
        conn.execute("UPDATE players SET name = ? WHERE id = ?", (name, player_id))
        conn.commit()
    except sqlite3.IntegrityError:
        flash(f"A player named \"{name}\" already exists.")
        return redirect(url_for("edit_player", player_id=player_id))
    finally:
        conn.close()
    return redirect(url_for("players"))


@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id):
    # TODO: When scores CRUD exists, prevent deleting players who have scores.
    conn = get_connection()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("players"))


@app.route("/cups")
def cups():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, date, notes FROM cups ORDER BY date DESC"
    ).fetchall()
    conn.close()
    return render_template("cups.html", cups=rows)


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

    conn = get_connection()
    try:
        conn.execute("INSERT INTO cups (date, notes) VALUES (?, ?)", (date_utc, notes))
        conn.commit()
    except sqlite3.IntegrityError:
        flash("A cup already exists at that time.")
    finally:
        conn.close()
    return redirect(url_for("cups"))


@app.route("/cups/<int:cup_id>/edit")
def edit_cup(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id, date, notes FROM cups WHERE id = ?", (cup_id,)
    ).fetchone()
    conn.close()
    if cup is None:
        abort(404)
    return render_template("cup_edit.html", cup=cup)


@app.route("/cups/<int:cup_id>/edit", methods=["POST"])
def update_cup(cup_id):
    conn = get_connection()
    cup = conn.execute(
        "SELECT id FROM cups WHERE id = ?", (cup_id,)
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

    try:
        conn.execute(
            "UPDATE cups SET date = ?, notes = ? WHERE id = ?",
            (date_utc, notes, cup_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        flash("A cup already exists at that time.")
        return redirect(url_for("edit_cup", cup_id=cup_id))
    finally:
        conn.close()
    return redirect(url_for("cups"))


@app.route("/cups/<int:cup_id>/delete", methods=["POST"])
def delete_cup(cup_id):
    # TODO: When scores CRUD exists, prevent deleting cups that have scores.
    conn = get_connection()
    conn.execute("DELETE FROM cups WHERE id = ?", (cup_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("cups"))


if __name__ == "__main__":
    debug = True
    # In debug mode, Flask's reloader runs this file twice — once in the
    # parent (watcher) and once in the child (actual server). Only init the
    # DB in the child to avoid double backups.
    is_reloader_parent = debug and os.environ.get("WERKZEUG_RUN_MAIN") is None
    if not is_reloader_parent:
        init_db()
    app.run(host="0.0.0.0", port=8080, debug=debug)
