from datetime import datetime, timedelta, timezone

from waypoint.eta import PROFILES, BaselineEta, RoutePoint, haversine_km
from waypoint.rules import evaluate_conditions
from waypoint.timeutil import iso

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
CHI, DET = (41.88, -87.63), (42.33, -83.05)


def test_haversine_chicago_detroit():
    assert 370 < haversine_km(*CHI, *DET) < 390


def test_eta_planned_starts_at_planned_pickup():
    stops = [RoutePoint(*CHI, False), RoutePoint(*DET, False)]
    pickup = NOW + timedelta(hours=5)
    eta = BaselineEta().predict("TL", "PLANNED", None, stops, pickup, NOW)
    expected_h = haversine_km(*CHI, *DET) / PROFILES["TL"].speed_kmh
    assert abs((eta - pickup).total_seconds() / 3600 - expected_h) < 0.01


def test_eta_in_transit_uses_position_and_dwell():
    mid = (42.1, -85.3)
    stops = [RoutePoint(*CHI, True), RoutePoint(42.0, -84.0, False), RoutePoint(*DET, False)]
    eta = BaselineEta().predict("LTL", "IN_TRANSIT", mid, stops, NOW - timedelta(days=1), NOW)
    km = haversine_km(*mid, 42.0, -84.0) + haversine_km(42.0, -84.0, *DET)
    expected = km / PROFILES["LTL"].speed_kmh + PROFILES["LTL"].dwell_h
    assert abs((eta - NOW).total_seconds() / 3600 - expected) < 0.01


def test_eta_ocean_adds_terminal_handling():
    stops = [RoutePoint(31.23, 121.47, True), RoutePoint(33.74, -118.27, False)]
    eta = BaselineEta().predict("OCEAN", "IN_TRANSIT", (33.0, -125.0), stops, NOW, NOW)
    assert (eta - NOW).total_seconds() / 3600 > PROFILES["OCEAN"].terminal_h


def sh(**kw):
    base = {"mode": "TL", "status": "IN_TRANSIT", "planned_pickup": iso(NOW - timedelta(hours=10)),
            "planned_delivery": iso(NOW + timedelta(hours=5)), "eta": iso(NOW + timedelta(hours=4)),
            "last_event_at": iso(NOW - timedelta(hours=1)), "status_since": iso(NOW - timedelta(hours=1))}
    return {**base, **kw}


def test_no_exceptions_when_healthy():
    assert evaluate_conditions(sh(), NOW) == {}


def test_late_and_severity():
    assert evaluate_conditions(sh(eta=iso(NOW + timedelta(hours=8))), NOW)["LATE"][0] == "medium"
    assert evaluate_conditions(sh(eta=iso(NOW + timedelta(hours=20))), NOW)["LATE"][0] == "high"


def test_late_tolerance_respected():
    assert "LATE" not in evaluate_conditions(sh(eta=iso(NOW + timedelta(hours=6.5))), NOW)


def test_stale_tracking_depends_on_mode():
    s = sh(last_event_at=iso(NOW - timedelta(hours=6)))
    assert "STALE_TRACKING" in evaluate_conditions(s, NOW)
    assert "STALE_TRACKING" not in evaluate_conditions({**s, "mode": "OCEAN"}, NOW)


def test_long_dwell():
    s = sh(status="AT_STOP", status_since=iso(NOW - timedelta(hours=5)), last_event_at=iso(NOW - timedelta(hours=1)))
    assert "LONG_DWELL" in evaluate_conditions(s, NOW)


def test_missed_pickup():
    s = sh(status="PLANNED", planned_pickup=iso(NOW - timedelta(hours=3)), last_event_at=None)
    assert set(evaluate_conditions(s, NOW)) == {"MISSED_PICKUP"}


def test_terminal_shipments_have_no_exceptions():
    assert evaluate_conditions(sh(status="DELIVERED", eta=iso(NOW + timedelta(days=3))), NOW) == {}
