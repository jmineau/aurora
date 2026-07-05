"""Cloud cover and precipitable water vapour from the Open-Meteo Forecast API.

Both variables are fetched in a single request since they share the same
endpoint.  Low/mid/high cloud layers are included so the alert SMS can
identify which part of the atmosphere is the limiting factor.

Precipitable water vapour (PWV) is the integrated water vapour column in mm.
Higher PWV increases near-IR extinction but has a smaller effect on the
visual aurora band compared with clouds or AOD.

Data source: https://api.open-meteo.com/v1/forecast (free, no key required)
"""

import datetime as dt
from dataclasses import dataclass

import httpx

_URL = "https://api.open-meteo.com/v1/forecast"


_DEFAULT_PWV_MM = 20.0  # typical mid-latitude column when PWV is unavailable


def _extract_pwv(hourly: dict, idx: int) -> float:
    """PWV (mm) at hour *idx*, or a mid-latitude default if unavailable.

    The forecast endpoint doesn't reliably return integrated water vapour, so
    guard against a missing/short series rather than indexing blindly.
    """
    series = hourly.get("total_column_integrated_water_vapour")
    if series and idx < len(series) and series[idx] is not None:
        return float(series[idx])
    return _DEFAULT_PWV_MM


@dataclass
class WeatherResult:
    cloud_cover: float  # total cloud cover, 0–100 %
    low_cloud: float    # low-level cloud cover, 0–100 %
    mid_cloud: float    # mid-level cloud cover, 0–100 %
    high_cloud: float   # high-level cloud cover, 0–100 %
    pwv_mm: float       # precipitable water vapour, mm


async def fetch_weather(
    client: httpx.AsyncClient, lat: float, lon: float
) -> WeatherResult:
    """Fetch cloud cover and PWV for the current hour at (lat, lon)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": (
            "cloud_cover,cloud_cover_low,cloud_cover_mid,"
            "cloud_cover_high,precipitation"
        ),
        "forecast_days": 1,
        "timezone": "UTC",
    }
    resp = await client.get(_URL, params=params, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()

    # Find the index whose timestamp is closest to (but not after) now.
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:00")
    times = data["hourly"]["time"]
    idx = max((i for i, t in enumerate(times) if t <= now_str), default=0)

    h = data["hourly"]

    return WeatherResult(
        cloud_cover=float(h["cloud_cover"][idx] or 0.0),
        low_cloud=float(h["cloud_cover_low"][idx] or 0.0),
        mid_cloud=float(h["cloud_cover_mid"][idx] or 0.0),
        high_cloud=float(h["cloud_cover_high"][idx] or 0.0),
        pwv_mm=_extract_pwv(h, idx),
    )
