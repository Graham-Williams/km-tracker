import os
import socket
import tempfile
import threading
import time

import pytest

from db import get_connection, init_db


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def _server():
    """Start Flask once for the entire e2e test session.

    When E2E_BASE_URL is set, the server is already running externally
    (e.g. in a Docker container) — skip starting Flask and return the
    DB path from the DB_PATH env var so _reset_db can wipe between tests.
    """
    external_url = os.environ.get("E2E_BASE_URL")
    if external_url:
        db_path = os.environ.get("DB_PATH")
        yield {"url": external_url, "db_path": db_path}
        return

    from app import app

    db_dir = tempfile.mkdtemp()
    db_path = os.path.join(db_dir, "e2e_test.db")
    os.environ["DB_PATH"] = db_path
    init_db(db_path)
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


@pytest.fixture(autouse=True)
def _reset_db(_server):
    """Wipe all data between tests while keeping the schema."""
    yield
    conn = get_connection(_server["db_path"])
    conn.execute("DELETE FROM races")
    conn.execute("DELETE FROM cup_players")
    conn.execute("DELETE FROM scores")
    conn.execute("DELETE FROM line_changes")
    conn.execute("DELETE FROM cups")
    conn.execute("DELETE FROM players")
    conn.commit()
    conn.close()


@pytest.fixture(scope="session")
def base_url(_server):
    return _server["url"]
