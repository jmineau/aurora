"""Lunar sky-brightness factor (illumination gated by altitude).

A bright moon only washes out the sky when it is **above the horizon**. The
illuminated fraction alone is misleading: a 90%-lit moon that has set contributes
nothing, while the same moon near the zenith is the dominant source of sky glow.

So the scoring input is an *effective* moonlight value:

    effective = illuminated_fraction × max(0, sin(altitude))

which is 0 when the moon is below the horizon, scales with how high it is (a
proxy for both the sky area it lights and its atmospheric extinction), and equals
the illuminated fraction only when the moon is at the zenith.  ``f_moon`` consumes
this effective value.

Illuminated fraction and phase are location-independent; altitude needs the
observer's coordinate, so pass lat/lon.  Omit them (e.g. in a pure phase test) and
the altitude gate is skipped (effective == illuminated fraction).

All computed locally from ephemeris via astral — no API call.
"""

import datetime as dt
import math
from dataclasses import dataclass

from astral import Observer
from astral.moon import elevation as moon_elevation
from astral.moon import phase as moon_phase


@dataclass
class MoonResult:
    illumination: float               # raw illuminated fraction, 0 (new) – 1 (full)
    phase_days: float                 # days since new moon, 0–29.53
    altitude_deg: float | None = None  # moon elevation above the horizon (None if no location)
    effective_illumination: float = 0.0  # altitude-gated moonlight – the scoring input


def fetch_moon(
    when: dt.datetime, lat: float | None = None, lon: float | None = None
) -> MoonResult:
    """Return lunar illumination, phase, altitude, and effective moonlight at *when*.

    If *lat*/*lon* are given, the effective illumination is gated by the moon's
    altitude at that location (0 when below the horizon).  Without them, the
    altitude gate is skipped and effective == illuminated fraction.
    """
    phase_days = moon_phase(date=when.date())
    phase_angle = 2.0 * math.pi * phase_days / 29.53
    illumination = max(0.0, min(1.0, (1.0 - math.cos(phase_angle)) / 2.0))

    if lat is None or lon is None:
        return MoonResult(
            illumination=illumination,
            phase_days=phase_days,
            altitude_deg=None,
            effective_illumination=illumination,
        )

    # astral expects an aware datetime; treat a naive one as UTC.
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    altitude = moon_elevation(Observer(latitude=lat, longitude=lon), when)
    ramp = max(0.0, math.sin(math.radians(altitude)))

    return MoonResult(
        illumination=illumination,
        phase_days=phase_days,
        altitude_deg=altitude,
        effective_illumination=illumination * ramp,
    )
