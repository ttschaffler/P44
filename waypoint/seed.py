"""Generate realistic demo data across all six modes.

Usage: python -m waypoint.seed [--reset]   (uses WAYPOINT_DB)
"""
import argparse
import os
import random
from datetime import datetime, timedelta

from . import tracking
from .config import get_settings
from .db import Database
from .eta import PROFILES, haversine_km
from .timeutil import iso, utcnow

PLACES = {
    "Chicago, IL": (41.88, -87.63), "Detroit, MI": (42.33, -83.05), "Dallas, TX": (32.78, -96.80),
    "Atlanta, GA": (33.75, -84.39), "Memphis, TN": (35.15, -90.05), "Columbus, OH": (39.96, -83.00),
    "Indianapolis, IN": (39.77, -86.16), "Nashville, TN": (36.16, -86.78), "Denver, CO": (39.74, -104.99),
    "Kansas City, MO": (39.10, -94.58), "Los Angeles, CA": (34.05, -118.24), "Phoenix, AZ": (33.45, -112.07),
    "Port of Los Angeles": (33.74, -118.27), "Port of Savannah": (32.08, -81.09),
    "Port of Shanghai": (31.23, 121.47), "Port of Rotterdam": (51.95, 4.14), "Port of Singapore": (1.26, 103.84),
    "Port of Busan": (35.10, 129.04), "Hamburg, DE": (53.55, 9.99),
    "FRA Frankfurt Airport": (50.04, 8.56), "ORD O'Hare Airport": (41.97, -87.91), "PVG Shanghai Pudong": (31.14, 121.81),
    "MEM Memphis Airport": (35.04, -89.98), "Newark, NJ": (40.74, -74.17), "Charlotte, NC": (35.23, -80.84),
    "Seattle, WA": (47.61, -122.33), "Portland, OR": (45.52, -122.68),
}

CARRIERS = [
    ("NRTH", "Northline Freight", ["TL", "LTL"]),
    ("BLUE", "Blue Ridge Haulers", ["TL"]),
    ("PKLT", "Packlite Express", ["PARCEL"]),
    ("TIDE", "Tidewater Ocean Lines", ["OCEAN"]),
    ("SKYC", "Skycargo Partners", ["AIR"]),
    ("IRON", "Ironway Rail", ["RAIL"]),
    ("CRSD", "Crossdock Consolidated", ["LTL", "PARCEL"]),
]

LANES = {
    "TL": [["Chicago, IL", "Detroit, MI"], ["Dallas, TX", "Atlanta, GA"], ["Columbus, OH", "Indianapolis, IN", "Chicago, IL"],
           ["Denver, CO", "Kansas City, MO"], ["Los Angeles, CA", "Phoenix, AZ"], ["Seattle, WA", "Portland, OR"]],
    "LTL": [["Memphis, TN", "Nashville, TN", "Atlanta, GA"], ["Chicago, IL", "Columbus, OH"], ["Charlotte, NC", "Atlanta, GA"]],
    "PARCEL": [["Memphis, TN", "Dallas, TX"], ["Newark, NJ", "Charlotte, NC"], ["Indianapolis, IN", "Detroit, MI"]],
    "OCEAN": [["Port of Shanghai", "Port of Busan", "Port of Los Angeles"], ["Port of Singapore", "Port of Rotterdam"],
              ["Port of Rotterdam", "Port of Savannah"]],
    "AIR": [["PVG Shanghai Pudong", "ORD O'Hare Airport"], ["FRA Frankfurt Airport", "MEM Memphis Airport"]],
    "RAIL": [["Los Angeles, CA", "Kansas City, MO", "Chicago, IL"], ["Seattle, WA", "Denver, CO"]],
}

RAW = {  # how different carriers phrase the same milestone, to exercise normalization
    "TL": {"PICKED_UP": ["AF", "Picked up"], "IN_TRANSIT": ["X6", "In transit"], "AT_STOP": ["X1", "Arrived"],
           "OUT_FOR_DELIVERY": ["Out for delivery"], "DELIVERED": ["D1", "Delivered"]},
    "LTL": {"PICKED_UP": ["Picked up"], "IN_TRANSIT": ["Departed facility"], "AT_STOP": ["At cross dock"],
            "OUT_FOR_DELIVERY": ["Out for delivery"], "DELIVERED": ["POD"]},
    "PARCEL": {"PICKED_UP": ["Origin scan"], "IN_TRANSIT": ["On the way"], "AT_STOP": ["Arrived at hub"],
               "OUT_FOR_DELIVERY": ["OFD"], "DELIVERED": ["Delivered, front door"]},
    "OCEAN": {"PICKED_UP": ["Gate in full"], "IN_TRANSIT": ["Vessel departed"], "AT_STOP": ["Vessel arrived"],
              "OUT_FOR_DELIVERY": ["Gate out full"], "DELIVERED": ["Empty returned"]},
    "AIR": {"PICKED_UP": ["RCS"], "IN_TRANSIT": ["DEP"], "AT_STOP": ["ARR"], "OUT_FOR_DELIVERY": ["NFD"],
            "DELIVERED": ["DLV"]},
    "RAIL": {"PICKED_UP": ["Loaded"], "IN_TRANSIT": ["Train departed"], "AT_STOP": ["Rail arrived"],
             "OUT_FOR_DELIVERY": ["Available for pickup"], "DELIVERED": ["Delivered"]},
}

EVENT_EVERY_H = {"TL": 1, "LTL": 3, "PARCEL": 3, "OCEAN": 24, "AIR": 3, "RAIL": 8}


def _plan_hours(mode: str, names: list[str]) -> float:
    prof = PROFILES[mode]
    pts = [PLACES[n] for n in names]
    km = sum(haversine_km(*a, *b) for a, b in zip(pts, pts[1:]))
    return km / prof.speed_kmh + (len(names) - 2) * prof.dwell_h + prof.terminal_h


def _interp(names: list[str], frac: float) -> tuple[float, float, int]:
    """Position at a fraction of total route distance; returns (lat, lon, index of last passed stop)."""
    pts = [PLACES[n] for n in names]
    legs = [haversine_km(*a, *b) for a, b in zip(pts, pts[1:])]
    target = sum(legs) * min(max(frac, 0), 1)
    for i, leg in enumerate(legs):
        if target <= leg or i == len(legs) - 1:
            f = 0 if leg == 0 else min(target / leg, 1)
            a, b = pts[i], pts[i + 1]
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, i
        target -= leg
    return pts[-1][0], pts[-1][1], len(pts) - 1


def _events(rng, ref, mode, names, start: datetime, actual_h: float, now: datetime, stale_after: float | None):
    raw = RAW[mode]
    evs = [(start, rng.choice(raw["PICKED_UP"]), PLACES[names[0]], 0)]
    end = start + timedelta(hours=actual_h)
    ofd = end - timedelta(hours=max(actual_h * 0.05, 0.5))
    step = timedelta(hours=EVENT_EVERY_H[mode])
    t, last_stop = start + step, 0
    cutoff = now if stale_after is None else min(now, start + timedelta(hours=actual_h * stale_after))
    while t < min(ofd, cutoff):
        frac = (t - start) / (end - start)
        lat, lon, idx = _interp(names, frac)
        if 0 < idx != last_stop and idx < len(names) - 1:
            evs.append((t, rng.choice(raw["AT_STOP"]), PLACES[names[idx]], idx))
            last_stop = idx
        evs.append((t, rng.choice(raw["IN_TRANSIT"]), (lat + rng.uniform(-.05, .05), lon + rng.uniform(-.05, .05)), None))
        t += step
    if end <= cutoff:
        evs.append((ofd, rng.choice(raw["OUT_FOR_DELIVERY"]), PLACES[names[-1]], None))
        evs.append((end, rng.choice(raw["DELIVERED"]), PLACES[names[-1]], len(names) - 1))
    return [{"shipment_ref": ref, "timestamp": iso(ts), "status": st, "lat": round(p[0], 5), "lon": round(p[1], 5),
             "stop_sequence": seq, "source": "seed"} for ts, st, p, seq in evs if ts <= now]


def seed(db: Database, now: datetime | None = None, n_per_mode: int = 6, rng_seed: int = 44) -> dict:
    now = now or utcnow()
    rng = random.Random(rng_seed)
    for code, name, modes in CARRIERS:
        if not db.one("SELECT 1 FROM carriers WHERE code=?", (code,)):
            tracking.create_carrier(db, code, name, modes)
    by_mode = {m: [c for c, _, ms in CARRIERS if m in ms] for m in LANES}
    created, events = 0, []

    def make(ref, mode, names, pickup, order_ref=None, delay=1.0, stale=None):
        nonlocal created
        plan_h = _plan_hours(mode, names) * 1.15
        tracking.create_shipment(db, {
            "ref": ref, "mode": mode, "carrier": rng.choice(by_mode[mode]), "order_ref": order_ref,
            "planned_pickup": iso(pickup), "planned_delivery": iso(pickup + timedelta(hours=plan_h)),
            "stops": [{"name": n, "lat": PLACES[n][0], "lon": PLACES[n][1]} for n in names],
        }, now)
        created += 1
        start = pickup + timedelta(minutes=rng.randint(0, 90))
        if start < now:
            events.extend(_events(rng, ref, mode, names, start, plan_h / 1.15 * delay, now, stale))
        return pickup + timedelta(hours=plan_h * delay)

    for mode, lanes in LANES.items():
        typical = _plan_hours(mode, lanes[0])
        for i in range(n_per_mode):
            names = lanes[i % len(lanes)]
            # Spread pickups from well in the past (delivered) to the future (planned).
            offset_h = typical * rng.uniform(-2.2, 0.6)
            pickup = (now + timedelta(hours=offset_h)).replace(minute=0, second=0)
            roll = rng.random()
            delay = rng.uniform(1.3, 1.8) if roll < 0.2 else rng.uniform(0.85, 1.08)
            stale = rng.uniform(0.3, 0.7) if 0.2 <= roll < 0.3 else None
            make(f"{mode}-{1000 + i}", mode, names, pickup, delay=delay, stale=stale)

    # Two multimodal orders stitched across legs.
    tracking.create_order(db, "PO-77001", "Lakeshore Appliances", now)
    t = now - timedelta(days=12)
    t = make("PO77001-OCN", "OCEAN", ["Port of Shanghai", "Port of Busan", "Port of Los Angeles"], t, "PO-77001")
    t = make("PO77001-RAIL", "RAIL", ["Los Angeles, CA", "Kansas City, MO", "Chicago, IL"], t + timedelta(hours=30), "PO-77001")
    make("PO77001-TL", "TL", ["Chicago, IL", "Detroit, MI"], t + timedelta(hours=8), "PO-77001")

    tracking.create_order(db, "PO-77002", "Rhine Medical Supply", now)
    t = now - timedelta(hours=30)
    t = make("PO77002-AIR", "AIR", ["FRA Frankfurt Airport", "MEM Memphis Airport"], t, "PO-77002")
    make("PO77002-PCL", "PARCEL", ["Memphis, TN", "Dallas, TX"], t + timedelta(hours=4), "PO-77002")

    events.sort(key=lambda e: e["timestamp"])
    result = tracking.ingest(db, events, now)
    tracking.sweep(db, now)
    return {"shipments": created, "events": result["accepted"], "quarantined": len(result["quarantined"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the database file first")
    args = ap.parse_args()
    path = get_settings().db_path
    if args.reset and path != ":memory:" and os.path.exists(path):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    db = Database(path)
    if db.one("SELECT 1 FROM shipments LIMIT 1"):
        print("database already has shipments; use --reset to reseed")
        return
    print(seed(db))


if __name__ == "__main__":
    main()
