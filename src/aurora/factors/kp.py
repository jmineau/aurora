"""Planetary Kp geomagnetic index from NOAA SWPC.

Kp is a global index (0–9) of geomagnetic disturbance driven by solar wind.
High Kp extends the auroral oval equatorward and increases aurora brightness.
OVATION already incorporates geomagnetic conditions but Kp provides an
independent, real-time sanity check: if Kp is very low (< 1) the aurora
may be confined to polar latitudes regardless of the OVATION forecast.

The 1-minute estimated Kp values are fetched and the most recent value is
returned.  The endpoint updates in near-real-time.

Data source:
  https://services.swpc.noaa.gov/json/planetary_k_index_1m.json
"""

import datetime as dt
from dataclasses import dataclass

import httpx

_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
_CACHE_TTL_SECONDS = 5 * 60  # re-fetch at most every 5 minutes

_cache: tuple[float, dt.datetime] | None = None  # (kp_value, fetched_at)


@dataclass
class KpResult:
    kp_index: float  # most recent estimated Kp, 0–9


def _parse_latest_kp(data: list[dict]) -> float:
    """Latest estimated Kp from the SWPC feed.

    Each entry is a dict: {"time_tag", "kp_index", "estimated_kp", "kp"}.  Prefer
    the real-time estimated Kp; fall back to the integer kp_index; 0.0 if neither.
    """
    for entry in reversed(data):
        val = entry.get("estimated_kp", entry.get("kp_index"))
        if val is not None:
            return float(val)
    return 0.0


async def fetch_kp(client: httpx.AsyncClient) -> KpResult:
    """Return the most recent estimated planetary Kp index."""
    global _cache

    now = dt.datetime.now(dt.timezone.utc)
    if _cache is not None and (now - _cache[1]).total_seconds() < _CACHE_TTL_SECONDS:
        return KpResult(kp_index=_cache[0])

    resp = await client.get(_URL, timeout=20.0)
    resp.raise_for_status()

    kp = _parse_latest_kp(resp.json())
    _cache = (kp, now)
    return KpResult(kp_index=kp)
