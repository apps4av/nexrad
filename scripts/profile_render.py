"""Profile the NEXRAD tile render: per-zoom timing + network/CPU split.

Reuses the exact tile enumeration and rendering logic from `render_tiles.py`
/ `nexrad.radar`, but instruments it so you can see where the time goes as you
raise NEXRAD_MAX_ZOOM.

For each zoom level it reports:
  candidates  tiles considered
  written     non-empty tiles
  empty       transparent tiles skipped
  wall        wall-clock seconds for that zoom
  net         summed time spent inside render_tile (WMS fetch, network-bound)
  cpu         summed time spent decoding/checking the PNG (Pillow, CPU-bound)
  tiles/s     throughput for that zoom

Env vars mirror render_tiles.py (NEXRAD_PRODUCT, NEXRAD_MIN_ZOOM,
NEXRAD_MAX_ZOOM, NEXRAD_FRAMES, NEXRAD_WINDOW_MIN, NEXRAD_CONCURRENCY).

Usage:
  python scripts/profile_render.py
"""

from __future__ import annotations

import asyncio
import io
import os
import time
from typing import List, Optional

import httpx
from PIL import Image

from nexrad import config, radar
import render_tiles as rt


async def _profile_one(client, sem, z, x, y, iso, acc):
    async with sem:
        t0 = time.perf_counter()
        data = await radar.render_tile(client, z, x, y, config.DEFAULT_PRODUCT if not PRODUCT else PRODUCT, iso)
        t1 = time.perf_counter()
    # CPU-bound emptiness check (PNG decode)
    c0 = time.perf_counter()
    empty = rt._is_empty(data)
    c1 = time.perf_counter()
    acc["net"] += (t1 - t0)
    acc["cpu"] += (c1 - c0)
    acc["bytes"] += len(data)
    if empty:
        acc["empty"] += 1
    else:
        acc["written"] += 1


PRODUCT = os.environ.get("NEXRAD_PRODUCT", config.DEFAULT_PRODUCT)


async def main() -> None:
    min_z = rt.MIN_ZOOM
    max_z = rt.MAX_ZOOM
    frames_n = rt.FRAMES
    conc = rt.CONCURRENCY

    limits = httpx.Limits(max_connections=conc * 4, max_keepalive_connections=conc * 2)
    async with httpx.AsyncClient(
        limits=limits,
        headers={"User-Agent": "nexrad-profile/1.0"},
        follow_redirects=True,
    ) as client:
        if frames_n <= 1:
            frames: List[Optional[str]] = [None]
        else:
            ft = await radar.frame_times(client, rt.WINDOW_MIN, frames_n, PRODUCT)
            frames = ft if ft else [None]

        sem = asyncio.Semaphore(conc)
        print(f"product={PRODUCT} zooms={min_z}..{max_z} frames={len(frames)} "
              f"concurrency={conc}\n")
        header = f"{'z':>2} {'candidates':>10} {'written':>8} {'empty':>7} " \
                 f"{'wall(s)':>8} {'net(s)':>8} {'cpu(s)':>7} {'MB':>7} {'tiles/s':>8}"
        print(header)
        print("-" * len(header))

        grand = {"cand": 0, "written": 0, "empty": 0, "wall": 0.0, "net": 0.0, "cpu": 0.0, "bytes": 0}
        overall_start = time.perf_counter()

        for z in range(min_z, max_z + 1):
            tiles = rt._tiles_for_zoom(z)
            acc = {"written": 0, "empty": 0, "net": 0.0, "cpu": 0.0, "bytes": 0}
            zstart = time.perf_counter()
            tasks = []
            for iso in frames:
                for (x, y) in tiles:
                    tasks.append(_profile_one(client, sem, z, x, y, iso, acc))
            await asyncio.gather(*tasks)
            wall = time.perf_counter() - zstart
            cand = len(tiles) * len(frames)
            tps = cand / wall if wall > 0 else 0.0
            mb = acc["bytes"] / 1e6
            print(f"{z:>2} {cand:>10} {acc['written']:>8} {acc['empty']:>7} "
                  f"{wall:>8.2f} {acc['net']:>8.1f} {acc['cpu']:>7.1f} {mb:>7.1f} {tps:>8.1f}")
            grand["cand"] += cand
            grand["written"] += acc["written"]
            grand["empty"] += acc["empty"]
            grand["wall"] += wall
            grand["net"] += acc["net"]
            grand["cpu"] += acc["cpu"]
            grand["bytes"] += acc["bytes"]

        overall = time.perf_counter() - overall_start
        print("-" * len(header))
        print(f"{'ALL':>2} {grand['cand']:>10} {grand['written']:>8} {grand['empty']:>7} "
              f"{overall:>8.2f} {grand['net']:>8.1f} {grand['cpu']:>7.1f} "
              f"{grand['bytes']/1e6:>7.1f} {grand['cand']/overall if overall else 0:>8.1f}")
        print(f"\nwall clock: {overall:.1f}s  |  summed network: {grand['net']:.1f}s "
              f"(x{grand['net']/overall if overall else 0:.1f} concurrency benefit)  "
              f"|  summed cpu: {grand['cpu']:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
