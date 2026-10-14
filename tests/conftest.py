import pytest
from fastapi.testclient import TestClient

from waypoint.config import Settings
from waypoint.main import create_app

ADMIN = {"email": "admin@example.com", "password": "correct-horse-battery"}


@pytest.fixture
def app():
    return create_app(Settings(db_path=":memory:", secret_key="test-secret", bootstrap_token="boot-token",
                               sweep_seconds=0, public_url="https://waypoint.test"))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/bootstrap", json={"token": "boot-token", **ADMIN})
    assert r.status_code == 201, r.text
    token = client.post("/api/auth/login", json=ADMIN).json()["token"]
    return auth_header(token)


def invite_user(client, admin, email, role):
    inv = client.post("/api/users/invite", json={"email": email, "role": role}, headers=admin).json()
    r = client.post("/api/invites/accept", json={"token": inv["token"], "password": "another-long-pass"})
    assert r.status_code == 200, r.text
    return auth_header(r.json()["token"])
