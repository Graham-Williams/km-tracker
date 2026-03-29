import os
import sys

import pytest

# Add tests/ to the path so helpers.py can be imported from test files
sys.path.insert(0, os.path.dirname(__file__))

from app import app
from db import init_db


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = db_path
    init_db(db_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
    del os.environ["DB_PATH"]
