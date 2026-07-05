"""NOAA SWPC OVATION Prime aurora probability model.

OVATION ingests real-time solar wind data from DSCOVR and produces a 1°×1°
global grid of aurora probability (0–100) updated every ~30 minutes.

Rather than sampling the probability directly overhead the observer (which
under-predicts what mid-latitude viewers see), we sample a *poleward profile* —
the probability at a series of ground points marching toward the pole — and let
aurora.geometry decide which of those are geometrically above the observer's
horizon given the emission altitude.  See aurora.geometry for the reasoning.

The fitted interpolator (not just the raw JSON) is cached for CACHE_TTL_SECONDS,
so repeated location checks in a cycle reuse one download *and* one grid build.

Data source: https://services.swpc.noaa.gov/json/ovation_aurora_latest.json
"""

import datetime as dt
from dataclasses import dataclass, field

import httpx
import numpy as np
from scipy.interpolate import RegularGridInterpolator

from aurora import geometry

_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
_CACHE_TTL_SECONDS = 15 * 60  # re-fetch after 15 minutes

# Cached fitted model: (interpolator, observation_time, forecast_time, fetched_at).
_cache: tuple[RegularGridInterpolator, dt.datetime, dt.datetime, dt.datetime] | None = None


@dataclass
class OVATIONResult:
    probability: float            # OVATION probability directly overhead, 0–100
    observation_time: dt.datetime
    forecast_time: dt.datetime
    # Poleward samples: list of (ground_distance_m, probability).
    poleward_profile: list[tuple[float, float]] = field(default_factory=list)
    # Geometry-aware fields, filled in by the checker once terrain is known.
    visible_probability: float | None = None   # best prob above the horizon, 0–100
    visible_elevation_deg: float | None = None  # elevation it appears at


def _build_interpolator(data: dict) -> RegularGridInterpolator:
    """Build a bilinear interpolator over the OVATION probability grid.

    The coordinate list is [[lon (0–359), lat (−90–90), probability], ...].
    """
    coords = np.asarray(data["coordinates"], dtype=np.float64)  # (N, 3)
    grid_lons = np.arange(0, 360, dtype=np.float64)   # 360 values
    grid_lats = np.arange(-90, 91, dtype=np.float64)  # 181 values
    prob_grid = np.zeros((360, 181), dtype=np.float64)

    lo_idx = np.mod(np.round(coords[:, 0]).astype(int), 360)
    la_idx = np.round(coords[:, 1]).astype(int) + 90
    prob_grid[lo_idx, la_idx] = coords[:, 2]

    return RegularGridInterpolator(
        (grid_lons, grid_lats),
        prob_grid,
        method="linear",
        bounds_error=False,
        fill_value=0.0,
    )


def sample_poleward_profile(
    interp: RegularGridInterpolator,
    lat: float,
    lon: float,
    distances: list[float],
) -> list[tuple[float, float]]:
    """OVATION probability at each poleward ground point (pure; no I/O)."""
    bearing = geometry.poleward_bearing(lat)
    points = [geometry.destination_point(lat, lon, bearing, d) for d in distances]
    query = np.array([[plon % 360.0, plat] for plat, plon in points])
    probs = np.clip(interp(query), 0.0, 100.0)
    return list(zip(distances, (float(p) for p in probs)))


async def fetch_ovation(
    client: httpx.AsyncClient, lat: float, lon: float
) -> OVATIONResult:
    """Fetch the latest OVATION grid and sample it overhead and poleward at (lat, lon)."""
    global _cache

    now = dt.datetime.now(dt.timezone.utc)
    if _cache is None or (now - _cache[3]).total_seconds() > _CACHE_TTL_SECONDS:
        resp = await client.get(_URL, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        interp = _build_interpolator(data)
        obs_time = dt.datetime.fromisoformat(data["Observation Time"].replace("Z", "+00:00"))
        fcast_time = dt.datetime.fromisoformat(data["Forecast Time"].replace("Z", "+00:00"))
        _cache = (interp, obs_time, fcast_time, now)

    interp, obs_time, fcast_time, _ = _cache

    lon_norm = lon % 360.0
    overhead = float(np.clip(interp([[lon_norm, lat]])[0], 0.0, 100.0))
    profile = sample_poleward_profile(interp, lat, lon, geometry.sample_distances())

    return OVATIONResult(
        probability=overhead,
        observation_time=obs_time,
        forecast_time=fcast_time,
        poleward_profile=profile,
    )
