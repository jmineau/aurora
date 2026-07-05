"""Light pollution lookup from a pre-processed Bortle-class raster.

The raster is stored as a numpy array at data/bortle.npy (generated once by
data/download_bortle.py).  Grid resolution is 0.1° with shape (1800, 3600)
covering latitudes −90 to +90 and longitudes −180 to +180.

If the file is missing (e.g. before the download script has been run) the
module returns a conservative default of Bortle 4 (rural/suburban sky) with
a warning, so the server can still start and operate.

Bortle scale reference:
  1  – Truly dark sky (zodiacal light visible)
  4  – Rural/suburban transition
  7  – Suburban sky
  9  – Inner-city sky
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_RASTER_PATH = Path(__file__).parent.parent.parent.parent / "data" / "bortle.npy"
_BORTLE_DEFAULT = 4.0  # fallback when raster is unavailable
_grid: np.ndarray | None = None


def _load_grid() -> np.ndarray | None:
    """Load the Bortle raster on first use; return None if file is missing."""
    global _grid
    if _grid is not None:
        return _grid
    if not _RASTER_PATH.exists():
        log.warning(
            "Bortle raster not found at %s – run data/download_bortle.py to "
            "generate it.  Using default Bortle %.0f.",
            _RASTER_PATH,
            _BORTLE_DEFAULT,
        )
        return None
    _grid = np.load(_RASTER_PATH)
    return _grid


@dataclass
class LightPollutionResult:
    bortle: float  # Bortle class at the site, 1–9


def fetch_light_pollution(lat: float, lon: float) -> LightPollutionResult:
    """Return the Bortle class at (lat, lon) from the pre-built raster.

    Grid layout (matches download_bortle.py output):
      axis 0 – latitude  index 0 = 90°N, index 1799 = 90°S  (step −0.1°)
      axis 1 – longitude index 0 = 180°W, index 3599 = 180°E (step +0.1°)
    """
    grid = _load_grid()
    if grid is None:
        return LightPollutionResult(bortle=_BORTLE_DEFAULT)

    lat_idx = int(round((90.0 - lat) / 0.1))
    lon_idx = int(round((lon + 180.0) / 0.1))

    # Clamp to valid indices.
    lat_idx = max(0, min(lat_idx, grid.shape[0] - 1))
    lon_idx = max(0, min(lon_idx, grid.shape[1] - 1))

    bortle = float(np.clip(grid[lat_idx, lon_idx], 1.0, 9.0))
    return LightPollutionResult(bortle=bortle)
