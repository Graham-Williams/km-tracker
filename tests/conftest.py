import os
import sys

import pytest

# Add tests/ to the path so helpers.py can be imported from test files
sys.path.insert(0, os.path.dirname(__file__))

import app as app_module
from app import app
from db import init_db


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = db_path
    init_db(db_path)
    app.config["TESTING"] = True
    # The /extract-scores throttle lives in a module-level dict keyed by client
    # IP, and every test client is 127.0.0.1 in one process — so without this,
    # extraction calls accumulate ACROSS tests and an unrelated test eventually
    # gets a 429. Each test starts with an empty bucket; the throttle's own
    # tests exercise it explicitly.
    app_module._extract_calls.clear()
    with app.test_client() as client:
        yield client
    del os.environ["DB_PATH"]
