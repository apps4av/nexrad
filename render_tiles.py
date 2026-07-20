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
) -> Tuple[int, int, bool]:
    """Render+write one tile. Returns (x, y, keep_children).

    ``keep_children`` is True when this tile's z+1 children should still be
    rendered -- i.e. the tile has radar data, OR the fetch was incomplete (so we
    can't safely conclude the area is empty). Only a tile that is *empty AND
    completely fetched* prunes its subtree.
    """
    async with sem:
        data, complete = await radar.render_tile_ex(client, z, x, y, PRODUCT, iso)
    empty = _is_empty(data)
    if empty:
        counters["empty"] += 1
        # Keep children only if the emptiness is uncertain (a fetch failed).
        return x, y, (not complete)
    d = os.path.join(frame_dir, str(z), str(x))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{y}.png"), "wb") as f:
        f.write(data)
    counters["written"] += 1
    return x, y, True


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
        counters = {"written": 0, "empty": 0, "rendered": 0, "pruned": 0}

        # Region mask per zoom: the tiles that geographically cover any US
        # radar region. Quadtree pruning then further restricts each zoom to the
        # children of parents that actually had data.
        zoom_mask = {z: _tiles_for_zoom(z) for z in range(MIN_ZOOM, MAX_ZOOM + 1)}
        full_candidates = sum(len(t) for t in zoom_mask.values()) * len(frames)
        print(
            f"Rendering product={PRODUCT} zooms={MIN_ZOOM}..{MAX_ZOOM} "
            f"frames={len(frames)} mask_candidates={full_candidates} out={OUT}"
        )

        frame_meta = []
        chunk = CONCURRENCY * 50
        for fi, iso in enumerate(frames):
            frame_dir = os.path.join(tiles_dir, str(fi))
            frame_meta.append({"index": fi, "time": iso or "latest"})

            # Parents (in the previous zoom) whose subtree should be explored.
            keep_parents: Optional[Set[Tuple[int, int]]] = None
            for z in range(MIN_ZOOM, MAX_ZOOM + 1):
                mask = zoom_mask[z]
                if keep_parents is None:
                    candidates = mask  # top zoom: render the whole mask
                else:
                    candidates = {
                        (x, y) for (x, y) in mask
                        if (x >> 1, y >> 1) in keep_parents
                    }
                    counters["pruned"] += (len(mask) - len(candidates))

                keep_here: Set[Tuple[int, int]] = set()
                cand_list = list(candidates)
                counters["rendered"] += len(cand_list)
                for i in range(0, len(cand_list), chunk):
                    results = await asyncio.gather(*(
                        _render_one(client, sem, frame_dir, z, x, y, iso, counters)
                        for (x, y) in cand_list[i:i + chunk]
                    ))
                    for rx, ry, keep in results:
                        if keep:
                            keep_here.add((rx, ry))
                print(f"  frame {fi} z{z}: candidates={len(cand_list)} "
                      f"kept={len(keep_here)} "
                      f"(written={counters['written']} empty={counters['empty']} "
                      f"pruned={counters['pruned']})")
                keep_parents = keep_here

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
        f"Done in {elapsed:.1f}s | rendered={counters['rendered']} "
        f"written={counters['written']} skipped_empty={counters['empty']} "
        f"pruned_by_quadtree={counters['pruned']} | manifest -> {OUT}/frames.json"
    )


if __name__ == "__main__":
    asyncio.run(main())
