"""Configuration for the NEXRAD tile server.

Radar imagery is sourced from NOAA's public IDP GeoServer
(https://opengeo.ncep.noaa.gov/geoserver), which serves quality-controlled
NEXRAD reflectivity mosaics as OGC WMS layers, one workspace per region.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Tuple

# Base URL of NOAA's public GeoServer WMS.
GEOSERVER_BASE = os.environ.get(
    "NEXRAD_GEOSERVER_BASE",
    "https://opengeo.ncep.noaa.gov/geoserver",
)

# Radar product suffix. NOAA offers both:
#   "bref_qcd" -> Base Reflectivity (lowest tilt), quality controlled
#   "cref_qcd" -> Composite Reflectivity (max over all tilts), quality controlled
DEFAULT_PRODUCT = os.environ.get("NEXRAD_PRODUCT", "bref_qcd")

# Tile render size in pixels (square).
TILE_SIZE = int(os.environ.get("NEXRAD_TILE_SIZE", "256"))

# In-memory tile cache time-to-live in seconds. NOAA refreshes roughly every
# 2 minutes and advertises max-age=120, so we match that.
CACHE_TTL_SECONDS = int(os.environ.get("NEXRAD_CACHE_TTL", "120"))

# Max number of cached tiles held in memory before old entries are dropped.
CACHE_MAX_ENTRIES = int(os.environ.get("NEXRAD_CACHE_MAX", "4096"))

# Per-region upstream request timeout (seconds).
UPSTREAM_TIMEOUT = float(os.environ.get("NEXRAD_UPSTREAM_TIMEOUT", "20"))


@dataclass(frozen=True)
class Region:
    """A NOAA radar mosaic region.

    ``workspace`` is the GeoServer workspace; the layer name is
    ``{workspace}_{product}``. ``bounds`` is a rough (west, south, east, north)
    lon/lat extent used to skip regions a tile cannot possibly overlap.
    """

    key: str
    workspace: str
    bounds: Tuple[float, float, float, float]


# The set of regions that together cover the entire United States and its
# territories. CONUS + Alaska + Hawaii are the ones explicitly requested;
# Puerto Rico/Caribbean and Guam are included for completeness.
REGIONS: Dict[str, Region] = {
    "conus": Region("conus", "conus", (-127.0, 22.0, -65.0, 51.0)),
    "alaska": Region("alaska", "alaska", (-180.0, 48.0, -128.0, 73.0)),
    "hawaii": Region("hawaii", "hawaii", (-161.5, 17.5, -153.5, 23.5)),
    "carib": Region("carib", "carib", (-68.5, 16.5, -63.5, 19.5)),
    "guam": Region("guam", "guam", (143.5, 12.5, 146.5, 15.5)),
}


def layer_name(region: Region, product: str) -> str:
    """Full WMS layer name for a region + product, e.g. ``conus_bref_qcd``."""
    return f"{region.workspace}_{product}"


def wms_endpoint(region: Region) -> str:
    """Workspace-level WMS/OWS endpoint URL for a region.

    GeoServer exposes a WMS at each workspace which can serve any layer in that
    workspace, so this works for any product suffix.
    """
    return f"{GEOSERVER_BASE}/{region.workspace}/ows"
