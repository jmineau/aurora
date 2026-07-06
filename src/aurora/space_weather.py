"""Historical space-weather reconstruction for backfill (Kp-driven oval model).

The live OVATION product (NOAA SWPC) is a nowcast and isn't archived, and the
research reconstructions (OvationPyme, driven by OMNI solar wind) don't install
cleanly on this stack.  So to give backfilled sightings a space-weather factor we
reconstruct it from **archived Kp** plus a simple **auroral-oval model**:

1. Fetch the definitive Kp for the observation time (GFZ Potsdam archive).
2. Model the auroral-presence probability at any location as a function of its
   geomagnetic latitude and Kp — the oval sits near a boundary magnetic latitude
   that moves equatorward as Kp rises.
3. Evaluate that at points marching poleward from the observer to build the same
   kind of profile the live OVATION sampler produces, then run it through the
   identical ``geometry.visible_aurora`` geometry.

This is a proxy, not the OVATION grid: it has no local-time / substructure detail
and the boundary constants are approximate (calibration will adjust the OVATION
coefficient anyway).  It also differs in source from the live SWPC feature, so
mixing many live and backfilled rows introduces some model skew — the way to
remove that entirely is to drive *both* live and historical from one reconstruction
(OvationPyme), which is a larger, own-the-model commitment.  See docs/roadmap.md.
"""

import datetime as dt
import math

import httpx

from aurora import geometry

_GFZ_KP_URL = "https://kp.gfz-potsdam.de/app/json/"

# Auroral-oval equatorward boundary (geomagnetic latitude) vs Kp.  ~66° at Kp 0,
# moving ~2.5° equatorward per Kp step.  Approximate; refine/calibrate later.
_BOUNDARY_MLAT_KP0 = 66.0
_BOUNDARY_MLAT_PER_KP = 2.5
_OVAL_WIDTH_DEG = 2.0  # softness of the equatorward edge


def oval_probability(maglat: float, kp: float) -> float:
    """Auroral-presence probability (0–100) at geomagnetic *maglat* for a given Kp.

    A logistic in (|maglat| − equatorward_boundary(kp)): ~50% at the boundary,
    rising poleward into the oval.  (Poleward of the oval into the polar cap the
    real aurora weakens again; we don't model that, but mid-latitude sampling never
    reaches those latitudes.)
    """
    boundary = _BOUNDARY_MLAT_KP0 - _BOUNDARY_MLAT_PER_KP * kp
    return 100.0 / (1.0 + math.exp(-(abs(maglat) - boundary) / _OVAL_WIDTH_DEG))


def modeled_poleward_profile(
    lat: float, lon: float, kp: float, distances: list[float]
) -> list[tuple[float, float]]:
    """Oval-model presence probability at each poleward ground point.

    Same shape as ovation.sample_poleward_profile, so it feeds the identical
    geometry.visible_aurora reduction.
    """
    bearing = geometry.geomagnetic_pole_bearing(lat, lon)
    profile = []
    for d in distances:
        plat, plon = geometry.destination_point(lat, lon, bearing, d)
        mlat = geometry.geomagnetic_latitude(plat, plon)
        profile.append((d, oval_probability(mlat, kp)))
    return profile


def _select_kp(payload: dict, when: dt.datetime) -> float | None:
    """Pick the 3-hour Kp interval containing *when* from a GFZ JSON response."""
    times = payload.get("datetime", [])
    values = payload.get("Kp", [])
    target = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    chosen = None
    for t, v in zip(times, values):
        if t <= target:
            chosen = v
        else:
            break
    if chosen is None and values:
        chosen = values[0]
    return float(chosen) if chosen is not None else None


async def fetch_kp_archive(client: httpx.AsyncClient, when: dt.datetime) -> float | None:
    """Definitive Kp for the 3-hour window containing *when* (UTC), from GFZ.

    Returns None if the archive is unreachable or has no data for the date.
    """
    when = when if when.tzinfo is None else when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    day = when.date()
    params = {
        "start": f"{day}T00:00:00Z",
        "end": f"{day}T23:59:59Z",
        "index": "Kp",
    }
    try:
        resp = await client.get(_GFZ_KP_URL, params=params, timeout=25.0, follow_redirects=True)
        resp.raise_for_status()
        return _select_kp(resp.json(), when)
    except (httpx.HTTPError, ValueError, KeyError):
        return None
