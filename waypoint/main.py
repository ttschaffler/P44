"""HTTP API and dashboard."""
import asyncio
import contextlib
import csv
import io
import secrets
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, tracking, webhooks
from .auth import Principal, require
from .config import Settings, get_settings
from .db import Database
from .timeutil import utcnow

STATIC = Path(__file__).parent / "static"
Role = Literal["admin", "member", "viewer"]
Mode = Literal["TL", "LTL", "PARCEL", "OCEAN", "AIR", "RAIL"]


# ---------- request models ----------

class BootstrapIn(BaseModel):
    token: str
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class AcceptIn(BaseModel):
    token: str
    password: str


class InviteIn(BaseModel):
    email: str
    role: Role = "viewer"


class ApiKeyIn(BaseModel):
    name: str
    role: Literal["member", "viewer"] = "member"


class CarrierIn(BaseModel):
    code: str = Field(min_length=2, max_length=8)
    name: str
    modes: list[Mode]


class OrderIn(BaseModel):
    ref: str
    customer: str | None = None


class StopIn(BaseModel):
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    planned_arrival: str | None = None


class ShipmentIn(BaseModel):
    ref: str
    mode: Mode
    carrier: str | None = None
    order_ref: str | None = None
    planned_pickup: str
    planned_delivery: str
    stops: list[StopIn]


class EventIn(BaseModel):
    shipment_ref: str | None = None
    timestamp: str | None = None
    status: str | None = None
    lat: float | str | None = None
    lon: float | str | None = None
    stop_sequence: int | str | None = None
    source: str | None = None


class WebhookIn(BaseModel):
    url: str = Field(pattern=r"^https?://")
    events: list[Literal["shipment.status_changed", "exception.opened", "exception.resolved"]]


# ---------- app ----------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.sweep_seconds > 0:
            async def loop():
                while True:
                    await asyncio.sleep(settings.sweep_seconds)
                    notes = await asyncio.to_thread(tracking.sweep, db, utcnow())
                    await asyncio.to_thread(webhooks.deliver, db, notes)
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Waypoint", version=__version__, lifespan=lifespan,
                  description="Clean-room multimodal supply-chain visibility platform.")
    app.state.db = db
    app.state.settings = settings

    def guard(fn, *args):
        try:
            return fn(*args)
        except tracking.NotFound as e:
            raise HTTPException(404, str(e))
        except tracking.Invalid as e:
            raise HTTPException(400, str(e))

    def notify(bg: BackgroundTasks, notes: list[dict]):
        if notes:
            bg.add_task(webhooks.deliver, db, notes)

    # --- public ---
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.post("/api/auth/bootstrap", status_code=201)
    def bootstrap(body: BootstrapIn):
        return auth.bootstrap_admin(db, settings, body.token, body.email, body.password)

    @app.post("/api/auth/login")
    def login(body: LoginIn):
        return auth.login(db, settings, body.email, body.password)

    @app.post("/api/invites/accept")
    def accept(body: AcceptIn):
        return auth.accept_invite(db, settings, body.token, body.password)

    # --- session ---
    @app.get("/api/auth/me")
    def me(p: Principal = Depends(auth.current_principal)):
        return {"name": p.name, "role": p.role, "kind": p.kind}

    @app.post("/api/auth/logout", status_code=204)
    def logout(p: Principal = Depends(auth.current_principal)):
        if p.kind == "user":
            db.execute("DELETE FROM sessions WHERE token_hash=?", (p.token_hash,))

    # --- admin: people and keys ---
    @app.post("/api/users/invite", status_code=201)
    def invite(body: InviteIn, p: Principal = Depends(require("admin"))):
        return auth.invite(db, settings, p, body.email, body.role)

    @app.get("/api/users")
    def users(p: Principal = Depends(require("admin"))):
        people = [dict(r) for r in db.query("SELECT id,email,role,active,created_at FROM users ORDER BY email")]
        pending = [dict(r) for r in db.query(
            "SELECT email,role,expires_at FROM invites WHERE used_at IS NULL AND expires_at > ? ORDER BY email",
            (tracking.iso(utcnow()),))]
        return {"users": people, "pending_invites": pending}

    @app.post("/api/users/{user_id}/revoke", status_code=204)
    def revoke(user_id: int, p: Principal = Depends(require("admin"))):
        auth.revoke_user(db, p, user_id)

    @app.post("/api/api-keys", status_code=201)
    def create_key(body: ApiKeyIn, p: Principal = Depends(require("admin"))):
        return auth.create_api_key(db, settings, p, body.name, body.role)

    @app.get("/api/api-keys")
    def keys(p: Principal = Depends(require("admin"))):
        return [dict(r) for r in db.query("SELECT id,name,role,created_at,revoked FROM api_keys ORDER BY id")]

    @app.delete("/api/api-keys/{key_id}", status_code=204)
    def revoke_key(key_id: int, p: Principal = Depends(require("admin"))):
        db.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (key_id,))
        auth.audit(db, p.name, "revoke_api_key", str(key_id))

    @app.get("/api/audit")
    def audit_log(limit: int = 200, p: Principal = Depends(require("admin"))):
        return [dict(r) for r in db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    # --- master data ---
    @app.get("/api/carriers")
    def carriers(p: Principal = Depends(require("viewer"))):
        return [{**dict(r), "modes": r["modes"].split(",")} for r in db.query("SELECT code,name,modes FROM carriers ORDER BY code")]

    @app.post("/api/carriers", status_code=201)
    def add_carrier(body: CarrierIn, p: Principal = Depends(require("member"))):
        return guard(tracking.create_carrier, db, body.code, body.name, list(body.modes))

    @app.get("/api/orders")
    def orders(p: Principal = Depends(require("viewer"))):
        return tracking.list_orders(db)

    @app.get("/api/orders/{ref}")
    def order(ref: str, p: Principal = Depends(require("viewer"))):
        return guard(tracking.order_view, db, ref)

    @app.post("/api/orders", status_code=201)
    def add_order(body: OrderIn, p: Principal = Depends(require("member"))):
        return guard(tracking.create_order, db, body.ref, body.customer, utcnow())

    # --- shipments ---
    @app.get("/api/shipments")
    def shipments(mode: str | None = None, carrier: str | None = None, status: str | None = None,
                  exceptions_only: bool = False, p: Principal = Depends(require("viewer"))):
        return tracking.list_shipments(db, mode, carrier, status, exceptions_only)

    @app.get("/api/shipments/{ref}")
    def shipment(ref: str, p: Principal = Depends(require("viewer"))):
        return guard(tracking.shipment_detail, db, ref)

    @app.post("/api/shipments", status_code=201)
    def add_shipment(body: ShipmentIn, bg: BackgroundTasks, p: Principal = Depends(require("member"))):
        notify(bg, guard(tracking.create_shipment, db, body.model_dump(), utcnow()))
        return tracking.shipment_detail(db, body.ref)

    @app.post("/api/shipments/{ref}/cancel")
    def cancel(ref: str, bg: BackgroundTasks, p: Principal = Depends(require("member"))):
        notify(bg, guard(tracking.cancel_shipment, db, ref, utcnow()))
        return tracking.shipment_detail(db, ref)

    # --- tracking ingestion ---
    @app.post("/api/events")
    def events(body: EventIn | list[EventIn], bg: BackgroundTasks, p: Principal = Depends(require("member"))):
        records = [e.model_dump() for e in (body if isinstance(body, list) else [body])]
        if len(records) > 5000:
            raise HTTPException(413, "max 5000 events per request")
        result = tracking.ingest(db, records, utcnow())
        notify(bg, result.pop("notifications"))
        return result

    @app.post("/api/events/csv")
    async def events_csv(request: Request, bg: BackgroundTasks, p: Principal = Depends(require("member"))):
        text = (await request.body()).decode("utf-8-sig")
        records = list(csv.DictReader(io.StringIO(text)))
        result = tracking.ingest(db, records, utcnow())
        notify(bg, result.pop("notifications"))
        return result

    @app.get("/api/quarantine")
    def quarantine(limit: int = 100, p: Principal = Depends(require("member"))):
        return [dict(r) for r in db.query("SELECT * FROM quarantine ORDER BY id DESC LIMIT ?", (limit,))]

    # --- exceptions & analytics ---
    @app.get("/api/exceptions")
    def exceptions(open_only: bool = True, p: Principal = Depends(require("viewer"))):
        sql = """SELECT e.type,e.severity,e.message,e.opened_at,e.resolved_at,s.ref AS shipment_ref,s.mode
                 FROM exceptions e JOIN shipments s ON s.id=e.shipment_id"""
        if open_only:
            sql += " WHERE e.resolved_at IS NULL"
        return [dict(r) for r in db.query(sql + " ORDER BY e.opened_at DESC")]

    @app.post("/api/exceptions/sweep")
    def run_sweep(bg: BackgroundTasks, p: Principal = Depends(require("member"))):
        notes = tracking.sweep(db, utcnow())
        notify(bg, notes)
        return {"changes": notes}

    @app.get("/api/analytics")
    def analytics(group_by: Literal["carrier", "mode", "lane"] | None = None, mode: str | None = None,
                  carrier: str | None = None, p: Principal = Depends(require("viewer"))):
        return tracking.analytics(db, utcnow(), group_by, mode, carrier)

    # --- webhooks ---
    @app.get("/api/webhooks")
    def list_hooks(p: Principal = Depends(require("admin"))):
        return [{**dict(r), "secret": None} for r in db.query("SELECT * FROM webhooks")]

    @app.post("/api/webhooks", status_code=201)
    def add_hook(body: WebhookIn, p: Principal = Depends(require("admin"))):
        secret = secrets.token_hex(24)
        hid = db.execute("INSERT INTO webhooks (url,secret,events) VALUES (?,?,?)",
                         (body.url, secret, ",".join(body.events)))
        auth.audit(db, p.name, "create_webhook", body.url)
        return {"id": hid, "url": body.url, "events": body.events, "secret": secret}

    @app.delete("/api/webhooks/{hook_id}", status_code=204)
    def delete_hook(hook_id: int, p: Principal = Depends(require("admin"))):
        db.execute("UPDATE webhooks SET active=0 WHERE id=?", (hook_id,))

    # --- dashboard ---
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    return app

