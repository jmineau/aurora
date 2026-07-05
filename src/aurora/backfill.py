"""Backfill ground-truth observations with reconstructed conditions.

Turns a CSV of sightings (see data/observations_template.csv) into labelled
``Observation`` rows the calibration fit can train on.  Because these are
historical — the alert server wasn't running then — there is no logged snapshot,
so we *reconstruct* the factor vector for each observation's time and place from
reanalysis and store it as a (``backfilled``) ``AlertLog`` the observation links to.

Phase 1 reconstructs the atmospheric/visibility and static factors:

  cloud (ERA5 archive) · PWV · AOD (CAMS archive) · elevation · poleward horizon
  · Bortle · moon

The **space-weather** factors (OVATION probability, Kp) are left NULL — historical
OVATION isn't archived and reconstructing it is a separate track (see
docs/roadmap.md).  The fit imputes those NULLs to neutral, so backfilled rows
calibrate the *visibility* half of the model — the part a photo (which proves the
aurora was present) is best suited to.

CSV columns: observed_at_local, lat, lon, place, saw, intensity, notes.  Local
time is resolved to UTC using the time zone at the coordinate.

Usage::

    uv run aurora-import [path/to/observations.csv]
"""

import argparse
import asyncio
import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from aurora.db import AlertLog, Observation, SessionLocal, init_db
from aurora.factors.aod import fetch_aod_archive
from aurora.factors.light_pollution import fetch_light_pollution
from aurora.factors.moon import fetch_moon
from aurora.factors.terrain import fetch_terrain
from aurora.factors.weather import fetch_weather_archive
from aurora.geocoding import geocode

_DEFAULT_CSV = Path("data/observations.csv")
_YES = {"y", "yes", "true", "1"}
_NO = {"n", "no", "false", "0"}


@dataclass
class ObsRow:
    observed_at_local: str
    lat: float | None
    lon: float | None
    place: str | None
    saw: bool
    intensity: int | None
    notes: str | None


def parse_csv(path: Path) -> list[ObsRow]:
    """Parse the observations CSV into rows, skipping blanks and EXAMPLE rows."""
    rows: list[ObsRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            saw_str = (raw.get("saw") or "").strip().lower()
            when = (raw.get("observed_at_local") or "").strip()
            notes = (raw.get("notes") or "").strip()
            if not when or saw_str not in _YES | _NO or "EXAMPLE" in notes.upper():
                continue
            rows.append(ObsRow(
                observed_at_local=when,
                lat=_to_float(raw.get("lat")),
                lon=_to_float(raw.get("lon")),
                place=(raw.get("place") or "").strip() or None,
                saw=saw_str in _YES,
                intensity=_to_int(raw.get("intensity")),
                notes=notes or None,
            ))
    return rows


def _to_float(v: str | None) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


def _to_int(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(float(v)) if v else None


# timezonefinder loads a data file on init; build one lazily and reuse it.
_tf = None


def _resolve_latlon(row: ObsRow) -> tuple[float, float]:
    if row.lat is not None and row.lon is not None:
        return row.lat, row.lon
    if row.place:
        return geocode(row.place)
    raise ValueError("row has neither lat/lon nor a geocodable place")


def resolve_utc(local_str: str, lat: float, lon: float) -> dt.datetime:
    """Convert a local wall-clock 'YYYY-MM-DD HH:MM' to naive UTC via the site's tz."""
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    naive_local = dt.datetime.strptime(local_str.strip(), "%Y-%m-%d %H:%M")
    tzname = _tf.timezone_at(lat=lat, lng=lon) or "UTC"
    aware = naive_local.replace(tzinfo=ZoneInfo(tzname))
    return aware.astimezone(dt.timezone.utc).replace(tzinfo=None)


async def reconstruct_factors(
    client: httpx.AsyncClient, lat: float, lon: float, when: dt.datetime
) -> dict:
    """Reconstruct the atmospheric/static/moon factors at (lat, lon, when).

    Space-weather factors (ovation_prob, kp_index) are intentionally left None.
    """
    weather, aod, terrain = await asyncio.gather(
        fetch_weather_archive(client, lat, lon, when),
        fetch_aod_archive(client, lat, lon, when),
        fetch_terrain(client, lat, lon),
    )
    moon = fetch_moon(when)
    lp = fetch_light_pollution(lat, lon)
    return {
        "cloud_cover": weather.cloud_cover,
        "pwv_mm": weather.pwv_mm,
        "aod": aod.aod,
        "elevation_m": terrain.elevation_m,
        "horizon_deg": terrain.horizon_deg,
        "moon_illumination": moon.illumination,
        "bortle": lp.bortle,
    }


def _already_imported(db, when: dt.datetime, lat: float, lon: float) -> bool:
    return (
        db.query(Observation)
        .filter(
            Observation.source == "backfill",
            Observation.observed_at == when,
            Observation.lat == lat,
            Observation.lon == lon,
        )
        .first()
        is not None
    )


async def import_row(db, client: httpx.AsyncClient, row: ObsRow) -> str:
    """Reconstruct and store one observation; returns a status string."""
    lat, lon = _resolve_latlon(row)
    when = resolve_utc(row.observed_at_local, lat, lon)

    if _already_imported(db, when, lat, lon):
        return f"skip (already imported): {row.observed_at_local} @ {lat:.3f},{lon:.3f}"

    factors = await reconstruct_factors(client, lat, lon, when)
    snapshot = AlertLog(
        checked_at=when,
        backfilled=True,
        ovation_prob=None,   # space weather not reconstructed (phase 1)
        kp_index=None,
        visibility_score=None,
        alerted=False,
        **factors,
    )
    db.add(snapshot)
    db.flush()  # assign snapshot.id

    db.add(Observation(
        observed_at=when,
        saw_aurora=row.saw,
        intensity=row.intensity,
        source="backfill",
        note=row.notes,
        lat=lat,
        lon=lon,
        alert_log_id=snapshot.id,
    ))
    db.commit()
    seen = "SAW" if row.saw else "none"
    return (f"imported [{seen}] {row.observed_at_local} @ {lat:.3f},{lon:.3f}  "
            f"cloud={factors['cloud_cover']:.0f}% moon={factors['moon_illumination']:.2f} "
            f"bortle={factors['bortle']:.1f}")


async def run(path: Path) -> None:
    rows = parse_csv(path)
    print(f"Parsed {len(rows)} observation(s) from {path}.")
    init_db()
    db = SessionLocal()
    imported = 0
    try:
        async with httpx.AsyncClient() as client:
            for row in rows:
                try:
                    status = await import_row(db, client, row)
                    print(f"  {status}")
                    imported += status.startswith("imported")
                except Exception as exc:  # keep going on a bad row
                    db.rollback()
                    print(f"  ERROR on {row.observed_at_local}: {type(exc).__name__}: {exc}")
    finally:
        db.close()
    print(f"Done. {imported} new observation(s) imported.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill observations from a CSV.")
    parser.add_argument("csv", nargs="?", default=str(_DEFAULT_CSV),
                        help=f"Path to the observations CSV (default: {_DEFAULT_CSV}).")
    args = parser.parse_args()
    asyncio.run(run(Path(args.csv)))


if __name__ == "__main__":
    main()
