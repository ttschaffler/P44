"""Baseline ETA engine: remaining route distance / mode speed + dwell at remaining stops.

Deliberately simple and explainable. Anything implementing `EtaModel.predict` can replace it.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ModeProfile:
    speed_kmh: float          # average door-to-door moving speed
    dwell_h: float            # dwell per remaining intermediate stop
    terminal_h: float         # fixed handling before final delivery (port/airport)
    late_tolerance_h: float   # how late before it counts as late
    stale_after_h: float      # no event for this long = stale tracking
    long_dwell_h: float       # AT_STOP longer than this = long dwell
    arrival_radius_km: float  # position within this of a stop counts as arriving there


PROFILES = {
    "TL":     ModeProfile(65, 1, 0, 2, 4, 4, 3),
    "LTL":    ModeProfile(45, 6, 0, 4, 12, 18, 3),
    "PARCEL": ModeProfile(40, 0.5, 0, 4, 24, 24, 5),
    "OCEAN":  ModeProfile(28, 36, 24, 24, 48, 120, 30),
    "AIR":    ModeProfile(650, 6, 6, 6, 12, 24, 25),
    "RAIL":   ModeProfile(35, 12, 0, 12, 24, 48, 10),
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class RoutePoint:
    lat: float
    lon: float
    visited: bool


class EtaModel(Protocol):
    def predict(self, mode: str, status: str, position: tuple[float, float] | None,
                stops: Sequence[RoutePoint], planned_pickup: datetime, now: datetime) -> datetime: ...


class BaselineEta:
    def predict(self, mode, status, position, stops, planned_pickup, now):
        prof = PROFILES[mode]
        remaining = [s for s in stops if not s.visited]
        if not remaining:
            return now
        if status == "PLANNED":
            # Not moving yet: leave the origin at the later of now and planned pickup.
            start = max(now, planned_pickup)
            cursor = (stops[0].lat, stops[0].lon)
            remaining = [s for s in stops[1:] if not s.visited]
        elif position is None:
            visited = [s for s in stops if s.visited]
            anchor = visited[-1] if visited else stops[0]
            start, cursor = now, (anchor.lat, anchor.lon)
        else:
            start, cursor = now, position
        km = 0.0
        for s in remaining:
            km += haversine_km(cursor[0], cursor[1], s.lat, s.lon)
            cursor = (s.lat, s.lon)
        intermediate = max(len(remaining) - 1, 0)
        hours = km / prof.speed_kmh + intermediate * prof.dwell_h
        if status != "OUT_FOR_DELIVERY":
            hours += prof.terminal_h
        return start + timedelta(hours=hours)


default_model: EtaModel = BaselineEta()
