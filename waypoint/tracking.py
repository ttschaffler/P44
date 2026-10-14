"""Shipment lifecycle: creation, event ingestion, ETA refresh, order stitching, analytics."""
import json
from datetime import datetime, timedelta

from . import rules
from .eta import PROFILES, RoutePoint, default_model, haversine_km
from .normalize import (ACTIVE_STATUSES, MODES, STATUS_RANK, TERMINAL_STATUSES, Rejected,
                        normalize)
from .timeutil import iso, parse_ts


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


# ---------- reads ----------

def shipment_row(db, ref: str) -> dict:
    row = db.one("""SELECT s.*, c.code AS carrier_code, c.name AS carrier_name, o.ref AS order_ref
                    FROM shipments s LEFT JOIN carriers c ON c.id=s.carrier_id
                    LEFT JOIN orders o ON o.id=s.order_id WHERE s.ref=?""", (ref,))
    if row is None:
        raise NotFound(f"shipment {ref} not found")
    return dict(row)


def stops_for(db, shipment_id: int) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM stops WHERE shipment_id=? ORDER BY seq", (shipment_id,))]


def summarize(db, sh: dict) -> dict:
    stops = stops_for(db, sh["id"])
    open_exc = db.query("SELECT type, severity FROM exceptions WHERE shipment_id=? AND resolved_at IS NULL",
                        (sh["id"],))
    return {
        "ref": sh["ref"], "mode": sh["mode"], "status": sh["status"],
        "carrier": sh.get("carrier_code"), "carrier_name": sh.get("carrier_name"), "order_ref": sh.get("order_ref"),
        "origin": stops[0]["name"] if stops else None, "destination": stops[-1]["name"] if stops else None,
        "planned_pickup": sh["planned_pickup"], "planned_delivery": sh["planned_delivery"],
        "eta": sh["eta"], "delivered_at": sh["delivered_at"], "last_event_at": sh["last_event_at"],
        "position": {"lat": sh["cur_lat"], "lon": sh["cur_lon"]} if sh["cur_lat"] is not None else None,
        "exceptions": [dict(e) for e in open_exc],
    }


def shipment_detail(db, ref: str) -> dict:
    sh = shipment_row(db, ref)
    out = summarize(db, sh)
    out["stops"] = stops_for(db, sh["id"])
    out["events"] = [dict(r) for r in db.query(
        "SELECT ts,status,raw_status,lat,lon,stop_seq,source FROM events WHERE shipment_id=? ORDER BY ts DESC",
        (sh["id"],))]
    out["exception_history"] = [dict(r) for r in db.query(
        "SELECT type,severity,message,opened_at,resolved_at FROM exceptions WHERE shipment_id=? ORDER BY opened_at DESC",
        (sh["id"],))]
    return out


def list_shipments(db, mode=None, carrier=None, status=None, exceptions_only=False, order_ref=None) -> list[dict]:
    sql = """SELECT s.*, c.code AS carrier_code, c.name AS carrier_name, o.ref AS order_ref
             FROM shipments s LEFT JOIN carriers c ON c.id=s.carrier_id LEFT JOIN orders o ON o.id=s.order_id WHERE 1=1"""
    params: list = []
    if mode:
        sql += " AND s.mode=?"; params.append(mode.upper())
    if carrier:
        sql += " AND c.code=?"; params.append(carrier.upper())
    if status:
        sql += " AND s.status=?"; params.append(status.upper())
    if order_ref:
        sql += " AND o.ref=?"; params.append(order_ref)
    if exceptions_only:
        sql += " AND EXISTS (SELECT 1 FROM exceptions e WHERE e.shipment_id=s.id AND e.resolved_at IS NULL)"
    sql += " ORDER BY s.planned_delivery"
    return [summarize(db, dict(r)) for r in db.query(sql, params)]


# ---------- writes ----------

def create_carrier(db, code: str, name: str, modes: list[str]) -> dict:
    bad = [m for m in modes if m not in MODES]
    if bad:
        raise Invalid(f"unknown modes {bad}")
    if db.one("SELECT 1 FROM carriers WHERE code=?", (code.upper(),)):
        raise Invalid(f"carrier {code} already exists")
    db.execute("INSERT INTO carriers (code,name,modes) VALUES (?,?,?)", (code.upper(), name, ",".join(modes)))
    return {"code": code.upper(), "name": name, "modes": modes}


def create_order(db, ref: str, customer: str | None, now: datetime) -> dict:
    if db.one("SELECT 1 FROM orders WHERE ref=?", (ref,)):
        raise Invalid(f"order {ref} already exists")
    db.execute("INSERT INTO orders (ref,customer,created_at) VALUES (?,?,?)", (ref, customer, iso(now)))
    return {"ref": ref, "customer": customer}


def create_shipment(db, data: dict, now: datetime) -> list[dict]:
    mode = data["mode"].upper()
    if mode not in MODES:
        raise Invalid(f"mode must be one of {MODES}")
    stops = data["stops"]
    if len(stops) < 2:
        raise Invalid("a shipment needs at least an origin and a destination stop")
    pickup, delivery = parse_ts(data["planned_pickup"]), parse_ts(data["planned_delivery"])
    if delivery <= pickup:
        raise Invalid("planned_delivery must be after planned_pickup")
    with db.tx():
        if db.one("SELECT 1 FROM shipments WHERE ref=?", (data["ref"],)):
            raise Invalid(f"shipment {data['ref']} already exists")
        carrier_id = order_id = None
        if data.get("carrier"):
            c = db.one("SELECT id, modes FROM carriers WHERE code=?", (data["carrier"].upper(),))
            if not c:
                raise Invalid(f"unknown carrier {data['carrier']}")
            if mode not in c["modes"].split(","):
                raise Invalid(f"carrier {data['carrier']} does not run {mode}")
            carrier_id = c["id"]
        if data.get("order_ref"):
            o = db.one("SELECT id FROM orders WHERE ref=?", (data["order_ref"],))
            if not o:
                raise Invalid(f"unknown order {data['order_ref']}")
            order_id = o["id"]
        sid = db.execute(
            """INSERT INTO shipments (ref,mode,carrier_id,order_id,planned_pickup,planned_delivery,status,status_since,created_at)
               VALUES (?,?,?,?,?,?, 'PLANNED', ?, ?)""",
            (data["ref"], mode, carrier_id, order_id, iso(pickup), iso(delivery), iso(now), iso(now)))
        for i, s in enumerate(stops):
            db.execute("INSERT INTO stops (shipment_id,seq,name,lat,lon,planned_arrival) VALUES (?,?,?,?,?,?)",
                       (sid, i, s["name"], s["lat"], s["lon"], s.get("planned_arrival")))
        return refresh(db, data["ref"], now)


def cancel_shipment(db, ref: str, now: datetime) -> list[dict]:
    with db.tx():
        sh = shipment_row(db, ref)
        if sh["status"] in TERMINAL_STATUSES:
            raise Invalid(f"shipment is already {sh['status']}")
        db.execute("UPDATE shipments SET status='CANCELLED', status_since=? WHERE id=?", (iso(now), sh["id"]))
        notes = [{"event": "shipment.status_changed", "shipment_ref": ref, "from": sh["status"], "to": "CANCELLED"}]
        return notes + refresh(db, ref, now)


def refresh(db, ref: str, now: datetime) -> list[dict]:
    """Recompute ETA and exceptions for one shipment. Caller holds the transaction."""
    sh = shipment_row(db, ref)
    if sh["status"] == "DELIVERED":
        eta = sh["delivered_at"]
    elif sh["status"] == "CANCELLED":
        eta = None
    else:
        stops = stops_for(db, sh["id"])
        points = [RoutePoint(s["lat"], s["lon"], s["actual_arrival"] is not None) for s in stops]
        pos = (sh["cur_lat"], sh["cur_lon"]) if sh["cur_lat"] is not None else None
        eta = iso(default_model.predict(sh["mode"], sh["status"], pos, points, parse_ts(sh["planned_pickup"]), now))
    db.execute("UPDATE shipments SET eta=? WHERE id=?", (eta, sh["id"]))
    sh["eta"] = eta
    return rules.apply_rules(db, sh, now)


def _match_stop(stops: list[dict], ev, mode: str) -> dict | None:
    if ev.stop_seq is not None:
        return next((s for s in stops if s["seq"] == ev.stop_seq), None)
    if ev.lat is None:
        return None
    radius = PROFILES[mode].arrival_radius_km
    candidates = [s for s in stops if haversine_km(ev.lat, ev.lon, s["lat"], s["lon"]) <= radius]
    unvisited = [s for s in candidates if s["actual_arrival"] is None]
    return (unvisited or candidates or [None])[0]


def ingest(db, records: list[dict], now: datetime) -> dict:
    """Normalize, dedupe and apply a batch of raw tracking records."""
    accepted, duplicates, quarantined, notes = 0, 0, [], []
    for idx, rec in enumerate(records):
        try:
            ev = normalize(rec, now)
            with db.tx():
                try:
                    sh = shipment_row(db, ev.shipment_ref)
                except NotFound:
                    raise Rejected(f"unknown shipment {ev.shipment_ref}")
                if db.one("SELECT 1 FROM events WHERE fingerprint=?", (ev.fingerprint,)):
                    duplicates += 1
                    continue
                notes += _apply_event(db, sh, ev, now)
                accepted += 1
        except Rejected as e:
            db.execute("INSERT INTO quarantine (received_at,payload,reason) VALUES (?,?,?)",
                       (iso(now), json.dumps(rec, default=str), str(e)))
            quarantined.append({"index": idx, "reason": str(e)})
    return {"accepted": accepted, "duplicates": duplicates, "quarantined": quarantined, "notifications": notes}


def _apply_event(db, sh: dict, ev, now: datetime) -> list[dict]:
    stops = stops_for(db, sh["id"])
    stop = None
    if ev.status in ("AT_STOP", "DELIVERED", "PICKED_UP"):
        stop = _match_stop(stops, ev, sh["mode"])
        if stop is None and ev.status == "DELIVERED":
            stop = stops[-1]
        if stop is None and ev.status == "PICKED_UP":
            stop = stops[0]
    db.execute("""INSERT INTO events (shipment_id,ts,status,raw_status,lat,lon,stop_seq,source,fingerprint,received_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (sh["id"], iso(ev.ts), ev.status, ev.raw_status, ev.lat, ev.lon,
                stop["seq"] if stop else ev.stop_seq, ev.source, ev.fingerprint, iso(now)))

    last = parse_ts(sh["last_event_at"])
    if last and ev.ts < last:
        # Late-arriving history: keep it in the timeline, don't rewind current state.
        return []
    if sh["status"] in TERMINAL_STATUSES:
        return []

    # Stop bookkeeping.
    if stop and ev.status in ("AT_STOP", "DELIVERED") and stop["actual_arrival"] is None:
        db.execute("UPDATE stops SET actual_arrival=? WHERE id=?", (iso(ev.ts), stop["id"]))
    if stop and ev.status == "PICKED_UP":
        db.execute("UPDATE stops SET actual_arrival=COALESCE(actual_arrival,?), actual_departure=? WHERE id=?",
                   (iso(ev.ts), iso(ev.ts), stop["id"]))
    if ev.status in ("IN_TRANSIT", "OUT_FOR_DELIVERY"):
        db.execute("""UPDATE stops SET actual_departure=? WHERE shipment_id=? AND actual_arrival IS NOT NULL
                      AND actual_departure IS NULL""", (iso(ev.ts), sh["id"]))

    new_status = ev.status
    # Never move backwards (IN_TRANSIT and AT_STOP share a rank, so they may alternate freely).
    if STATUS_RANK[new_status] < STATUS_RANK[sh["status"]]:
        new_status = sh["status"]
    lat = ev.lat if ev.lat is not None else (stop["lat"] if stop else sh["cur_lat"])
    lon = ev.lon if ev.lon is not None else (stop["lon"] if stop else sh["cur_lon"])
    status_since = iso(ev.ts) if new_status != sh["status"] else sh["status_since"]
    delivered_at = iso(ev.ts) if new_status == "DELIVERED" else None
    db.execute("""UPDATE shipments SET status=?, status_since=?, cur_lat=?, cur_lon=?, last_event_at=?,
                  delivered_at=COALESCE(?, delivered_at) WHERE id=?""",
               (new_status, status_since, lat, lon, iso(ev.ts), delivered_at, sh["id"]))
    notes = []
    if new_status != sh["status"]:
        notes.append({"event": "shipment.status_changed", "shipment_ref": sh["ref"],
                      "from": sh["status"], "to": new_status, "at": iso(ev.ts)})
    return notes + refresh(db, sh["ref"], now)


def sweep(db, now: datetime) -> list[dict]:
    """Periodic re-evaluation so time-based exceptions open without new events."""
    notes = []
    refs = [r["ref"] for r in db.query(
        "SELECT ref FROM shipments WHERE status NOT IN ('DELIVERED','CANCELLED')")]
    for ref in refs:
        with db.tx():
            notes += refresh(db, ref, now)
    return notes


# ---------- orders ----------

def order_view(db, ref: str) -> dict:
    o = db.one("SELECT * FROM orders WHERE ref=?", (ref,))
    if not o:
        raise NotFound(f"order {ref} not found")
    legs = list_shipments(db, order_ref=ref)
    return {"ref": o["ref"], "customer": o["customer"], **stitch(legs), "shipments": legs}


def stitch(legs: list[dict]) -> dict:
    """Order status = status of the current leg (first undelivered one in planned order).

    Order ETA cascades: a leg that has not started cannot leave before the previous leg arrives,
    so upstream delay is pushed onto downstream legs (`projected_eta`).
    """
    live = sorted((l for l in legs if l["status"] != "CANCELLED"), key=lambda l: l["planned_pickup"])
    if not live:
        return {"status": "CANCELLED" if legs else "PLANNED", "eta": None, "modes": [], "open_exceptions": 0,
                "current_leg": None}
    carry = None
    for leg in live:
        eta = parse_ts(leg["eta"])
        if eta and carry and leg["status"] == "PLANNED":
            slip = carry - parse_ts(leg["planned_pickup"])
            if slip > timedelta(0):
                eta += slip
        leg["projected_eta"] = iso(eta)
        carry = eta or carry
    pending = [l for l in live if l["status"] != "DELIVERED"]
    current = pending[0] if pending else live[-1]
    final = parse_ts(live[-1]["projected_eta"])
    planned_final = parse_ts(live[-1]["planned_delivery"])
    return {
        "status": current["status"],
        "current_leg": current["ref"],
        "eta": iso(final),
        "planned_delivery": iso(planned_final),
        "delay_h": round(max((final - planned_final).total_seconds(), 0) / 3600, 1) if final else None,
        "modes": sorted({l["mode"] for l in live}),
        "open_exceptions": sum(len(l["exceptions"]) for l in live),
    }


def list_orders(db) -> list[dict]:
    out = []
    for o in db.query("SELECT ref, customer FROM orders ORDER BY ref"):
        legs = list_shipments(db, order_ref=o["ref"])
        out.append({"ref": o["ref"], "customer": o["customer"], "legs": len(legs), **stitch(legs)})
    return out


# ---------- analytics ----------

def analytics(db, now: datetime, group_by: str | None = None, mode=None, carrier=None) -> dict:
    rows = list_shipments(db, mode=mode, carrier=carrier)

    def key(r):
        if group_by == "carrier":
            return r["carrier"] or "UNASSIGNED"
        if group_by == "mode":
            return r["mode"]
        if group_by == "lane":
            return f"{r['origin']} -> {r['destination']}"
        return "all"

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return {"generated_at": iso(now), "group_by": group_by or "all",
            "groups": [{"key": k, **_metrics(v, now)} for k, v in sorted(groups.items())]}


def _metrics(rows: list[dict], now: datetime) -> dict:
    delivered = [r for r in rows if r["status"] == "DELIVERED" and r["delivered_at"]]
    on_time, delays = 0, []
    for r in delivered:
        tol = timedelta(hours=PROFILES[r["mode"]].late_tolerance_h)
        delay = parse_ts(r["delivered_at"]) - parse_ts(r["planned_delivery"])
        if delay <= tol:
            on_time += 1
        delays.append(max(delay.total_seconds() / 3600, 0))
    active = [r for r in rows if r["status"] in ACTIVE_STATUSES]
    tracked = [r for r in active if r["last_event_at"] and
               now - parse_ts(r["last_event_at"]) <= timedelta(hours=PROFILES[r["mode"]].stale_after_h)]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    exc: dict[str, int] = {}
    for r in rows:
        for e in r["exceptions"]:
            exc[e["type"]] = exc.get(e["type"], 0) + 1
    return {
        "shipments": len(rows),
        "delivered": len(delivered),
        "on_time_pct": round(100 * on_time / len(delivered), 1) if delivered else None,
        "avg_delay_h": round(sum(delays) / len(delays), 2) if delays else None,
        "active": len(active),
        "tracking_coverage_pct": round(100 * len(tracked) / len(active), 1) if active else None,
        "by_status": by_status,
        "open_exceptions": exc,
    }
