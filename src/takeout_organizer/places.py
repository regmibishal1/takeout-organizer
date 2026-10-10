"""Place names for folders (--place-names), offline, from GeoNames data.

data/places.tsv lists every town and city with more than 15,000 people
(GeoNames, CC BY 4.0; made by packaging/make_places.py). A photo's place is
one of them within RADIUS_KM of where it was taken. Among those, the nearest
wins unless a much larger one is close too, so a photo by Tokyo Tower is
filed under Tokyo, not under the ward it stands in, while one taken in
Yokohama stays in Yokohama.
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Tuple

ATTRIBUTION = "Place names from GeoNames (geonames.org), CC BY 4.0"
RADIUS_KM = 30.0
# Distances under this count as this, so a place's own centre does not
# outweigh a city around it.
_MIN_KM = 1.0
_EARTH_KM = 6371.0
_KM_PER_DEGREE = math.pi * _EARTH_KM / 180

_DATA = os.path.join(os.path.dirname(__file__), 'data', 'places.tsv')
_cells: Optional[Dict[Tuple[int, int], List[Tuple[float, float, int, str]]]] = None


def _load() -> Dict[Tuple[int, int], List[Tuple[float, float, int, str]]]:
    """The places, by one-degree cell of latitude and longitude."""
    global _cells
    if _cells is None:
        cells: Dict[Tuple[int, int], List[Tuple[float, float, int, str]]] = {}
        with open(_DATA, encoding='utf-8') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                lat, lon, population, name = line.rstrip('\n').split('\t')
                place = (float(lat), float(lon), max(int(population), 1), name)
                cells.setdefault(_cell(place[0], place[1]), []).append(place)
        _cells = cells
    return _cells


def _cell(lat: float, lon: float) -> Tuple[int, int]:
    return math.floor(lat), math.floor(lon)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_KM * math.asin(math.sqrt(min(1.0, h)))


def place_name(lat: float, lon: float) -> Optional[str]:
    """The name of the town or city a photo taken at lat, lon belongs to, or
    None when none is within RADIUS_KM (or the position is not usable)."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    cells = _load()
    lat_span = math.ceil(RADIUS_KM / _KM_PER_DEGREE)
    cos_lat = math.cos(math.radians(min(abs(lat) + lat_span, 90)))
    lon_span = 180 if cos_lat < 1e-6 else min(180, math.ceil(RADIUS_KM / (_KM_PER_DEGREE * cos_lat)))
    row, column = _cell(lat, lon)
    best, best_score = None, 0.0
    columns = {(column + step + 180) % 360 - 180 for step in range(-lon_span, lon_span + 1)}
    for r in range(row - lat_span, row + lat_span + 1):
        for c in columns:
            for p_lat, p_lon, population, name in cells.get((r, c), ()):
                d = distance_km(lat, lon, p_lat, p_lon)
                if d > RADIUS_KM:
                    continue
                # Population over distance squared: the nearest place, unless a
                # far larger one is nearly as close.
                score = population / max(d, _MIN_KM) ** 2
                if score > best_score:
                    best, best_score = name, score
    return best
