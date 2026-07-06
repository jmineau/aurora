"""Site elevation and approximate horizon elevation from the Open-Meteo API.

Elevation is a static factor – it doesn't change between checks.  After the
first successful fetch the result is cached in the Subscription row in the
database so this module is only called once per subscription.

Horizon elevation is estimated toward the *pole*, because that is the direction
the aurora appears from — a ridge to the north (N hemisphere) obstructs the oval,
whereas terrain to the south is irrelevant.  We sample the poleward bearing at a
few distances and take the maximum angular elevation, which the geometry layer
then uses to decide whether the aurora clears the horizon.

Data source: https://api.open-meteo.com/v1/elevation (free, no key)
"""

import math
from dataclasses import dataclass

import httpx

from aurora import geometry

_URL = "https://api.open-meteo.com/v1/elevation"
# Poleward horizon sample distances — a near ridge blocks more sky than a far one.
_SAMPLE_DISTANCES_M = [2_000, 5_000, 10_000, 20_000]


@dataclass
class TerrainResult:
    elevation_m: float    # site elevation above MSL, metres
    horizon_deg: float    # poleward horizon obstruction angle, degrees


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
    """Fetch site elevation and estimate the poleward horizon at (lat, lon).

    Sends a single request with the site coordinate plus sample points marching
    toward the pole, and finds the maximum angular elevation to those samples —
    the terrain obstruction in the direction the aurora appears from.
    """
    bearing = geometry.geomagnetic_pole_bearing(lat, lon)
    sample_points = [
        geometry.destination_point(lat, lon, bearing, d) for d in _SAMPLE_DISTANCES_M
    ]
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

    # Angular elevation to each poleward sample point (negative = below site).
    max_horizon = 0.0
    for sample_elev, distance_m in zip(elevations[1:], _SAMPLE_DISTANCES_M):
        angle_deg = math.degrees(math.atan2(sample_elev - site_elev, distance_m))
        if angle_deg > max_horizon:
            max_horizon = angle_deg

    return TerrainResult(
        elevation_m=max(site_elev, 0.0),
        horizon_deg=max(max_horizon, 0.0),
    )
