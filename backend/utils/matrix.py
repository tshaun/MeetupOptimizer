from __future__ import annotations

import math
from typing import List, Sequence


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two lat/lon points."""
    R = 6371.0  # km
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def compute_matrix_minutes(
    venues: Sequence[dict], *, speed_kmh: float = 3
) -> List[List[float]]:
    """
    Compute a symmetric NxN matrix of travel time minutes using haversine distance
    and a constant speed model. Diagonal = 0.
    """
    n = len(venues)
    if n == 0:
        return []
    m: List[List[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        lat1 = float(venues[i]["lat"])  # type: ignore[index]
        lon1 = float(venues[i]["lon"])  # type: ignore[index]
        for j in range(i + 1, n):
            lat2 = float(venues[j]["lat"])  # type: ignore[index]
            lon2 = float(venues[j]["lon"])  # type: ignore[index]
            km = haversine_km(lat1, lon1, lat2, lon2)
            minutes = (km / max(speed_kmh, 1e-6)) * 60.0
            # Clamp to a reasonable upper bound to avoid extreme values
            minutes = max(0.0, min(minutes, 180.0))
            m[i][j] = m[j][i] = round(minutes, 1)
    return m
