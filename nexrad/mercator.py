"""Web Mercator (EPSG:3857) <-> XYZ slippy-map tile math.

The tile scheme is the standard XYZ / slippy-map scheme: zoom level ``z`` has
``2**z`` tiles in each axis. Tiles are rendered at ``TILE_SIZE`` px square, which
does not change the geographic bounds of a tile -- only the render resolution.
"""

from __future__ import annotations

import math
from typing import Tuple

# Half the circumference of the Earth at the equator in Web Mercator meters.
# This is the max extent of EPSG:3857 in either direction from 0.
EARTH_RADIUS_M = 6378137.0
MERC_MAX = math.pi * EARTH_RADIUS_M  # 20037508.342789244


def tile_bounds_3857(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """Return the EPSG:3857 bounds (minx, miny, maxx, maxy) of an XYZ tile."""
    n = 2 ** z
    tile_span = (2.0 * MERC_MAX) / n
    minx = -MERC_MAX + x * tile_span
    maxx = minx + tile_span
    maxy = MERC_MAX - y * tile_span
    miny = maxy - tile_span
    return (minx, miny, maxx, maxy)


def merc_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    """Convert EPSG:3857 meters to (lon, lat) degrees."""
    lon = (x / MERC_MAX) * 180.0
    lat = math.degrees(2.0 * math.atan(math.exp((y / MERC_MAX) * math.pi)) - math.pi / 2.0)
    return (lon, lat)


def tile_bounds_lonlat(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """Return the geographic bounds (west, south, east, north) of an XYZ tile."""
    minx, miny, maxx, maxy = tile_bounds_3857(z, x, y)
    west, south = merc_to_lonlat(minx, miny)
    east, north = merc_to_lonlat(maxx, maxy)
    return (west, south, east, north)


def bbox_intersects(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    """Return True if two (west, south, east, north) boxes intersect."""
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    return not (ae < bw or aw > be or an < bs or as_ > bn)
