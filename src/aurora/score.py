"""Weighted-product aurora visibility scoring model.

The final score is the product of nine independent factor terms, each raised
to a configurable weight exponent, multiplied by 100 to give a 0–100 scale:

    score = f_dark
          × f_ovation^w_ovation
          × f_kp^w_kp
          × f_cloud^w_cloud
          × f_aod^w_aod
          × f_elev^w_elev
          × f_moon^w_moon
          × f_lp^w_lp
          × f_pwv^w_pwv
          × f_horiz^w_horiz
          × 100

f_dark is a hard gate (0 in daylight, 0–1 through twilight, 1 at night) and
is never raised to an exponent – if the sun is above the horizon the score is
always 0.

Factor derivations
------------------
f_ovation  = clip(ovation_prob / 100, 0, 1)
f_kp       = clip(0.1 + 0.9 * kp / 5, 0, 1)
               → 0.1 at Kp=0 (quiet), 1.0 at Kp≥5 (minor storm)
f_cloud    = 1 - clip(cloud_cover / 100, 0, 1)
f_aod      = exp(-aod * 2)       Beer-Lambert, airmass ≈ 2 at 30° elevation
f_elev     = 0.5 + 0.5*(1 - exp(-elevation_m / 1500))
               → 0.5 at sea level, asymptotic toward 1.0 above ~3 km
f_moon     = 1 - 0.8 * illumination_fraction
               → 1.0 at new moon, 0.2 at full moon
f_lp       = clip((10 - bortle) / 9, 0.05, 1.0)
               → 1.0 at Bortle 1 (darkest), 0.11 at Bortle 9 (city centre)
f_pwv      = exp(-pwv_mm / 40)   40 mm PWV ~ very humid tropical atmosphere
f_horiz    = clip(1 - horizon_deg / 20, 0, 1)
               → 1.0 flat terrain, 0.0 at ≥20° mean horizon elevation
"""

import datetime as dt
from dataclasses import dataclass

import numpy as np
from astral import Observer
from astral.sun import elevation as solar_elevation

from aurora.config import Settings


@dataclass
class FactorBundle:
    """All raw atmospheric and geophysical inputs required to compute a score."""

    ovation_prob: float       # OVATION aurora probability, 0–100
    kp_index: float           # NOAA Kp geomagnetic index, 0–9
    cloud_cover: float        # Total cloud cover, 0–100 %
    aod: float                # Aerosol optical depth at 550 nm
    elevation_m: float        # Site elevation above MSL, metres
    moon_illumination: float  # Lunar illumination fraction, 0–1
    bortle: float             # Bortle class, 1 (darkest) – 9 (city centre)
    pwv_mm: float             # Precipitable water vapour, mm
    horizon_deg: float        # Mean horizon elevation angle, degrees
    lat: float
    lon: float
    when: dt.datetime         # UTC datetime (for darkness calculation)


@dataclass
class ScoreBreakdown:
    """Final score plus the intermediate [0, 1] factor values.

    Useful for SMS breakdowns and API responses.
    """

    visibility_score: float  # 0–100
    f_dark: float
    f_ovation: float
    f_kp: float
    f_cloud: float
    f_aod: float
    f_elev: float
    f_moon: float
    f_lp: float
    f_pwv: float
    f_horiz: float


def _darkness(lat: float, lon: float, when: dt.datetime) -> float:
    """Return a darkness gate value in [0, 1].

    1.0 = full astronomical night (sun ≤ −18°)
    0.0 = daytime (sun ≥ 0°)
    Linear gradient through nautical/civil twilight.
    """
    observer = Observer(latitude=lat, longitude=lon)
    sun_elev = solar_elevation(observer, dateandtime=when)

    if sun_elev <= -18.0:
        return 1.0
    if sun_elev >= 0.0:
        return 0.0
    return -sun_elev / 18.0


def compute_score(bundle: FactorBundle, settings: Settings) -> ScoreBreakdown:
    """Compute the weighted-product aurora visibility score.

    Returns a ScoreBreakdown with both the final score and all intermediate
    factor values so callers can explain what drove the result.
    """
    f_dark = _darkness(bundle.lat, bundle.lon, bundle.when)

    # Short-circuit in daylight – avoids wasted exponentiation.
    if f_dark == 0.0:
        return ScoreBreakdown(
            visibility_score=0.0,
            f_dark=0.0,
            f_ovation=float(np.clip(bundle.ovation_prob / 100.0, 0.0, 1.0)),
            f_kp=float(np.clip(0.1 + 0.9 * bundle.kp_index / 5.0, 0.0, 1.0)),
            f_cloud=float(1.0 - np.clip(bundle.cloud_cover / 100.0, 0.0, 1.0)),
            f_aod=float(np.exp(-bundle.aod * 2.0)),
            f_elev=float(0.5 + 0.5 * (1.0 - np.exp(-bundle.elevation_m / 1500.0))),
            f_moon=float(1.0 - 0.8 * bundle.moon_illumination),
            f_lp=float(np.clip((10.0 - bundle.bortle) / 9.0, 0.05, 1.0)),
            f_pwv=float(np.exp(-bundle.pwv_mm / 40.0)),
            f_horiz=float(np.clip(1.0 - bundle.horizon_deg / 20.0, 0.0, 1.0)),
        )

    # Compute each factor in [0, 1].
    f_ovation = float(np.clip(bundle.ovation_prob / 100.0, 0.0, 1.0))
    f_kp = float(np.clip(0.1 + 0.9 * bundle.kp_index / 5.0, 0.0, 1.0))
    f_cloud = float(1.0 - np.clip(bundle.cloud_cover / 100.0, 0.0, 1.0))
    f_aod = float(np.exp(-np.clip(bundle.aod, 0.0, 5.0) * 2.0))
    f_elev = float(0.5 + 0.5 * (1.0 - np.exp(-max(bundle.elevation_m, 0.0) / 1500.0)))
    f_moon = float(np.clip(1.0 - 0.8 * bundle.moon_illumination, 0.0, 1.0))
    f_lp = float(np.clip((10.0 - bundle.bortle) / 9.0, 0.05, 1.0))
    f_pwv = float(np.exp(-max(bundle.pwv_mm, 0.0) / 40.0))
    f_horiz = float(np.clip(1.0 - bundle.horizon_deg / 20.0, 0.0, 1.0))

    w = settings
    score = (
        f_dark
        * (f_ovation ** w.weight_ovation)
        * (f_kp ** w.weight_kp)
        * (f_cloud ** w.weight_cloud)
        * (f_aod ** w.weight_aod)
        * (f_elev ** w.weight_elev)
        * (f_moon ** w.weight_moon)
        * (f_lp ** w.weight_lp)
        * (f_pwv ** w.weight_pwv)
        * (f_horiz ** w.weight_horiz)
        * 100.0
    )

    return ScoreBreakdown(
        visibility_score=float(np.clip(score, 0.0, 100.0)),
        f_dark=f_dark,
        f_ovation=f_ovation,
        f_kp=f_kp,
        f_cloud=f_cloud,
        f_aod=f_aod,
        f_elev=f_elev,
        f_moon=f_moon,
        f_lp=f_lp,
        f_pwv=f_pwv,
        f_horiz=f_horiz,
    )
