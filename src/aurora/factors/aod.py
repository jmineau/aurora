"""Aerosol Optical Depth (AOD) at 550 nm from CAMS via Open-Meteo Air Quality.

AOD quantifies the integrated extinction of light by aerosols through the
full atmospheric column.  High AOD events (dust storms, wildfire smoke, haze)
can significantly reduce the visibility of faint aurora.

Beer-Lambert transmittance: T = exp(-AOD × airmass).  With typical aurora
near 30° elevation the effective airmass is ~2.

Data source: https://air-quality-api.open-meteo.com/v1/air-quality
             (CAMS European Centre for Medium-Range Weather Forecasts)
             Free, no API key required.
"""

import datetime as dt
from dataclasses import dataclass

import httpx

_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


@dataclass
class AODResult:
    aod: float  # aerosol optical depth at 550 nm (dimensionless)


async def fetch_aod(
    client: httpx.AsyncClient, lat: float, lon: float
) -> AODResult:
    """Fetch the current-hour AOD at (lat, lon)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "aerosol_optical_depth",
        "forecast_days": 1,
        "timezone": "UTC",
    }
    resp = await client.get(_URL, params=params, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()

    return _parse_aod(data, dt.datetime.now(dt.timezone.utc))


_DEFAULT_AOD = 0.1  # low background value when CAMS has no data for this cell/time


def _parse_aod(data: dict, target: dt.datetime) -> AODResult:
    times = data["hourly"]["time"]
    target_str = target.strftime("%Y-%m-%dT%H:00")
    idx = max((i for i, t in enumerate(times) if t <= target_str), default=0)
    raw = data["hourly"]["aerosol_optical_depth"][idx]
    return AODResult(aod=float(raw) if raw is not None else _DEFAULT_AOD)


async def fetch_aod_archive(
    client: httpx.AsyncClient, lat: float, lon: float, when: dt.datetime
) -> AODResult:
    """Reconstruct AOD at (lat, lon) for a past *when* (UTC) from CAMS history.

    Falls back to a low background value if the archive has no coverage.
    """
    date = when.strftime("%Y-%m-%d")
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "aerosol_optical_depth",
        "start_date": date,
        "end_date": date,
        "timezone": "UTC",
    }
    try:
        resp = await client.get(_URL, params=params, timeout=30.0)
        resp.raise_for_status()
        return _parse_aod(resp.json(), when)
    except (httpx.HTTPError, KeyError, IndexError):
        return AODResult(aod=_DEFAULT_AOD)
