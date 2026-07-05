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

    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:00")
    times = data["hourly"]["time"]
    idx = max((i for i, t in enumerate(times) if t <= now_str), default=0)

    raw = data["hourly"]["aerosol_optical_depth"][idx]
    # Fall back to a low background value if CAMS has no data for this cell.
    return AODResult(aod=float(raw) if raw is not None else 0.1)
