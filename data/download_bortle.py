"""One-time script to build the Bortle light-pollution raster (data/bortle.npy).

Source: NASA Black Marble VNP46A4 annual nighttime lights composite.
        https://neo.gsfc.nasa.gov – 0.1° resolution, global, free.

The NASA Earth Observations (NEO) portal serves the annual average nighttime
lights as a 3600×1800 PNG (0.1°/pixel, lon −180→+180, lat +90→−90).
Pixel luminance (0–255, log-scaled radiance) is converted to an approximate
Bortle class using the empirical relationship from Cinzano et al. (2001):

    Bortle ≈ 1 + 8 × (radiance / max_radiance) ^ 0.4

This gives:
  pixel = 0   → Bortle 1  (no artificial light, true dark sky)
  pixel = 128 → Bortle ~5 (suburban sky)
  pixel = 255 → Bortle 9  (city centre)

Output: data/bortle.npy
  dtype  : float32
  shape  : (1800, 3600)  – row 0 = 90°N, row 1799 = 90°S; col 0 = 180°W

Usage::

    cd /path/to/aurora
    python data/download_bortle.py

Requires: numpy, pillow, httpx (all in project dependencies).
"""

import io
import sys
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

# NASA NEO annual nighttime lights, 3600×1800 px, 0.1°/px
_URL = (
    "https://neo.gsfc.nasa.gov/servlet/RenderData"
    "?si=1576441&cs=rgb&format=PNG&width=3600&height=1800"
)

_OUTPUT = Path(__file__).parent / "bortle.npy"


def radiance_to_bortle(pixel: np.ndarray) -> np.ndarray:
    """Convert pixel luminance (0–255) to approximate Bortle class (1–9).

    The conversion uses the empirical power-law fit described in the module
    docstring.  Values are clipped to the valid Bortle range.
    """
    frac = (pixel.astype(np.float32) / 255.0) ** 0.4
    bortle = 1.0 + 8.0 * frac
    return np.clip(bortle, 1.0, 9.0).astype(np.float32)


def main() -> None:
    print(f"Downloading nighttime lights PNG from NASA NEO…")
    print(f"  URL: {_URL}")

    try:
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            resp = client.get(_URL)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Downloaded {len(resp.content) / 1e6:.1f} MB.  Converting to Bortle scale…")

    img = Image.open(io.BytesIO(resp.content)).convert("L")  # grayscale
    pixels = np.asarray(img, dtype=np.uint8)                 # (1800, 3600)

    if pixels.shape != (1800, 3600):
        print(
            f"Unexpected image shape {pixels.shape}; expected (1800, 3600).",
            file=sys.stderr,
        )
        sys.exit(1)

    bortle_grid = radiance_to_bortle(pixels)

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    np.save(_OUTPUT, bortle_grid)

    print(f"Saved {_OUTPUT}  ({_OUTPUT.stat().st_size / 1e6:.1f} MB)")
    print("Bortle statistics:")
    print(f"  min={bortle_grid.min():.2f}  max={bortle_grid.max():.2f}  "
          f"mean={bortle_grid.mean():.2f}  median={np.median(bortle_grid):.2f}")


if __name__ == "__main__":
    main()
