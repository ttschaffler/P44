"""Exception engine: open and auto-resolve shipment exceptions from current state."""
from datetime import datetime, timedelta

from .eta import PROFILES
from .timeutil import iso, parse_ts

MISSED_PICKUP_GRACE_H = 2


def evaluate_conditions(sh: dict, now: datetime) -> dict[str, tuple[str, str]]:
    """Return {type: (severity, message)} for every exception condition currently true."""
    prof = PROFILES[sh["mode"]]
    status = sh["status"]
    found: dict[str, tuple[str, str]] = {}
    if status in ("DELIVERED", "CANCELLED"):
        return found

    planned_delivery = parse_ts(sh["planned_delivery"])
    eta = parse_ts(sh["eta"])
    if eta and eta > planned_delivery + timedelta(hours=prof.late_tolerance_h):
        late_h = (eta - planned_delivery).total_seconds() / 3600
        severity = "high" if late_h > prof.late_tolerance_h * 3 else "medium"
        found["LATE"] = (severity, f"ETA {late_h:.1f} h after planned delivery")

    if status == "PLANNED":
        pickup = parse_ts(sh["planned_pickup"])
        if now > pickup + timedelta(hours=MISSED_PICKUP_GRACE_H):
            found["MISSED_PICKUP"] = ("high", f"Not picked up {(now - pickup).total_seconds() / 3600:.1f} h after plan")
        return found

    last = parse_ts(sh["last_event_at"])
    if last and now - last > timedelta(hours=prof.stale_after_h):
        found["STALE_TRACKING"] = ("medium", f"No tracking update for {(now - last).total_seconds() / 3600:.1f} h")

    since = parse_ts(sh["status_since"])
    if status == "AT_STOP" and since and now - since > timedelta(hours=prof.long_dwell_h):
        found["LONG_DWELL"] = ("medium", f"At stop for {(now - since).total_seconds() / 3600:.1f} h")
    return found


def apply_rules(db, sh: dict, now: datetime) -> list[dict]:
    """Sync the exceptions table with current conditions. Caller holds the transaction."""
    current = evaluate_conditions(sh, now)
    open_rows = {r["type"]: r for r in db.query(
        "SELECT * FROM exceptions WHERE shipment_id=? AND resolved_at IS NULL", (sh["id"],))}
    changes = []
    for typ, (severity, message) in current.items():
        if typ in open_rows:
            db.execute("UPDATE exceptions SET severity=?, message=? WHERE id=?",
                       (severity, message, open_rows[typ]["id"]))
        else:
            db.execute("INSERT INTO exceptions (shipment_id,type,severity,message,opened_at) VALUES (?,?,?,?,?)",
                       (sh["id"], typ, severity, message, iso(now)))
            changes.append({"event": "exception.opened", "shipment_ref": sh["ref"], "type": typ,
                            "severity": severity, "message": message})
    for typ, row in open_rows.items():
        if typ not in current:
            db.execute("UPDATE exceptions SET resolved_at=? WHERE id=?", (iso(now), row["id"]))
            changes.append({"event": "exception.resolved", "shipment_ref": sh["ref"], "type": typ})
    return changes
