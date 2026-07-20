"""Fetch NEXRAD reflectivity from NOAA WMS and composite into XYZ tiles."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from PIL import Image

from . import config
from .mercator import bbox_intersects, tile_bounds_3857, tile_bounds_lonlat

logger = logging.getLogger("nexrad.radar")

# A single fully-transparent tile, reused as a fallback.
_TRANSPARENT_TILE: Optional[bytes] = None


def transparent_tile() -> bytes:
    """Return the bytes of a fully-transparent PNG tile (cached)."""
    global _TRANSPARENT_TILE
    if _TRANSPARENT_TILE is None:
        img = Image.new("RGBA", (config.TILE_SIZE, config.TILE_SIZE), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _TRANSPARENT_TILE = buf.getvalue()
    return _TRANSPARENT_TILE


def _wms_params(layer: str, bounds_3857, time: Optional[str] = None) -> dict:
    minx, miny, maxx, maxy = bounds_3857
    params = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetMap",
        "layers": layer,
        "styles": "",
        "crs": "EPSG:3857",
        # WMS 1.3.0 EPSG:3857 uses x,y (easting, northing) order.
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "width": config.TILE_SIZE,
        "height": config.TILE_SIZE,
        "format": "image/png",
        "transparent": "true",
    }
    if time:
        # nearestValue=1 on the layer means the server snaps to the closest
        # available frame, so this works across regions with slightly different
        # scan times.
        params["time"] = time
    return params


async def _fetch_region(
    client: httpx.AsyncClient,
    region: config.Region,
    product: str,
    bounds_3857,
    time: Optional[str] = None,
) -> Optional[Image.Image]:
    """Fetch one region's reflectivity for a tile bbox. None on failure."""
    layer = config.layer_name(region, product)
    url = config.wms_endpoint(region)
    params = _wms_params(layer, bounds_3857, time)
    try:
        resp = await client.get(url, params=params, timeout=config.UPSTREAM_TIMEOUT)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "image/png" not in ctype:
            # GeoServer returns a text/xml ServiceException on error.
            logger.warning(
                "region=%s non-image response (%s): %s",
                region.key, ctype, resp.text[:200],
            )
            return None
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - upstream can fail many ways
        logger.warning("region=%s fetch failed: %s", region.key, exc)
        return None


def _regions_for_tile(z: int, x: int, y: int) -> List[config.Region]:
    """Regions whose extent overlaps the given tile."""
    tile_ll = tile_bounds_lonlat(z, x, y)
    return [r for r in config.REGIONS.values() if bbox_intersects(tile_ll, r.bounds)]


async def render_tile_ex(
    client: httpx.AsyncClient,
    z: int,
    x: int,
    y: int,
    product: str = config.DEFAULT_PRODUCT,
    time: Optional[str] = None,
) -> "tuple[bytes, bool]":
    """Render a tile and report whether the render was *complete*.

    Returns ``(png_bytes, complete)`` where ``complete`` is ``True`` only if
    every overlapping region responded successfully (or no region overlaps the
    tile at all). When ``complete`` is ``False``, at least one upstream fetch
    failed, so a transparent result must NOT be trusted as "no radar here" --
    callers doing quadtree pruning should keep this tile's children as
    candidates rather than assume they're empty too.
    """
    regions = _regions_for_tile(z, x, y)
    if not regions:
        # Nothing is expected here; a transparent tile is authoritative.
        return transparent_tile(), True

    bounds_3857 = tile_bounds_3857(z, x, y)
    results = await asyncio.gather(
        *(_fetch_region(client, r, product, bounds_3857, time) for r in regions)
    )
    complete = all(img is not None for img in results)

    layers = [img for img in results if img is not None]
    if not layers:
        return transparent_tile(), complete

    base = Image.new("RGBA", (config.TILE_SIZE, config.TILE_SIZE), (0, 0, 0, 0))
    for img in layers:
        if img.size != (config.TILE_SIZE, config.TILE_SIZE):
            img = img.resize((config.TILE_SIZE, config.TILE_SIZE))
        base = Image.alpha_composite(base, img)

    buf = io.BytesIO()
    base.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), complete


async def render_tile(
    client: httpx.AsyncClient,
    z: int,
    x: int,
    y: int,
    product: str = config.DEFAULT_PRODUCT,
    time: Optional[str] = None,
) -> bytes:
    """Render a composited NEXRAD PNG tile for the given XYZ address.

    Queries every US radar region that overlaps the tile, in parallel, and
    alpha-composites the results. Regions are spatially disjoint, so ordering
    does not matter. If ``time`` (ISO8601) is given, that historical frame is
    requested (the server snaps to the nearest available scan). Returns a
    transparent tile if nothing overlaps or all upstream requests fail.
    """
    data, _ = await render_tile_ex(client, z, x, y, product, time)
    return data


# --- Animation frame times ------------------------------------------------

_DIMENSION_RE = re.compile(
    r'<Dimension[^>]*name="time"[^>]*>(.*?)</Dimension>',
    re.IGNORECASE | re.DOTALL,
)

# Cache the parsed capabilities time list briefly so /frames doesn't hit
# upstream on every request. (timestamp, list-of-iso-strings)
_FRAMES_CACHE: Optional[tuple] = None
_FRAMES_CACHE_TTL = 30.0


def _parse_iso(ts: str) -> Optional[datetime]:
    ts = ts.strip()
    if not ts:
        return None
    # Normalize to a form datetime.fromisoformat accepts.
    t = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _all_frame_times(client: httpx.AsyncClient, product: str) -> List[datetime]:
    """Fetch and parse the full list of available time steps for a product.

    Uses the CONUS layer as the canonical clock; other regions snap to the
    nearest scan via the WMS ``nearestValue`` behavior.
    """
    global _FRAMES_CACHE
    import time as _time

    now = _time.time()
    if _FRAMES_CACHE is not None:
        ts, cached_product, frames = _FRAMES_CACHE
        if cached_product == product and (now - ts) < _FRAMES_CACHE_TTL:
            return frames

    layer = f"conus_{product}"
    url = f"{config.GEOSERVER_BASE}/conus/{layer}/ows"
    params = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetCapabilities",
    }
    resp = await client.get(url, params=params, timeout=config.UPSTREAM_TIMEOUT)
    resp.raise_for_status()
    match = _DIMENSION_RE.search(resp.text)
    frames: List[datetime] = []
    if match:
        for token in match.group(1).split(","):
            dt = _parse_iso(token)
            if dt is not None:
                frames.append(dt)
    frames.sort()
    _FRAMES_CACHE = (now, product, frames)
    return frames


async def frame_times(
    client: httpx.AsyncClient,
    minutes: int = 60,
    max_frames: int = 13,
    product: str = config.DEFAULT_PRODUCT,
) -> List[str]:
    """Return ISO8601 frame timestamps for the last ``minutes``.

    The available frames (~every 2 min) are filtered to the requested window and
    evenly subsampled to at most ``max_frames`` entries, always keeping the most
    recent frame. Returns strings like ``2026-07-19T22:30:00.000Z``.
    """
    all_frames = await _all_frame_times(client, product)
    if not all_frames:
        return []

    latest = all_frames[-1]
    cutoff = latest - timedelta(minutes=minutes)
    window = [dt for dt in all_frames if dt >= cutoff]
    if not window:
        window = [latest]

    if max_frames > 0 and len(window) > max_frames:
        # Evenly sample indices across the window, keeping first and last.
        n = len(window)
        step = (n - 1) / (max_frames - 1) if max_frames > 1 else 0
        idx = sorted({round(i * step) for i in range(max_frames)})
        window = [window[i] for i in idx]

    return [
        dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") for dt in window
    ]
