# NEXRAD Tile Server

Serves **256×256 XYZ web-map tiles** of live NEXRAD weather-radar reflectivity
covering the **entire United States** — CONUS, **Alaska**, **Hawaii**, Puerto
Rico/Caribbean, and Guam — built on demand from NOAA's public government radar
imagery.

Radar data comes from **NOAA's public IDP GeoServer**
(`https://opengeo.ncep.noaa.gov/geoserver`), which publishes quality-controlled
NEXRAD reflectivity mosaics as OGC WMS layers (one workspace per region). This
server reprojects/crops those WMS layers into standard slippy-map tiles that any
web map (Leaflet, MapLibre, OpenLayers, Google Maps, etc.) can consume.

## How it works

For each requested tile `/(z)/(x)/(y)`:

1. The tile's Web Mercator (EPSG:3857) bounds are computed.
2. Every US radar region whose extent overlaps the tile is queried in parallel
   via WMS `GetMap` at 256×256 in EPSG:3857.
3. The (spatially disjoint) regional PNGs are alpha-composited into one tile.
4. The result is cached in memory (TTL ≈ upstream refresh, ~2 min) and returned.

An in-flight request coalescer prevents duplicate upstream fetches under load.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run (0.0.0.0 so it's reachable from the internet / other devices)
uvicorn nexrad.main:app --host 0.0.0.0 --port 8000
```

Then open <http://localhost:8000/> for the interactive demo map.

## Endpoints

| Endpoint | Description |
| --- | --- |
| `GET /tiles/{z}/{x}/{y}.png` | 256×256 radar tile. Query: `?product=bref_qcd` (default) or `cref_qcd`; `?time=<ISO8601>` for a historical frame (omit for latest). |
| `GET /frames` | List available frame timestamps. Query: `?minutes=60` (window, 1–180), `?max=13` (max frames), `?product=`. |
| `GET /` | Interactive Leaflet demo map with a **loop player**. |
| `GET /healthz` | Health check. |
| `GET /stats` | Cache stats + config. |

### Animating the last hour

Radar scans update every ~2 minutes and NOAA's WMS exposes a `TIME` dimension,
so you can loop past frames. Fetch the timestamps, then request each frame's
tiles with `&time=`:

```bash
curl "http://localhost:8000/frames?minutes=60"        # -> {"frames": ["2026-...Z", ...]}
curl "http://localhost:8000/tiles/5/7/12.png?time=2026-07-19T22:06:14.000Z" -o frame.png
```

The demo map at `/` does this automatically: it preloads one tile layer per
frame and toggles opacity for smooth playback, with play/pause, a timeline
scrubber, a selectable window (30 min / 1 h / 2 h), and a `LIVE` badge on the
newest frame. Historical frames are immutable, so the server returns them with
`Cache-Control: max-age=3600`.

### Use in a web map

Leaflet (standard 256px tiles):

```js
L.tileLayer('https://YOUR_HOST/tiles/{z}/{x}/{y}.png?product=bref_qcd', {
  tileSize: 256, maxNativeZoom: 12, opacity: 0.85,
  attribution: 'Radar: NOAA/NWS NEXRAD'
}).addTo(map);
```

MapLibre / Mapbox GL:

```js
map.addSource('nexrad', {
  type: 'raster', tileSize: 256,
  tiles: ['https://YOUR_HOST/tiles/{z}/{x}/{y}.png']
});
map.addLayer({ id: 'nexrad', type: 'raster', source: 'nexrad', paint: { 'raster-opacity': 0.85 } });
```

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `NEXRAD_PRODUCT` | `bref_qcd` | Default radar product. |
| `NEXRAD_TILE_SIZE` | `256` | Tile edge in pixels. |
| `NEXRAD_CACHE_TTL` | `120` | Tile cache TTL (seconds). |
| `NEXRAD_CACHE_MAX` | `4096` | Max cached tiles. |
| `NEXRAD_MAX_ZOOM` | `12` | Highest zoom served. |
| `NEXRAD_UPSTREAM_TIMEOUT` | `20` | Per-region WMS timeout (seconds). |
| `NEXRAD_GEOSERVER_BASE` | NOAA GeoServer | Upstream WMS base URL. |

## Deploying to the internet

Run behind any reverse proxy / TLS terminator (nginx, Caddy) or a platform
(Fly.io, Render, a VM). For heavier production traffic, run multiple uvicorn
workers and/or put a CDN in front of `/tiles/*` (responses are already
`Cache-Control: public`). Example:

```bash
uvicorn nexrad.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Hosting on GitHub Pages (no server)

GitHub Pages is **static-only**, so the Python tile server cannot run there.
It doesn't need to: NOAA's GeoServer sends `Access-Control-Allow-Origin: *`, so a
static page can pull the radar WMS tiles **directly from NOAA in the browser**.
`docs/index.html` is a self-contained page that does exactly this — it stacks the
five regional WMS layers (CONUS/AK/HI/PR/Guam) client-side and animates the last
hour via the WMS `TIME` dimension. No backend, always live.

Deploy it:

```bash
git init && git add . && git commit -m "NEXRAD radar"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then in the repo: **Settings → Pages → Source: Deploy from a branch → `main` /
`docs`**. Your live map appears at `https://<you>.github.io/<repo>/`.

## Dynamically-updated tiles on Pages (GitHub Actions)

You can't run the Python server *on* Pages (static hosting only), but you can
have **GitHub Actions render the tiles on a schedule and publish them to Pages**
— effectively "the server, run periodically" — so the site serves real
pre-rendered `{z}/{x}/{y}.png` tiles that refresh automatically.

Pieces (all included):

- **`render_tiles.py`** — reuses the server's rendering logic to write a bounded
  tile pyramid (plus animation frames) into an output dir. Skips fully
  transparent tiles to save space; writes a `frames.json` manifest and copies
  the client app in as `index.html`.
- **`webapp/index.html`** — the map client that reads `frames.json` and the
  local `tiles/{frame}/{z}/{x}/{y}.png` (with the same animation player).
- **`.github/workflows/pages.yml`** — a cron workflow (`*/15 * * * *`, plus
  manual `workflow_dispatch` and on-push) that renders and deploys via the
  official Pages actions (`upload-pages-artifact` + `deploy-pages`), so nothing
  bloats your git history.

Set it up:

1. Push the repo to GitHub.
2. **Settings → Pages → Source: GitHub Actions** (not "deploy from a branch").
3. The workflow runs on push, then every ~15 min. Your site is at
   `https://<you>.github.io/<repo>/`.

Render locally to preview:

```bash
NEXRAD_OUT=_site NEXRAD_MAX_ZOOM=6 NEXRAD_FRAMES=6 python render_tiles.py
python -m http.server -d _site 8000   # open http://localhost:8000/
```

Tune via env vars in the workflow: `NEXRAD_MIN_ZOOM`, `NEXRAD_MAX_ZOOM`,
`NEXRAD_FRAMES`, `NEXRAD_WINDOW_MIN`, `NEXRAD_PRODUCT`, `NEXRAD_CONCURRENCY`.

**Important trade-offs vs. the live client-side page (`docs/index.html`):**

- **Freshness:** GitHub's cron is best-effort and often delayed, so ~10–20 min
  lag is normal. The live client-side page is always current; this route trades
  freshness for hosting real tile files.
- **Tile volume:** each extra zoom level ~4× the tiles, and each animation frame
  multiplies the whole set. Defaults (zoom 0–6, 6 frames) are a few thousand
  tiles — fine. Going to zoom 9+ with many frames can blow past Pages' size and
  the Actions time budget, so raise zoom/frames deliberately.

## Data & attribution

Radar imagery © NOAA / National Weather Service, served from the public NCEP IDP
GeoServer. Please credit NOAA/NWS when displaying these tiles.
