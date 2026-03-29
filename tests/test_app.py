from app import app


def test_index_returns_200():
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/")
        assert response.status_code == 200


def test_index_contains_title():
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/")
        assert b"KM Tracker" in response.data
