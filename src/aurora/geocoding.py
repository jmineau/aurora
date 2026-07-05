"""Geocoding helper with on-disk pickle cache.

Accepts plain addresses, "lat,lon" strings, or (lat, lon) tuples.
Nominatim results are cached to disk so repeated lookups across restarts
don't hit the API.
"""

import pickle
import re
from pathlib import Path

from geopy.geocoders import Nominatim

_CACHE_FILE = Path("geocode_cache.pkl")
_cache: dict[str, tuple[float, float]] = {}
_geolocator = Nominatim(user_agent="aurora-alert-server/1.0")


def init_cache() -> None:
    """Load the geocode cache from disk (call once at server startup)."""
    global _cache
    if _CACHE_FILE.exists():
        with _CACHE_FILE.open("rb") as fh:
            _cache = pickle.load(fh)


def _save_cache() -> None:
    with _CACHE_FILE.open("wb") as fh:
        pickle.dump(_cache, fh)


def geocode(address: str) -> tuple[float, float]:
    """Resolve *address* to (lat, lon).

    Accepts:
      - "lat,lon" strings          e.g. "64.2,-21.9"
      - Plain addresses            e.g. "Fairbanks, AK"

    Raises ValueError if the address cannot be geocoded.
    """
    # Bare "lat,lon" string – no API call needed.
    match = re.fullmatch(
        r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", address
    )
    if match:
        return float(match.group(1)), float(match.group(2))

    if address in _cache:
        return _cache[address]

    location = _geolocator.geocode(address)
    if location is None:
        raise ValueError(f"Could not geocode address: {address!r}")

    coords: tuple[float, float] = (location.latitude, location.longitude)
    _cache[address] = coords
    _save_cache()
    return coords
