"""Lunar illumination fraction from the astral library.

A bright moon raises the sky background and reduces the contrast of faint
aurora, similar to a moderate level of light pollution.  Full moon (fraction
= 1.0) can wash out all but the most intense aurora displays.

The moon phase angle is computed entirely locally from ephemeris data – no
API call required.
"""

import datetime as dt
import math
from dataclasses import dataclass

from astral.moon import phase as moon_phase


@dataclass
class MoonResult:
    illumination: float  # lunar illumination fraction, 0 (new) – 1 (full)
    phase_days: float    # days since new moon, 0–29.53


def fetch_moon(when: dt.datetime) -> MoonResult:
    """Return the lunar illumination fraction at *when* (UTC).

    astral.moon.phase() returns days since the last new moon (0–29.53).
    The illumination fraction follows a sinusoidal approximation:
        fraction = (1 - cos(phase_angle)) / 2
    where phase_angle = 2π × days / 29.53.
    """
    phase_days = moon_phase(date=when.date())
    phase_angle = 2.0 * math.pi * phase_days / 29.53
    illumination = (1.0 - math.cos(phase_angle)) / 2.0

    return MoonResult(
        illumination=max(0.0, min(1.0, illumination)),
        phase_days=phase_days,
    )
