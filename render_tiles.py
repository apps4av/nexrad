"""Render a static NEXRAD tile pyramid for GitHub Pages.

Instead of serving tiles on demand, this pre-renders a bounded set of
`{z}/{x}/{y}.png` tiles (optionally several animation frames) into an output
directory that can be published to GitHub Pages. Run it on a schedule with
GitHub Actions so the published tiles stay fresh.

It reuses the same rendering logic as the live server (`nexrad.radar`), so the
tiles look identical -- they're just written to disk instead of streamed.

Configuration (environment variables):
  NEXRAD_OUT          output dir (default: _site)
  NEXRAD_PRODUCT      bref_qcd | cref_qcd (default: bref_qcd)
  NEXRAD_MIN_ZOOM     min zoom (default: 0)
  NEXRAD_MAX_ZOOM     max zoom (default: 6)
  NEXRAD_FRAMES       number of animation frames (default: 6; 1 = latest only)
  NEXRAD_WINDOW_MIN   animation window in minutes (default: 60)
  NEXRAD_CONCURRENCY  max concurrent tile renders (default: 12)
  NEXRAD_WEBAPP       path to client index.html to copy (default: webapp/index.html)
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import shutil
import time
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

import httpx
from PIL import Image

from nexrad import config, radar

OUT = os.environ.get("NEXRAD_OUT", "_site")
PRODUCT = os.environ.get("NEXRAD_PRODUCT", config.DEFAULT_PRODUCT)
MIN_ZOOM = int(os.environ.get("NEXRAD_MIN_ZOOM", "0"))
MAX_ZOOM = int(os.environ.get("NEXRAD_MAX_ZOOM", "6"))
FRAMES = int(os.environ.get("NEXRAD_FRAMES", "6"))
WINDOW_MIN = int(os.environ.get("NEXRAD_WINDOW_MIN", "60"))
CONCURRENCY = int(os.environ.get("NEXRAD_CONCURRENCY", "12"))
WEBAPP = os.environ.get("NEXRAD_WEBAPP", os.path.join(os.path.dirname(__file__), "webapp", "index.html"))

MERC_LAT_LIMIT = 85.05112878


def _lon2x(lon: float, z: int) -> int:
    return int((lon + 180.0) / 360.0 * (2 ** z))


def _lat2y(lat: float, z: int) -> int:
    lat = max(min(lat, MERC_LAT_LIMIT), -MERC_LAT_LIMIT)
    r = math.radians(lat)
    return int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * (2 ** z))


def _tiles_for_zoom(z: int) -> Set[Tuple[int, int]]:
    """Set of (x, y) tiles at zoom z that cover any US radar region."""
    n = 2 ** z
    tiles: Set[Tuple[int, int]] = set()
    for region in config.REGIONS.values():
        w, s, e, nth = region.bounds
        x0, x1 = _lon2x(w, z), _lon2x(e, z)
        y0, y1 = _lat2y(nth, z), _lat2y(s, z)  # north -> smaller y
        for x in range(max(0, min(x0, x1)), min(n - 1, max(x0, x1)) + 1):
            for y in range(max(0, min(y0, y1)), min(n - 1, max(y0, y1)) + 1):
                tiles.add((x, y))
    return tiles


def _is_empty(data: bytes) -> bool:
    try:
        im = Image.open(io.BytesIO(data))
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        return im.getchannel("A").getextrema()[1] == 0
    except Exception:
        return False


async def _render_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    frame_dir: str,
    z: int,
    x: int,
    y: int,
    iso: Optional[str],
    counters: dict,
) -> None:
    async with sem:
        data = await radar.render_tile(client, z, x, y, PRODUCT, iso)
    if _is_empty(data):
        counters["empty"] += 1
        return
    d = os.path.join(frame_dir, str(z), str(x))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{y}.png"), "wb") as f:
        f.write(data)
    counters["written"] += 1


async def _resolve_frames(client: httpx.AsyncClient) -> List[Optional[str]]:
    if FRAMES <= 1:
        return [None]  # latest only
    times = await radar.frame_times(client, WINDOW_MIN, FRAMES, PRODUCT)
    return times if times else [None]


async def main() -> None:
    started = time.time()
    tiles_dir = os.path.join(OUT, "tiles")
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    os.makedirs(OUT, exist_ok=True)

    limits = httpx.Limits(max_connections=CONCURRENCY * 4, max_keepalive_connections=CONCURRENCY * 2)
    async with httpx.AsyncClient(
        limits=limits,
        headers={"User-Agent": "nexrad-tile-renderer/1.0"},
        follow_redirects=True,
    ) as client:
        frames = await _resolve_frames(client)
        sem = asyncio.Semaphore(CONCURRENCY)
        counters = {"written": 0, "empty": 0}

        zoom_tiles = {z: _tiles_for_zoom(z) for z in range(MIN_ZOOM, MAX_ZOOM + 1)}
        total_candidates = sum(len(t) for t in zoom_tiles.values()) * len(frames)
        print(
            f"Rendering product={PRODUCT} zooms={MIN_ZOOM}..{MAX_ZOOM} "
            f"frames={len(frames)} candidates={total_candidates} out={OUT}"
        )

        tasks = []
        frame_meta = []
        for fi, iso in enumerate(frames):
            frame_dir = os.path.join(tiles_dir, str(fi))
            frame_meta.append({"index": fi, "time": iso or "latest"})
            for z, tiles in zoom_tiles.items():
                for (x, y) in tiles:
                    tasks.append(_render_one(client, sem, frame_dir, z, x, y, iso, counters))

        # Run in chunks so a single gather isn't unbounded in memory.
        chunk = CONCURRENCY * 50
        for i in range(0, len(tasks), chunk):
            await asyncio.gather(*tasks[i:i + chunk])
            print(f"  progress: {min(i + chunk, len(tasks))}/{len(tasks)} "
                  f"(written={counters['written']} empty={counters['empty']})")

    manifest = {
        "product": PRODUCT,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "minZoom": MIN_ZOOM,
        "maxZoom": MAX_ZOOM,
        "tileSize": config.TILE_SIZE,
        "windowMinutes": WINDOW_MIN,
        "frames": frame_meta,
    }
    with open(os.path.join(OUT, "frames.json"), "w") as f:
        json.dump(manifest, f, indent=1)

    # Copy the client app as index.html and add .nojekyll for Pages.
    if os.path.isfile(WEBAPP):
        shutil.copyfile(WEBAPP, os.path.join(OUT, "index.html"))
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    elapsed = time.time() - started
    print(
        f"Done in {elapsed:.1f}s | tiles written={counters['written']} "
        f"skipped_empty={counters['empty']} | manifest -> {OUT}/frames.json"
    )


if __name__ == "__main__":
    asyncio.run(main())
