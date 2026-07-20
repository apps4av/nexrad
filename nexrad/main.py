"""NEXRAD tile server.

Serves XYZ web-map tiles of NEXRAD radar reflectivity covering the
entire United States -- CONUS, Alaska, Hawaii, Puerto Rico/Caribbean and Guam --
built on demand from NOAA's public government radar imagery (WMS).

Endpoints:
  GET /tiles/{z}/{x}/{y}.png   -> a radar tile (optional ?product=bref_qcd|cref_qcd)
  GET /                        -> interactive demo map
  GET /healthz                 -> health check
  GET /stats                   -> cache statistics
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import config, radar
from .cache import TileCache

logging.basicConfig(
    level=os.environ.get("NEXRAD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("nexrad")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Maximum XYZ zoom served. Beyond this the radar mosaic has no more detail and
# we would just be hammering upstream, so we cap it.
MAX_ZOOM = int(os.environ.get("NEXRAD_MAX_ZOOM", "12"))

ALLOWED_PRODUCTS = {"bref_qcd", "cref_qcd"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    app.state.client = httpx.AsyncClient(
        limits=limits,
        headers={"User-Agent": "nexrad-tile-server/1.0 (+https://github.com/)"},
        follow_redirects=True,
    )
    app.state.cache = TileCache(config.CACHE_TTL_SECONDS, config.CACHE_MAX_ENTRIES)
    app.state.inflight: Dict[str, asyncio.Future] = {}
    logger.info(
        "NEXRAD tile server ready | product=%s tile=%dpx regions=%s",
        config.DEFAULT_PRODUCT, config.TILE_SIZE, ",".join(config.REGIONS),
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="NEXRAD Tile Server", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


async def _get_tile(
    app: FastAPI, z: int, x: int, y: int, product: str, time: Optional[str]
) -> bytes:
    """Return tile bytes, using cache and single-flight to avoid stampedes."""
    key = f"{product}/{time or 'latest'}/{z}/{x}/{y}"
    cache: TileCache = app.state.cache

    cached = cache.get(key)
    if cached is not None:
        return cached

    inflight: Dict[str, asyncio.Future] = app.state.inflight
    existing = inflight.get(key)
    if existing is not None:
        return await existing

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    inflight[key] = fut
    try:
        data = await radar.render_tile(app.state.client, z, x, y, product, time)
        cache.set(key, data)
        fut.set_result(data)
        return data
    except Exception as exc:  # noqa: BLE001
        fut.set_exception(exc)
        raise
    finally:
        inflight.pop(key, None)


@app.get("/tiles/{z}/{x}/{y}.png")
async def get_tile(
    z: int,
    x: int,
    y: int,
    product: str = Query(config.DEFAULT_PRODUCT),
    time: Optional[str] = Query(None, description="ISO8601 frame time; omit for latest"),
):
    if z < 0 or z > MAX_ZOOM:
        raise HTTPException(status_code=404, detail=f"zoom out of range (0..{MAX_ZOOM})")
    n = 2 ** z
    if not (0 <= x < n and 0 <= y < n):
        raise HTTPException(status_code=404, detail="tile x/y out of range")
    if product not in ALLOWED_PRODUCTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown product '{product}', allowed: {sorted(ALLOWED_PRODUCTS)}",
        )

    data = await _get_tile(app, z, x, y, product, time)
    # Historical frames are immutable; cache them hard. The latest frame gets a
    # short TTL so clients pick up new scans.
    max_age = 3600 if time else config.CACHE_TTL_SECONDS
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": f"public, max-age={max_age}",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/frames")
async def frames(
    minutes: int = Query(60, ge=1, le=180),
    max: int = Query(13, ge=1, le=120),
    product: str = Query(config.DEFAULT_PRODUCT),
):
    """List available radar frame timestamps for the last ``minutes``."""
    if product not in ALLOWED_PRODUCTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown product '{product}', allowed: {sorted(ALLOWED_PRODUCTS)}",
        )
    times = await radar.frame_times(app.state.client, minutes, max, product)
    return JSONResponse(
        {"product": product, "minutes": minutes, "count": len(times), "frames": times},
        headers={"Cache-Control": "public, max-age=30", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/stats")
async def stats():
    return JSONResponse(
        {
            "cache": app.state.cache.stats(),
            "product": config.DEFAULT_PRODUCT,
            "tile_size": config.TILE_SIZE,
            "max_zoom": MAX_ZOOM,
            "regions": list(config.REGIONS),
            "source": config.GEOSERVER_BASE,
        }
    )


@app.get("/")
async def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
