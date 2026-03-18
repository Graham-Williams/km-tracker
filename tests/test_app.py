import pytest

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_index_returns_200(client):
    response = client.get("/")
    assert response.status_code == 200


def test_index_contains_title(client):
    response = client.get("/")
    assert b"KM Tracker" in response.data
