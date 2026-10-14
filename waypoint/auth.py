"""Invitation-only accounts, sessions, API keys, roles and audit log."""
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request

from .security import (ROLE_RANK, hash_password, new_token, password_ok, token_hash,
                       verify_password)
from .timeutil import iso, parse_ts, utcnow


@dataclass
class Principal:
    kind: str          # "user" | "api_key"
    id: int
    name: str
    role: str
    token_hash: str | None = None


def audit(db, actor: str | None, action: str, detail: str = "") -> None:
    db.execute("INSERT INTO audit (ts,actor,action,detail) VALUES (?,?,?,?)", (iso(utcnow()), actor, action, detail))


def _err(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


def bootstrap_admin(db, settings, token: str, email: str, password: str) -> dict:
    if not settings.bootstrap_token or token != settings.bootstrap_token:
        _err(403, "invalid bootstrap token")
    if db.one("SELECT 1 FROM users WHERE role='admin'"):
        _err(409, "an admin already exists; use invitations")
    if not password_ok(password):
        _err(400, "password must be at least 10 characters")
    uid = db.execute("INSERT INTO users (email,password_hash,role,created_at) VALUES (?,?,?,?)",
                     (email.lower(), hash_password(password), "admin", iso(utcnow())))
    audit(db, email.lower(), "bootstrap_admin")
    return {"id": uid, "email": email.lower(), "role": "admin"}


def login(db, settings, email: str, password: str) -> dict:
    u = db.one("SELECT * FROM users WHERE email=?", (email.lower(),))
    if not u or not u["active"] or not verify_password(password, u["password_hash"]):
        audit(db, email.lower(), "login_failed")
        _err(401, "invalid email or password")
    token = new_token("wps")
    db.execute("INSERT INTO sessions (token_hash,user_id,expires_at) VALUES (?,?,?)",
               (token_hash(token, settings.secret_key), u["id"],
                iso(utcnow() + timedelta(hours=settings.session_hours))))
    audit(db, u["email"], "login")
    return {"token": token, "user": {"id": u["id"], "email": u["email"], "role": u["role"]}}


def invite(db, settings, actor: Principal, email: str, role: str) -> dict:
    if role not in ROLE_RANK:
        _err(400, "role must be admin, member or viewer")
    email = email.lower()
    if db.one("SELECT 1 FROM users WHERE email=? AND active=1", (email,)):
        _err(409, "user already exists")
    token = new_token("wpi")
    expires = utcnow() + timedelta(hours=settings.invite_hours)
    db.execute("INSERT INTO invites (email,role,token_hash,expires_at,created_by) VALUES (?,?,?,?,?)",
               (email, role, token_hash(token, settings.secret_key), iso(expires), actor.id))
    audit(db, actor.name, "invite", f"{email} as {role}")
    return {"email": email, "role": role, "expires_at": iso(expires), "token": token,
            "invite_url": f"{settings.public_url.rstrip('/')}/?invite={token}"}


def accept_invite(db, settings, token: str, password: str) -> dict:
    inv = db.one("SELECT * FROM invites WHERE token_hash=?", (token_hash(token, settings.secret_key),))
    if not inv or inv["used_at"] or parse_ts(inv["expires_at"]) < utcnow():
        _err(400, "invitation is invalid, used or expired")
    if not password_ok(password):
        _err(400, "password must be at least 10 characters")
    with db.tx():
        existing = db.one("SELECT id FROM users WHERE email=?", (inv["email"],))
        if existing:  # re-inviting a revoked user reactivates them
            db.execute("UPDATE users SET password_hash=?, role=?, active=1 WHERE id=?",
                       (hash_password(password), inv["role"], existing["id"]))
        else:
            db.execute("INSERT INTO users (email,password_hash,role,created_at) VALUES (?,?,?,?)",
                       (inv["email"], hash_password(password), inv["role"], iso(utcnow())))
        db.execute("UPDATE invites SET used_at=? WHERE id=?", (iso(utcnow()), inv["id"]))
    audit(db, inv["email"], "invite_accepted")
    return login(db, settings, inv["email"], password)


def revoke_user(db, actor: Principal, user_id: int) -> None:
    if user_id == actor.id and actor.kind == "user":
        _err(400, "you cannot revoke yourself")
    u = db.one("SELECT email FROM users WHERE id=?", (user_id,))
    if not u:
        _err(404, "user not found")
    with db.tx():
        db.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.execute("UPDATE api_keys SET revoked=1 WHERE created_by=?", (user_id,))
    audit(db, actor.name, "revoke_user", u["email"])


def create_api_key(db, settings, actor: Principal, name: str, role: str) -> dict:
    if role not in ("member", "viewer"):
        _err(400, "API key role must be member or viewer")
    key = new_token("wpk")
    kid = db.execute("INSERT INTO api_keys (name,key_hash,role,created_by,created_at) VALUES (?,?,?,?,?)",
                     (name, token_hash(key, settings.secret_key), role, actor.id, iso(utcnow())))
    audit(db, actor.name, "create_api_key", f"{name} ({role})")
    return {"id": kid, "name": name, "role": role, "key": key}


def resolve(db, settings, token: str) -> Principal | None:
    th = token_hash(token, settings.secret_key)
    if token.startswith("wps_"):
        row = db.one("""SELECT u.id, u.email, u.role, u.active, s.expires_at FROM sessions s
                        JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (th,))
        if row and row["active"] and parse_ts(row["expires_at"]) > utcnow():
            return Principal("user", row["id"], row["email"], row["role"], th)
    elif token.startswith("wpk_"):
        row = db.one("""SELECT k.id, k.name, k.role FROM api_keys k JOIN users u ON u.id=k.created_by
                        WHERE k.key_hash=? AND k.revoked=0 AND u.active=1""", (th,))
        if row:
            return Principal("api_key", row["id"], f"key:{row['name']}", row["role"], th)
    return None


def current_principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    p = resolve(request.app.state.db, request.app.state.settings, token) if token else None
    if p is None:
        _err(401, "authentication required")
    return p


def require(min_role: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if ROLE_RANK[p.role] < ROLE_RANK[min_role]:
            _err(403, f"requires {min_role} role")
        return p
    return dep
