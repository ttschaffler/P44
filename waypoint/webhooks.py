"""Outbound, HMAC-signed webhook notifications. Failures are logged, never raised."""
import json
import urllib.request

from .security import sign_payload
from .timeutil import iso, utcnow


def deliver(db, notifications: list[dict]) -> None:
    if not notifications:
        return
    hooks = db.query("SELECT * FROM webhooks WHERE active=1")
    for n in notifications:
        for h in hooks:
            if n["event"] not in h["events"].split(","):
                continue
            body = json.dumps({**n, "sent_at": iso(utcnow())}).encode()
            req = urllib.request.Request(h["url"], data=body, method="POST", headers={
                "Content-Type": "application/json",
                "X-Waypoint-Event": n["event"],
                "X-Waypoint-Signature": sign_payload(body, h["secret"]),
            })
            code, err = None, None
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    code = resp.status
            except Exception as e:  # noqa: BLE001 - delivery must never break ingestion
                err = str(e)[:300]
            db.execute("INSERT INTO webhook_deliveries (webhook_id,event,status_code,error,ts) VALUES (?,?,?,?,?)",
                       (h["id"], n["event"], code, err, iso(utcnow())))
