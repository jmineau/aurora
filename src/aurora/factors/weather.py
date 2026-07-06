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

from aurora import geometry

_URL = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_HOURLY_VARS = "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,precipitation"
# Distance poleward (toward the geomagnetic pole) to sample the "northern horizon"
# cloud that obscures low aurora — roughly where a low sight line is at cloud height.
_CLOUD_SAMPLE_M = 60_000.0


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
    cloud_cover: float           # total cloud cover overhead, 0–100 %
    low_cloud: float             # low-level cloud cover, 0–100 %
    mid_cloud: float             # mid-level cloud cover, 0–100 %
    high_cloud: float            # high-level cloud cover, 0–100 %
    pwv_mm: float                # precipitable water vapour, mm
    cloud_cover_poleward: float = 0.0  # total cloud toward the poleward horizon, 0–100 %


def _nearest_hour_index(times: list[str], target: dt.datetime) -> int:
    """Index of the hourly timestamp at or just before *target* (UTC)."""
    target_str = target.strftime("%Y-%m-%dT%H:00")
    return max((i for i, t in enumerate(times) if t <= target_str), default=0)


def _parse_weather(loc: dict, target: dt.datetime) -> WeatherResult:
    h = loc["hourly"]
    idx = _nearest_hour_index(h["time"], target)
    return WeatherResult(
        cloud_cover=float(h["cloud_cover"][idx] or 0.0),
        low_cloud=float(h["cloud_cover_low"][idx] or 0.0),
        mid_cloud=float(h["cloud_cover_mid"][idx] or 0.0),
        high_cloud=float(h["cloud_cover_high"][idx] or 0.0),
        pwv_mm=_extract_pwv(h, idx),
    )


def _cloud_at(loc: dict, target: dt.datetime) -> float:
    h = loc["hourly"]
    idx = _nearest_hour_index(h["time"], target)
    return float(h["cloud_cover"][idx] or 0.0)


def _two_point_params(lat: float, lon: float) -> dict:
    """Query params sampling the overhead point and a point poleward of it."""
    bearing = geometry.geomagnetic_pole_bearing(lat, lon)
    plat, plon = geometry.destination_point(lat, lon, bearing, _CLOUD_SAMPLE_M)
    return {
        "latitude": f"{lat},{plat}",
        "longitude": f"{lon},{plon}",
        "hourly": _HOURLY_VARS,
    }


def _combine(data, target: dt.datetime) -> WeatherResult:
    """Overhead WeatherResult (data[0]) plus poleward cloud (data[1])."""
    locs = data if isinstance(data, list) else [data]
    result = _parse_weather(locs[0], target)
    result.cloud_cover_poleward = _cloud_at(locs[1], target) if len(locs) > 1 else result.cloud_cover
    return result


async def fetch_weather(
    client: httpx.AsyncClient, lat: float, lon: float
) -> WeatherResult:
    """Fetch overhead + poleward cloud and PWV for the current hour at (lat, lon)."""
    params = _two_point_params(lat, lon)
    params |= {"forecast_days": 1, "timezone": "UTC"}
    resp = await client.get(_URL, params=params, timeout=20.0)
    resp.raise_for_status()
    return _combine(resp.json(), dt.datetime.now(dt.timezone.utc))


async def fetch_weather_archive(
    client: httpx.AsyncClient, lat: float, lon: float, when: dt.datetime
) -> WeatherResult:
    """Reconstruct overhead + poleward cloud / PWV at (lat, lon) for a past *when*.

    Uses the Open-Meteo ERA5 archive.  For backfilling ground-truth observations.
    """
    date = when.strftime("%Y-%m-%d")
    params = _two_point_params(lat, lon)
    params |= {"start_date": date, "end_date": date, "timezone": "UTC"}
    resp = await client.get(_ARCHIVE_URL, params=params, timeout=30.0)
    resp.raise_for_status()
    return _combine(resp.json(), when)
