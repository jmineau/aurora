"""NOAA SWPC OVATION Prime aurora probability model.

OVATION ingests real-time solar wind data from DSCOVR and produces a 1°×1°
global grid of aurora probability (0–100) updated every ~30 minutes.

The full JSON is cached in memory for CACHE_TTL_SECONDS to avoid hitting
the endpoint on every location check.  A scipy RegularGridInterpolator
provides bilinear interpolation between grid nodes.

Data source: https://services.swpc.noaa.gov/json/ovation_aurora_latest.json
"""

import datetime as dt
from dataclasses import dataclass

import httpx
import numpy as np
from scipy.interpolate import RegularGridInterpolator

_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
_CACHE_TTL_SECONDS = 15 * 60  # re-fetch after 15 minutes

# Tuple of (parsed_data_dict, fetched_at datetime).
_cache: tuple[dict, dt.datetime] | None = None


@dataclass
class OVATIONResult:
    probability: float           # interpolated aurora probability, 0–100
    observation_time: dt.datetime
    forecast_time: dt.datetime


async def fetch_ovation(
    client: httpx.AsyncClient, lat: float, lon: float
) -> OVATIONResult:
    """Fetch the latest OVATION aurora probability at (lat, lon).

    Uses a module-level cache so multiple subscriptions in the same check
    cycle share a single HTTP request.
    """
    global _cache

    now = dt.datetime.now(dt.timezone.utc)
    if _cache is None or (now - _cache[1]).total_seconds() > _CACHE_TTL_SECONDS:
        resp = await client.get(_URL, timeout=20.0)
        resp.raise_for_status()
        _cache = (resp.json(), now)

    data = _cache[0]

    obs_time = dt.datetime.fromisoformat(
        data["Observation Time"].replace("Z", "+00:00")
    )
    fcast_time = dt.datetime.fromisoformat(
        data["Forecast Time"].replace("Z", "+00:00")
    )

    # The coordinate list is [[lon (0–359), lat (−90–90), probability], ...].
    # Build a regular grid then interpolate.
    coords = np.asarray(data["coordinates"], dtype=np.float64)  # (N, 3)

    grid_lons = np.arange(0, 360, dtype=np.float64)   # 360 values
    grid_lats = np.arange(-90, 91, dtype=np.float64)  # 181 values
    prob_grid = np.zeros((360, 181), dtype=np.float64)

    for point in coords:
        lo_idx = int(round(point[0])) % 360
        la_idx = int(round(point[1])) + 90
        prob_grid[lo_idx, la_idx] = point[2]

    interp = RegularGridInterpolator(
        (grid_lons, grid_lats),
        prob_grid,
        method="linear",
        bounds_error=False,
        fill_value=0.0,
    )

    lon_norm = lon % 360.0
    probability = float(np.clip(interp([[lon_norm, lat]])[0], 0.0, 100.0))

    return OVATIONResult(
        probability=probability,
        observation_time=obs_time,
        forecast_time=fcast_time,
    )
