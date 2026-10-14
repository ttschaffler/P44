"""Turn heterogeneous carrier status updates into one canonical status set.

The vocabulary below is assembled from general logistics usage and public standards
(ANSI X12 214 shipment-status codes, IATA cargo message codes, common ocean/parcel wording).
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import iso, parse_ts

MODES = ("TL", "LTL", "PARCEL", "OCEAN", "AIR", "RAIL")
STATUSES = ("PLANNED", "PICKED_UP", "IN_TRANSIT", "AT_STOP", "OUT_FOR_DELIVERY", "DELIVERED", "CANCELLED")
ACTIVE_STATUSES = ("PICKED_UP", "IN_TRANSIT", "AT_STOP", "OUT_FOR_DELIVERY")
TERMINAL_STATUSES = ("DELIVERED", "CANCELLED")

# Progress rank: a newer event may never move a shipment backwards past pickup.
STATUS_RANK = {
    "PLANNED": 0, "PICKED_UP": 1, "IN_TRANSIT": 2, "AT_STOP": 2,
    "OUT_FOR_DELIVERY": 3, "DELIVERED": 4, "CANCELLED": 4,
}

_ALIASES = {
    "PLANNED": [
        "planned", "booked", "tendered", "label created", "shipment information received",
        "pre advice", "booking confirmed", "aa", "awaiting pickup", "created",
    ],
    "PICKED_UP": [
        "picked up", "pickup", "pu", "af", "collected", "departed origin", "received from shipper",
        "rcs", "gate in", "gate in full", "loaded", "x3", "origin scan", "accepted",
    ],
    "IN_TRANSIT": [
        "in transit", "en route", "x6", "departed", "dep", "departed facility", "vessel departed",
        "loaded on vessel", "rail departed", "train departed", "moving", "on the way", "transshipment departed",
        "p1", "man",
    ],
    "AT_STOP": [
        "at stop", "arrived", "arr", "arrived at facility", "x1", "vessel arrived", "discharged",
        "rail arrived", "at terminal", "arrived at hub", "rcf", "arrived at port", "transshipment arrived",
        "at cross dock", "x2",
    ],
    "OUT_FOR_DELIVERY": [
        "out for delivery", "ofd", "on vehicle for delivery", "gate out", "gate out full", "available for pickup",
        "nfd", "with delivery courier", "ag",
    ],
    "DELIVERED": [
        "delivered", "d1", "dlv", "pod", "proof of delivery", "empty returned", "completed", "received by consignee",
    ],
    "CANCELLED": ["cancelled", "canceled", "ca", "voided", "shipment cancelled"],
}

_LOOKUP = {alias: canon for canon, aliases in _ALIASES.items() for alias in aliases}


def _clean(raw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    return re.sub(r"\s+", " ", s)


def canonical_status(raw: str) -> str | None:
    s = _clean(raw)
    if not s:
        return None
    if s.upper().replace(" ", "_") in STATUSES:
        return s.upper().replace(" ", "_")
    if s in _LOOKUP:
        return _LOOKUP[s]
    # Longest alias contained in the text wins ("Shipment delivered to front door").
    best = None
    for alias, canon in _LOOKUP.items():
        if len(alias) > 3 and re.search(rf"\b{re.escape(alias)}\b", s):
            if best is None or len(alias) > len(best[0]):
                best = (alias, canon)
    return best[1] if best else None


@dataclass
class NormalizedEvent:
    shipment_ref: str
    ts: datetime
    status: str
    raw_status: str
    lat: float | None
    lon: float | None
    stop_seq: int | None
    source: str

    @property
    def fingerprint(self) -> str:
        key = f"{self.shipment_ref}|{iso(self.ts)}|{self.status}|{self.lat}|{self.lon}|{self.stop_seq}"
        return hashlib.sha256(key.encode()).hexdigest()


class Rejected(ValueError):
    pass


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        raise Rejected(f"coordinate {value!r} is not a number")


def normalize(record: dict, now: datetime) -> NormalizedEvent:
    """Validate and canonicalize one raw record; raise Rejected with a human-readable reason."""
    ref = str(record.get("shipment_ref") or "").strip()
    if not ref:
        raise Rejected("missing shipment_ref")
    raw_status = str(record.get("status") or "").strip()
    status = canonical_status(raw_status)
    if status is None:
        raise Rejected(f"unrecognized status {raw_status!r}")
    try:
        ts = parse_ts(str(record.get("timestamp") or ""))
    except ValueError:
        ts = None
    if ts is None:
        raise Rejected("missing or unparseable timestamp")
    if ts > now + timedelta(hours=1):
        raise Rejected("timestamp is in the future")
    lat, lon = _opt_float(record.get("lat")), _opt_float(record.get("lon"))
    if (lat is None) != (lon is None):
        raise Rejected("lat and lon must be given together")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise Rejected("coordinates out of range")
    if lat == 0 and lon == 0:
        raise Rejected("null-island coordinates (0,0)")
    seq = record.get("stop_sequence")
    try:
        seq = int(seq) if seq not in (None, "") else None
    except (TypeError, ValueError):
        raise Rejected(f"stop_sequence {seq!r} is not an integer")
    return NormalizedEvent(ref, ts, status, raw_status, lat, lon, seq, str(record.get("source") or "api"))
