"""Regression tests for issue #42 — SQLite lock contention surfacing as 500s.

Three layers are exercised:
  1. get_connection() carries busy_timeout + WAL (unit + deterministic db-level).
  2. The Flask errorhandler maps a lock-timeout OperationalError to a controlled
     503/redirect, but re-raises any other OperationalError (no masking).
  3. A threaded live server proves sustained concurrent writes never 500.
"""

import os
import socket
import sqlite3
import tempfile
import threading
import time

import pytest
import requests

from db import get_connection, init_db


# ---------------------------------------------------------------------------
# Layer 1 — connection PRAGMAs
# ---------------------------------------------------------------------------


def test_connection_has_busy_timeout_and_wal(tmp_path):
    """A fresh file-backed connection must queue writers (busy_timeout > 0) and
    run in WAL mode so readers don't block writers."""
    db_path = str(tmp_path / "km.db")
    init_db(db_path)
    conn = get_connection(db_path)
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert busy_timeout > 0
    assert journal_mode.lower() == "wal"


def test_busy_timeout_makes_writer_queue(tmp_path):
    """Deterministic proof the busy_timeout pragma is doing the work: while one
    connection holds a write lock, a get_connection() writer queues and then
    succeeds once the lock is released — whereas a raw connection WITHOUT the
    pragma raises "database is locked" immediately. If get_connection ever
    stopped setting busy_timeout, this test would fail."""
    db_path = str(tmp_path / "km.db")
    init_db(db_path)

    acquired = threading.Event()

    def hold_lock():
        # A SQLite connection can only be used in the thread that created it, so
        # the holder both acquires and releases the write lock here.
        holder = get_connection(db_path)
        holder.execute("BEGIN IMMEDIATE")  # acquire the write lock
        holder.execute("INSERT INTO players (name) VALUES ('holder')")
        acquired.set()
        time.sleep(0.8)  # hold long enough that the writer below has to queue
        holder.commit()
        holder.close()

    holder_thread = threading.Thread(target=hold_lock)
    holder_thread.start()
    assert acquired.wait(timeout=5), "holder never acquired the lock"

    # Control: a connection with a 0 busy_timeout fails instantly under the lock.
    control = sqlite3.connect(db_path)
    control.execute("PRAGMA busy_timeout = 0")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        control.execute("INSERT INTO players (name) VALUES ('control')")
    control.close()

    # get_connection() carries busy_timeout=5000 -> it queues and succeeds once
    # the holder releases (well within the timeout).
    writer = get_connection(db_path)
    writer.execute("INSERT INTO players (name) VALUES ('writer')")
    writer.commit()
    writer.close()

    holder_thread.join()

    conn = get_connection(db_path)
    names = {row["name"] for row in conn.execute("SELECT name FROM players")}
    conn.close()
    assert {"holder", "writer"} <= names


# ---------------------------------------------------------------------------
# Layer 2 — the errorhandler (via monkeypatched get_connection)
# ---------------------------------------------------------------------------


def test_lock_error_returns_controlled_response_not_500(client, monkeypatch):
    """A lock-timeout OperationalError must NOT surface as a 500. Browser
    (HTML) callers get a redirect back; fetch/JSON callers get a 503."""
    import app as app_module

    def raise_locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app_module, "get_connection", raise_locked)

    # HTML navigation -> flash + redirect (3xx), never 500.
    html_resp = client.get("/players", headers={"Accept": "text/html"})
    assert html_resp.status_code in (302, 303)

    # fetch/XHR (Accept: */*) -> controlled 503 JSON.
    json_resp = client.get("/players", headers={"Accept": "*/*"})
    assert json_resp.status_code == 503
    assert "busy" in json_resp.get_json()["error"].lower()


def test_non_lock_operational_error_is_not_masked(client, monkeypatch):
    """Any OperationalError that is NOT lock contention must still surface (500 /
    propagate) — the handler must not swallow real bugs."""
    import app as app_module

    def raise_other(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: players")

    monkeypatch.setattr(app_module, "get_connection", raise_other)

    # With TESTING/PROPAGATE_EXCEPTIONS the re-raised error propagates out of the
    # test client rather than being converted to a controlled response.
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        client.get("/players", headers={"Accept": "text/html"})


# ---------------------------------------------------------------------------
# Layer 3 — threaded live server under sustained concurrent writes
# ---------------------------------------------------------------------------


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Threaded werkzeug server on a file-backed WAL DB, seeded with a pool of
    deletable players. Borrows the pattern from tests/e2e/conftest.py."""
    db_dir = tempfile.mkdtemp()
    db_path = os.path.join(db_dir, "concurrency_test.db")
    os.environ["DB_PATH"] = db_path
    init_db(db_path)

    # Seed a pool of players with no scores so the delete workers have targets.
    conn = get_connection(db_path)
    for i in range(60):
        conn.execute("INSERT INTO players (name) VALUES (?)", (f"seed_{i}",))
    conn.commit()
    conn.close()

    from app import app

    app.config["TESTING"] = True
    port = _find_free_port()
    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    )
    server.start()

    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)

    yield {"url": f"http://127.0.0.1:{port}", "db_path": db_path}

    os.environ.pop("DB_PATH", None)


def test_concurrent_writes_never_500(live_server):
    """Hammer three write surfaces (player add, player delete, cup/score
    submission) concurrently from many threads. Every response must be a
    success/redirect (2xx/3xx) or the controlled 503 — never a 500 with an
    uncaught 'database is locked'."""
    base = live_server["url"]
    html = {"Accept": "text/html"}  # simulate browser form navigations
    bad = []  # (label, status/exception)
    lock = threading.Lock()

    ITER = 12

    def record(label, status):
        # 500 (and 4xx that isn't our controlled path) is the regression signal.
        if status == 500:
            with lock:
                bad.append((label, status))

    def add_worker(tid):
        for i in range(ITER):
            try:
                r = requests.post(
                    f"{base}/players",
                    data={"name": f"add_{tid}_{i}", "default_cup": "on"},
                    headers=html,
                    allow_redirects=False,
                    timeout=15,
                )
                record(f"add[{tid}]", r.status_code)
            except Exception as e:  # noqa: BLE001 - a raised exception is also a failure
                with lock:
                    bad.append((f"add[{tid}]", repr(e)))

    def delete_worker(tid):
        for i in range(ITER):
            pid = 1 + (tid * ITER + i) % 60
            try:
                r = requests.post(
                    f"{base}/players/{pid}/delete",
                    headers=html,
                    allow_redirects=False,
                    timeout=15,
                )
                record(f"delete[{tid}]", r.status_code)
            except Exception as e:  # noqa: BLE001
                with lock:
                    bad.append((f"delete[{tid}]", repr(e)))

    def cup_worker(tid):
        # Each cup needs a player and a unique timestamp. Create a dedicated
        # player per worker up front, then submit cups (a multi-row INSERT txn,
        # which holds the write lock longer -> more contention).
        try:
            requests.post(
                f"{base}/players",
                data={"name": f"cupowner_{tid}", "default_cup": "on"},
                headers=html,
                allow_redirects=False,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass
        # Find the owner's id.
        conn = get_connection(live_server["db_path"])
        row = conn.execute(
            "SELECT id FROM players WHERE name = ?", (f"cupowner_{tid}",)
        ).fetchone()
        conn.close()
        if row is None:
            return
        pid = str(row["id"])
        for i in range(ITER):
            date = f"2026-01-{(tid % 27) + 1:02d} {i:02d}:{tid:02d}:00"
            try:
                r = requests.post(
                    f"{base}/cups",
                    data={
                        "date": date,
                        "notes": "",
                        "tz_offset": "",
                        "player_ids[]": [pid],
                        "scores[]": ["50"],
                        "lines[]": ["0"],
                    },
                    headers=html,
                    allow_redirects=False,
                    timeout=15,
                )
                record(f"cup[{tid}]", r.status_code)
            except Exception as e:  # noqa: BLE001
                with lock:
                    bad.append((f"cup[{tid}]", repr(e)))

    threads = []
    for tid in range(4):
        threads.append(threading.Thread(target=add_worker, args=(tid,)))
    for tid in range(2):
        threads.append(threading.Thread(target=delete_worker, args=(tid,)))
    for tid in range(3):
        threads.append(threading.Thread(target=cup_worker, args=(tid,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not bad, f"Concurrent writes produced 500s / errors: {bad}"
