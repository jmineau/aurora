"""Site elevation and approximate horizon elevation from the Open-Meteo API.

Elevation is a static factor – it doesn't change between checks.  After the
first successful fetch the result is cached in the Subscription row in the
database so this module is only called once per subscription.

Horizon elevation is estimated by sampling the terrain in eight compass
directions at 20 km from the site and computing the maximum angular
elevation to those sample points.  This is a rough proxy for topographic
obstruction; aurora near the geomagnetic equatorward edge of the oval is
typically at 5–15° elevation from mid-latitude sites, so a high horizon
directly to the north matters.

Data source: https://api.open-meteo.com/v1/elevation (free, no key)
"""

import math
from dataclasses import dataclass

import httpx

_URL = "https://api.open-meteo.com/v1/elevation"
_SAMPLE_DISTANCE_M = 20_000  # sample horizon points 20 km from the site
_DIRECTIONS = [0, 45, 90, 135, 180, 225, 270, 315]  # compass bearings (°)


@dataclass
class TerrainResult:
    elevation_m: float    # site elevation above MSL, metres
    horizon_deg: float    # approximate maximum horizon elevation angle, degrees


def _offset_point(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Return the lat/lon of a point *distance_m* from (lat, lon) at *bearing_deg*.

    Uses a flat-Earth approximation – sufficient for 20 km offsets.
    """
    R = 6_371_000.0  # Earth radius, metres
    dlat = (distance_m / R) * math.cos(math.radians(bearing_deg)) * (180.0 / math.pi)
    dlon = (
        (distance_m / R)
        * math.sin(math.radians(bearing_deg))
        / math.cos(math.radians(lat))
        * (180.0 / math.pi)
    )
    return lat + dlat, lon + dlon


async def fetch_terrain(
    client: httpx.AsyncClient, lat: float, lon: float
) -> TerrainResult:
    """Fetch site elevation and estimate horizon elevation at (lat, lon).

    Sends a single request with the site coordinate plus 8 surrounding
    sample points (one per compass octant at 20 km) and finds the maximum
    angular elevation to those samples.
    """
    sample_points = [_offset_point(lat, lon, b, _SAMPLE_DISTANCE_M) for b in _DIRECTIONS]
    all_lats = [lat] + [p[0] for p in sample_points]
    all_lons = [lon] + [p[1] for p in sample_points]

    params = {
        "latitude": ",".join(f"{v:.5f}" for v in all_lats),
        "longitude": ",".join(f"{v:.5f}" for v in all_lons),
    }
    resp = await client.get(_URL, params=params, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()

    elevations: list[float] = [float(e or 0.0) for e in data["elevation"]]
    site_elev = elevations[0]

    # Angular elevation to each sample point (negative = below site).
    max_horizon = 0.0
    for sample_elev in elevations[1:]:
        angle_deg = math.degrees(
            math.atan2(sample_elev - site_elev, _SAMPLE_DISTANCE_M)
        )
        if angle_deg > max_horizon:
            max_horizon = angle_deg

    return TerrainResult(
        elevation_m=max(site_elev, 0.0),
        horizon_deg=max(max_horizon, 0.0),
    )
