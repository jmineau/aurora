"""Viewing geometry for the aurora — where it actually appears in your sky.

Existing aurora tools (and this project's first cut) sample the OVATION aurora
probability *directly overhead* the observer.  That badly under-predicts what
mid-latitude viewers see, because the aurora emits at ~100–400 km altitude and is
therefore visible from hundreds of km away, low on the *poleward* horizon.  A
viewer in Washington State during a storm sees an oval that is physically over
northern BC, not over their head.

This module supplies the pure geometry to do it right:

1. Walk *poleward* from the observer in steps (`destination_point`).
2. At each step, the aurora emitting at height ``h`` over that ground point appears
   at some elevation angle above the observer's horizon (`elevation_angle`).
3. The aurora the observer can actually see is the OVATION probability at the
   nearest poleward point whose emission layer clears both the true horizon and
   the local terrain (`visible_aurora`).

Everything here is pure math (no I/O, no numpy) so it is fully unit-tested.  The
OVATION sampler and the scorer consume it.

Approximations (documented so they can be improved — see docs/roadmap.md):
* "Poleward" is taken as geographic north/south. The oval is organised around the
  *geomagnetic* pole (~80°N, 72°W), so this can be off by tens of degrees in
  azimuth depending on longitude. Good enough for a first cut; a geomagnetic
  bearing is the refinement.
* Observer is treated as at sea level; the emission height dominates the geometry.
* Spherical Earth.
"""

from __future__ import annotations

import math

R_EARTH_M = 6_371_000.0
# Green-line (557.7 nm) emission peaks near 110 km and dominates visual aurora.
# Red (630 nm) reaches 200–400 km and is visible farther, but is fainter to the eye.
DEFAULT_EMISSION_M = 110_000.0

# Centred-dipole geomagnetic poles (IGRF, ~2020 epoch). The auroral oval is
# organised around these, not the geographic poles — which is why western-US
# longitudes see aurora at lower *geographic* latitudes than Europe. A centred
# dipole is an approximation; corrected geomagnetic (AACGM) would be more exact.
NORTH_GEOMAGNETIC_POLE = (80.65, -72.68)   # lat, lon
SOUTH_GEOMAGNETIC_POLE = (-80.65, 107.32)


def elevation_angle(
    ground_distance_m: float,
    height_m: float = DEFAULT_EMISSION_M,
    radius_m: float = R_EARTH_M,
) -> float:
    """Elevation angle (degrees) of an emission layer above the observer's horizon.

    *ground_distance_m* is the great-circle surface distance from the observer to
    the point beneath the emission; *height_m* is the emission altitude.  Returns
    90° directly overhead (distance 0), decreasing to 0° at the geometric horizon
    and negative beyond it (below the horizon — not visible).
    """
    gamma = ground_distance_m / radius_m  # earth-centre angle, radians
    r = radius_m + height_m
    # Observer at (radius, 0); emission point at (r cos gamma, r sin gamma).
    up = r * math.cos(gamma) - radius_m       # component along local vertical
    along = r * math.sin(gamma)               # component along local horizontal
    return math.degrees(math.atan2(up, along))


def max_visible_distance(
    height_m: float = DEFAULT_EMISSION_M, radius_m: float = R_EARTH_M
) -> float:
    """Ground distance at which the emission layer sits exactly on the horizon.

    Beyond this the aurora is geometrically below the horizon.  ~1180 km for a
    110 km emission height.
    """
    gamma = math.acos(radius_m / (radius_m + height_m))
    return radius_m * gamma


def sample_distances(
    height_m: float = DEFAULT_EMISSION_M,
    step_m: float = 100_000.0,
    radius_m: float = R_EARTH_M,
) -> list[float]:
    """Poleward sampling distances from 0 out to the geometric horizon."""
    d_max = max_visible_distance(height_m, radius_m)
    n = int(d_max // step_m)
    return [step_m * i for i in range(n + 1)] + [d_max]


def poleward_bearing(lat: float) -> float:
    """Compass bearing toward the nearer geographic pole (0° N hemi, 180° S hemi)."""
    return 0.0 if lat >= 0.0 else 180.0


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle initial bearing (degrees, 0–360) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def geomagnetic_latitude(lat: float, lon: float) -> float:
    """Centred-dipole geomagnetic latitude (degrees) of a geographic point.

    The auroral oval is organised in this coordinate, so it is what the oval model
    keys on.  Positive north.  Same centred-dipole approximation as the bearing.
    """
    pole = NORTH_GEOMAGNETIC_POLE
    phi, phi_p = math.radians(lat), math.radians(pole[0])
    dlam = math.radians(lon - pole[1])
    sin_mlat = math.sin(phi) * math.sin(phi_p) + math.cos(phi) * math.cos(phi_p) * math.cos(dlam)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_mlat))))


def geomagnetic_pole_bearing(lat: float, lon: float) -> float:
    """Bearing toward the nearer geomagnetic pole — the direction the oval lies in.

    The auroral oval is centred on the geomagnetic pole, so this (not geographic
    north) is the direction to walk when sampling OVATION poleward.  At western-US
    longitudes it points markedly east of true north.
    """
    pole = NORTH_GEOMAGNETIC_POLE if lat >= 0.0 else SOUTH_GEOMAGNETIC_POLE
    return initial_bearing(lat, lon, pole[0], pole[1])


def destination_point(
    lat: float,
    lon: float,
    bearing_deg: float,
    distance_m: float,
    radius_m: float = R_EARTH_M,
) -> tuple[float, float]:
    """Great-circle destination from (lat, lon) travelling *distance_m* on *bearing*.

    Spherical model — valid over the ~1000 km poleward reach where a flat-Earth
    offset would drift badly.  Returns (lat, lon) in degrees, lon in [-180, 180].
    """
    delta = distance_m / radius_m
    brg = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)

    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(brg)
    sin_phi2 = max(-1.0, min(1.0, sin_phi2))
    phi2 = math.asin(sin_phi2)
    lam2 = lam1 + math.atan2(
        math.sin(brg) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * sin_phi2,
    )
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0  # normalise to [-180, 180]
    return math.degrees(phi2), lon2


# Assumed aurora elevation when the true value is unknown (e.g. backfill has no
# OVATION geometry). Mid-latitude aurora sits low on the horizon, so lean poleward.
DEFAULT_AURORA_ELEVATION_DEG = 8.0


def line_of_sight_cloud(
    overhead_cover: float, poleward_cover: float, elevation_deg: float | None = None
) -> float:
    """Blend overhead and poleward cloud cover by where the aurora appears.

    Aurora high in the sky is seen through the overhead cloud; aurora low on the
    poleward horizon is seen through the cloud in *that* direction.  Weighting the
    two by ``sin(elevation)`` transitions smoothly between them (overhead at the
    zenith, fully poleward at the horizon).  Uses a low default elevation when the
    aurora's elevation is unknown.
    """
    theta = DEFAULT_AURORA_ELEVATION_DEG if elevation_deg is None else elevation_deg
    w_overhead = max(0.0, min(1.0, math.sin(math.radians(theta))))
    return w_overhead * overhead_cover + (1.0 - w_overhead) * poleward_cover


def visible_aurora(
    profile: list[tuple[float, float]],
    horizon_deg: float = 0.0,
    height_m: float = DEFAULT_EMISSION_M,
    radius_m: float = R_EARTH_M,
) -> tuple[float, float | None]:
    """Reduce a poleward OVATION profile to what the observer can actually see.

    *profile* is a list of ``(ground_distance_m, ovation_probability)`` sampled
    poleward from the observer.  *horizon_deg* is the local obstruction toward the
    pole (terrain).  Returns ``(visible_probability, elevation_deg)``: the highest
    OVATION probability among points whose emission clears the horizon, and the
    elevation angle at which that aurora appears.  ``(0.0, None)`` if nothing
    clears the horizon.
    """
    gate = max(horizon_deg, 0.0)
    best_prob = 0.0
    best_elev: float | None = None
    for distance_m, prob in profile:
        elev = elevation_angle(distance_m, height_m, radius_m)
        if elev <= 0.0 or elev < gate:
            continue
        if prob > best_prob:
            best_prob = prob
            best_elev = elev
    return best_prob, best_elev
