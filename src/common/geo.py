"""
DAH 2026 공용 지리 계산 (중립 수학, 공유 OK).
sim(트랙 A)의 스푸핑 오프셋 계산, blue(policy)의 발산 거리 계산에 모두 쓰인다.
"""
from __future__ import annotations

import math

from .wire import Position

_EARTH_R = 6_371_000.0  # m


def haversine_m(a: Position, b: Position) -> float:
    """두 좌표 간 수평 거리(m)."""
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dphi = math.radians(b.lat - a.lat)
    dlmb = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def offset_m(pos: Position, north_m: float, east_m: float) -> Position:
    """pos에서 북/동으로 미터만큼 이동한 좌표(소각 근사)."""
    dlat = north_m / _EARTH_R
    dlon = east_m / (_EARTH_R * math.cos(math.radians(pos.lat)))
    return Position(
        lat=pos.lat + math.degrees(dlat),
        lon=pos.lon + math.degrees(dlon),
        alt_m=pos.alt_m,
    )


def north_east_m(frm: Position, to: Position) -> tuple[float, float]:
    """frm→to 의 로컬 ENU 변위(북 m, 동 m). offset_m 의 역연산(등거리 근사)."""
    north = math.radians(to.lat - frm.lat) * _EARTH_R
    east = math.radians(to.lon - frm.lon) * _EARTH_R * math.cos(math.radians(frm.lat))
    return north, east
