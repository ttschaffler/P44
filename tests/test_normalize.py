from datetime import datetime, timezone

import pytest

from waypoint.normalize import Rejected, canonical_status, normalize

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw,expected", [
    ("AF", "PICKED_UP"), ("Picked up", "PICKED_UP"), ("gate-in full", "PICKED_UP"), ("RCS", "PICKED_UP"),
    ("X6", "IN_TRANSIT"), ("Vessel departed", "IN_TRANSIT"), ("DEP", "IN_TRANSIT"),
    ("ARR", "AT_STOP"), ("Arrived at hub", "AT_STOP"), ("X1", "AT_STOP"),
    ("OFD", "OUT_FOR_DELIVERY"), ("Gate out full", "OUT_FOR_DELIVERY"),
    ("D1", "DELIVERED"), ("Delivered, front door", "DELIVERED"), ("DLV", "DELIVERED"),
    ("Canceled", "CANCELLED"), ("in_transit", "IN_TRANSIT"),
])
def test_status_aliases(raw, expected):
    assert canonical_status(raw) == expected


def test_unknown_status_is_none():
    assert canonical_status("weather looks nice") is None
    assert canonical_status("") is None


def base(**kw):
    return {"shipment_ref": "S1", "timestamp": "2026-10-14T10:00:00Z", "status": "X6", "lat": 41.0, "lon": -87.0, **kw}


def test_normalize_ok():
    ev = normalize(base(), NOW)
    assert ev.status == "IN_TRANSIT" and ev.raw_status == "X6" and ev.lat == 41.0


def test_fingerprint_stable_and_distinct():
    assert normalize(base(), NOW).fingerprint == normalize(base(), NOW).fingerprint
    assert normalize(base(), NOW).fingerprint != normalize(base(lat=41.1), NOW).fingerprint


@pytest.mark.parametrize("override,reason", [
    ({"shipment_ref": ""}, "missing shipment_ref"),
    ({"status": "???"}, "unrecognized status"),
    ({"timestamp": "yesterday"}, "timestamp"),
    ({"timestamp": "2026-10-15T10:00:00Z"}, "future"),
    ({"lat": 95}, "out of range"),
    ({"lon": None}, "together"),
    ({"lat": 0, "lon": 0}, "null-island"),
    ({"lat": "abc"}, "not a number"),
])
def test_rejections(override, reason):
    with pytest.raises(Rejected, match=reason):
        normalize(base(**override), NOW)
