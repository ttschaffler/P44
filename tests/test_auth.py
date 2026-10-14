from .conftest import ADMIN, auth_header, invite_user


def test_everything_requires_auth(client):
    for path in ["/api/shipments", "/api/orders", "/api/analytics", "/api/exceptions", "/api/users", "/api/carriers"]:
        assert client.get(path).status_code == 401, path
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/shipments", headers=auth_header("wps_forged")).status_code == 401


def test_bootstrap_only_once_and_needs_token(client):
    assert client.post("/api/auth/bootstrap", json={"token": "wrong", **ADMIN}).status_code == 403
    assert client.post("/api/auth/bootstrap", json={"token": "boot-token", **ADMIN}).status_code == 201
    again = {"token": "boot-token", "email": "x@example.com", "password": "another-long-pass"}
    assert client.post("/api/auth/bootstrap", json=again).status_code == 409


def test_bad_password_rejected(client, admin):
    assert client.post("/api/auth/login", json={**ADMIN, "password": "nope"}).status_code == 401


def test_invite_flow_and_roles(client, admin):
    viewer = invite_user(client, admin, "viewer@example.com", "viewer")
    member = invite_user(client, admin, "member@example.com", "member")
    assert client.get("/api/shipments", headers=viewer).status_code == 200
    assert client.post("/api/carriers", json={"code": "ABC", "name": "A", "modes": ["TL"]}, headers=viewer).status_code == 403
    assert client.post("/api/carriers", json={"code": "ABC", "name": "A", "modes": ["TL"]}, headers=member).status_code == 201
    assert client.get("/api/users", headers=member).status_code == 403
    assert client.post("/api/users/invite", json={"email": "z@example.com"}, headers=member).status_code == 403


def test_invite_is_single_use(client, admin):
    inv = client.post("/api/users/invite", json={"email": "once@example.com", "role": "viewer"}, headers=admin).json()
    assert inv["invite_url"].startswith("https://waypoint.test/?invite=")
    assert client.post("/api/invites/accept", json={"token": inv["token"], "password": "long-enough-pw"}).status_code == 200
    assert client.post("/api/invites/accept", json={"token": inv["token"], "password": "long-enough-pw"}).status_code == 400


def test_short_password_rejected_on_invite(client, admin):
    inv = client.post("/api/users/invite", json={"email": "s@example.com", "role": "viewer"}, headers=admin).json()
    assert client.post("/api/invites/accept", json={"token": inv["token"], "password": "short"}).status_code == 400


def test_revoke_cuts_access_immediately(client, admin):
    viewer = invite_user(client, admin, "gone@example.com", "viewer")
    uid = next(u["id"] for u in client.get("/api/users", headers=admin).json()["users"] if u["email"] == "gone@example.com")
    assert client.post(f"/api/users/{uid}/revoke", headers=admin).status_code == 204
    assert client.get("/api/shipments", headers=viewer).status_code == 401
    assert client.post("/api/auth/login", json={"email": "gone@example.com", "password": "another-long-pass"}).status_code == 401


def test_api_key_scoped_and_revocable(client, admin):
    key = client.post("/api/api-keys", json={"name": "tms", "role": "viewer"}, headers=admin).json()
    h = auth_header(key["key"])
    assert client.get("/api/shipments", headers=h).status_code == 200
    assert client.post("/api/events", json=[], headers=h).status_code == 403
    assert client.delete(f"/api/api-keys/{key['id']}", headers=admin).status_code == 204
    assert client.get("/api/shipments", headers=h).status_code == 401


def test_logout_and_audit(client, admin):
    actions = [a["action"] for a in client.get("/api/audit", headers=admin).json()]
    assert "bootstrap_admin" in actions and "login" in actions
    assert client.post("/api/auth/logout", headers=admin).status_code == 204
    assert client.get("/api/auth/me", headers=admin).status_code == 401
