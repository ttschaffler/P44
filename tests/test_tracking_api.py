import hashlib
import hmac
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from waypoint import tracking, webhooks
from waypoint.seed import seed
from waypoint.timeutil import iso, utcnow

CHI = {"name": "Chicago, IL", "lat": 41.88, "lon": -87.63}
KAL = {"name": "Kalamazoo, MI", "lat": 42.29, "lon": -85.59}
DET = {"name": "Detroit, MI", "lat": 42.33, "lon": -83.05}


@pytest.fixture
def setup(client, admin):
    client.post("/api/carriers", json={"code": "NRTH", "name": "Northline", "modes": ["TL", "LTL"]}, headers=admin)
    now = utcnow()
    body = {"ref": "S1", "mode": "TL", "carrier": "NRTH", "planned_pickup": iso(now - timedelta(hours=3)),
            "planned_delivery": iso(now + timedelta(hours=4)), "stops": [CHI, KAL, DET]}
    r = client.post("/api/shipments", json=body, headers=admin)
    assert r.status_code == 201, r.text
    return now


def ev(now, h, status, lat=None, lon=None, ref="S1", **kw):
    return {"shipment_ref": ref, "timestamp": iso(now - timedelta(hours=h)), "status": status, "lat": lat, "lon": lon, **kw}


def test_planned_shipment_past_pickup_opens_missed_pickup(client, admin, setup):
    s = client.get("/api/shipments/S1", headers=admin).json()
    assert s["status"] == "PLANNED" and s["eta"]
    # Still at origin 3 h after planned pickup: missed pickup, and the ETA now overshoots the plan.
    assert {e["type"] for e in s["exceptions"]} == {"MISSED_PICKUP", "LATE"}


def test_lifecycle_updates_status_stops_eta_and_resolves(client, admin, setup):
    now = setup
    r = client.post("/api/events", json=[
        ev(now, 2.5, "AF"),
        ev(now, 1.5, "X6", 42.0, -86.6),
        ev(now, 1.0, "Arrived", 42.29, -85.59),
        ev(now, 0.5, "Departed", 42.3, -85.0),
    ], headers=admin).json()
    assert r["accepted"] == 4 and r["quarantined"] == []
    s = client.get("/api/shipments/S1", headers=admin).json()
    assert s["status"] == "IN_TRANSIT"
    assert s["exceptions"] == []  # MISSED_PICKUP auto-resolved
    assert [st["actual_arrival"] is not None for st in s["stops"]] == [True, True, False]
    assert s["stops"][1]["actual_departure"] is not None
    assert tracking.parse_ts(s["eta"]) > now
    assert any(e["type"] == "MISSED_PICKUP" and e["resolved_at"] for e in s["exception_history"])

    client.post("/api/events", json=ev(now, 0.1, "D1"), headers=admin)
    s = client.get("/api/shipments/S1", headers=admin).json()
    assert s["status"] == "DELIVERED" and s["eta"] == s["delivered_at"]


def test_duplicates_and_quarantine(client, admin, setup):
    now = setup
    e = ev(now, 2, "Picked up", 41.88, -87.63)
    assert client.post("/api/events", json=[e, e], headers=admin).json()["duplicates"] == 1
    r = client.post("/api/events", json=[ev(now, 1, "teleported"), ev(now, 1, "X6", ref="NOPE")], headers=admin).json()
    assert [q["reason"] for q in r["quarantined"]] == ["unrecognized status 'teleported'", "unknown shipment NOPE"]
    assert len(client.get("/api/quarantine", headers=admin).json()) == 2


def test_late_out_of_order_event_does_not_rewind(client, admin, setup):
    now = setup
    client.post("/api/events", json=[ev(now, 2, "AF"), ev(now, 0.5, "OFD", 42.33, -83.05)], headers=admin)
    client.post("/api/events", json=ev(now, 1, "X6", 42.2, -84.0), headers=admin)
    s = client.get("/api/shipments/S1", headers=admin).json()
    assert s["status"] == "OUT_FOR_DELIVERY"
    assert len(s["events"]) == 3


def test_status_never_moves_backwards(client, admin, setup):
    now = setup
    client.post("/api/events", json=[ev(now, 2, "AF"), ev(now, 1, "X6", 42.0, -86.0), ev(now, 0.5, "Booked")], headers=admin)
    assert client.get("/api/shipments/S1", headers=admin).json()["status"] == "IN_TRANSIT"


def test_late_exception_from_eta(client, admin):
    now = utcnow()
    body = {"ref": "LATE1", "mode": "TL", "planned_pickup": iso(now - timedelta(hours=10)),
            "planned_delivery": iso(now - timedelta(hours=1)), "stops": [CHI, DET]}
    client.post("/api/shipments", json=body, headers=admin)
    client.post("/api/events", json=ev(now, 0.2, "X6", 41.9, -87.0, ref="LATE1"), headers=admin)
    exc = client.get("/api/exceptions", headers=admin).json()
    assert any(e["shipment_ref"] == "LATE1" and e["type"] == "LATE" for e in exc)


def test_csv_ingestion(client, admin, setup):
    now = setup
    csv_body = "shipment_ref,timestamp,status,lat,lon\n" \
               f"S1,{iso(now - timedelta(hours=2))},Picked up,41.88,-87.63\n" \
               f"S1,{iso(now - timedelta(hours=1))},En route,42.0,-86.5\n" \
               f"S1,not-a-date,En route,42.0,-86.5\n"
    r = client.post("/api/events/csv", content=csv_body, headers={**admin, "Content-Type": "text/csv"}).json()
    assert r["accepted"] == 2 and len(r["quarantined"]) == 1


def test_validation_errors(client, admin, setup):
    now = utcnow()
    bad = {"ref": "S1", "mode": "TL", "planned_pickup": iso(now), "planned_delivery": iso(now + timedelta(hours=1)), "stops": [CHI, DET]}
    assert client.post("/api/shipments", json=bad, headers=admin).status_code == 400  # duplicate ref
    assert client.post("/api/shipments", json={**bad, "ref": "S2", "stops": [CHI]}, headers=admin).status_code == 400
    assert client.post("/api/shipments", json={**bad, "ref": "S3", "carrier": "NRTH", "mode": "AIR"}, headers=admin).status_code == 400
    assert client.get("/api/shipments/NOPE", headers=admin).status_code == 404


def test_cancel(client, admin, setup):
    r = client.post("/api/shipments/S1/cancel", headers=admin).json()
    assert r["status"] == "CANCELLED" and r["exceptions"] == [] and r["eta"] is None
    assert client.post("/api/shipments/S1/cancel", headers=admin).status_code == 400


def test_order_stitching(client, admin):
    now = utcnow()
    client.post("/api/orders", json={"ref": "PO-1", "customer": "Acme"}, headers=admin)
    leg = lambda ref, mode, p, d: {"ref": ref, "mode": mode, "order_ref": "PO-1", "planned_pickup": iso(now + timedelta(hours=p)),
                                   "planned_delivery": iso(now + timedelta(hours=d)), "stops": [CHI, DET]}
    client.post("/api/shipments", json=leg("L1", "RAIL", -20, 2), headers=admin)
    client.post("/api/shipments", json=leg("L2", "TL", 6, 14), headers=admin)
    client.post("/api/events", json=[ev(now, 19, "Loaded", ref="L1"), ev(now, 1, "Train departed", 42.0, -86.0, ref="L1")], headers=admin)
    o = client.get("/api/orders/PO-1", headers=admin).json()
    assert o["status"] == "IN_TRANSIT" and o["current_leg"] == "L1"
    assert o["modes"] == ["RAIL", "TL"]
    l1, l2 = sorted(o["shipments"], key=lambda s: s["ref"])
    # L2 cannot leave before L1 arrives: L1's slip past L2's planned pickup is pushed onto L2.
    slip = tracking.parse_ts(l1["eta"]) - tracking.parse_ts(l2["planned_pickup"])
    assert slip.total_seconds() > 0
    assert tracking.parse_ts(o["eta"]) == tracking.parse_ts(l2["eta"]) + slip
    assert l2["projected_eta"] == o["eta"]
    client.post("/api/events", json=ev(now, 0.1, "Delivered", ref="L1"), headers=admin)
    o = client.get("/api/orders/PO-1", headers=admin).json()
    assert o["status"] == "PLANNED" and o["current_leg"] == "L2"


def test_seed_and_analytics(app, client, admin):
    result = seed(app.state.db, n_per_mode=4)
    assert result["shipments"] >= 24 and result["events"] > 50 and result["quarantined"] == 0
    a = client.get("/api/analytics?group_by=mode", headers=admin).json()
    assert {g["key"] for g in a["groups"]} == {"TL", "LTL", "PARCEL", "OCEAN", "AIR", "RAIL"}
    total = client.get("/api/analytics", headers=admin).json()["groups"][0]
    assert total["shipments"] == result["shipments"]
    assert 0 <= (total["on_time_pct"] or 0) <= 100
    assert client.get("/api/analytics?group_by=lane", headers=admin).status_code == 200
    assert len(client.get("/api/orders", headers=admin).json()) == 2


def test_webhook_signed_delivery(app, client, admin):
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((dict(self.headers), body))
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        hook = client.post("/api/webhooks", json={"url": f"http://127.0.0.1:{srv.server_port}/hook",
                                                  "events": ["shipment.status_changed"]}, headers=admin).json()
        webhooks.deliver(app.state.db, [{"event": "shipment.status_changed", "shipment_ref": "S1", "to": "IN_TRANSIT"},
                                        {"event": "exception.opened", "shipment_ref": "S1"}])
        assert len(received) == 1
        headers, body = received[0]
        expected = "sha256=" + hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-Waypoint-Signature"] == expected
        assert json.loads(body)["to"] == "IN_TRANSIT"
    finally:
        srv.shutdown()


def test_webhook_failure_is_logged_not_raised(app, client, admin):
    client.post("/api/webhooks", json={"url": "http://127.0.0.1:9/nothing", "events": ["exception.opened"]}, headers=admin)
    webhooks.deliver(app.state.db, [{"event": "exception.opened", "shipment_ref": "X"}])
    row = app.state.db.one("SELECT * FROM webhook_deliveries")
    assert row["error"] and row["status_code"] is None


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Waypoint" in r.text
    assert r.headers["x-frame-options"] == "DENY"
