"""AuroraChecker – orchestrates all factor fetches and returns a scored result.

Static factors (elevation, horizon, Bortle) are expensive to fetch but
never change.  The checker accepts them as optional keyword arguments; if
provided (from the Subscription cache in the DB), the relevant API calls
are skipped.  If absent, they are fetched and returned so the caller can
persist them.

Dynamic factors (OVATION, Kp, weather, AOD) are always fetched fresh.
All async fetches are issued concurrently with asyncio.gather.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass

import httpx

from aurora import calibration as cal_module
from aurora import geometry
from aurora.calibration import Calibration
from aurora.config import settings

log = logging.getLogger(__name__)
from aurora.factors.aod import AODResult, fetch_aod
from aurora.factors.kp import KpResult, fetch_kp
from aurora.factors.light_pollution import LightPollutionResult, fetch_light_pollution
from aurora.factors.moon import MoonResult, fetch_moon
from aurora.factors.ovation import OVATIONResult, fetch_ovation
from aurora.factors.terrain import TerrainResult, fetch_terrain
from aurora.factors.weather import WeatherResult, fetch_weather
from aurora.score import FactorBundle, ScoreBreakdown, compute_score


@dataclass
class CheckResult:
    """All factor results plus the final scored breakdown."""

    lat: float
    lon: float
    ovation: OVATIONResult
    kp: KpResult
    weather: WeatherResult
    aod: AODResult
    terrain: TerrainResult
    moon: MoonResult
    light_pollution: LightPollutionResult
    when: dt.datetime
    score: ScoreBreakdown

    def to_dict(self) -> dict:
        """Serialize to a plain dict suitable for JSON responses and SMS formatting."""
        return {
            "lat": self.lat,
            "lon": self.lon,
            "visibility_score": round(self.score.visibility_score, 1),
            "calibrated": self.score.is_calibrated,
            "probability": (
                round(self.score.probability, 3)
                if self.score.probability is not None else None
            ),
            "heuristic_score": (
                round(self.score.heuristic_score, 1)
                if self.score.heuristic_score is not None else None
            ),
            # Geometry-aware probability that drives the score (poleward, above the
            # horizon); overhead probability kept for reference.
            "ovation_probability": round(
                self.ovation.visible_probability
                if self.ovation.visible_probability is not None
                else self.ovation.probability,
                1,
            ),
            "ovation_overhead_pct": round(self.ovation.probability, 1),
            "aurora_elevation_deg": (
                round(self.ovation.visible_elevation_deg, 1)
                if self.ovation.visible_elevation_deg is not None
                else None
            ),
            "kp_index": round(self.kp.kp_index, 1),
            "cloud_cover_pct": round(self.weather.cloud_cover, 1),
            "low_cloud_pct": round(self.weather.low_cloud, 1),
            "mid_cloud_pct": round(self.weather.mid_cloud, 1),
            "high_cloud_pct": round(self.weather.high_cloud, 1),
            "aod_550nm": round(self.aod.aod, 3),
            "pwv_mm": round(self.weather.pwv_mm, 1),
            "elevation_m": round(self.terrain.elevation_m, 0),
            "horizon_deg": round(self.terrain.horizon_deg, 1),
            "bortle": round(self.light_pollution.bortle, 1),
            "moon_illumination": round(self.moon.illumination, 2),
            "moon_altitude_deg": (
                round(self.moon.altitude_deg, 1)
                if self.moon.altitude_deg is not None else None
            ),
            "moon_effective": round(self.moon.effective_illumination, 2),
            "forecast_time": self.ovation.forecast_time.isoformat(),
            "checked_at": self.when.isoformat(),
            "factors": {
                "dark": round(self.score.f_dark, 2),
                "ovation": round(self.score.f_ovation, 2),
                "kp": round(self.score.f_kp, 2),
                "cloud": round(self.score.f_cloud, 2),
                "aod": round(self.score.f_aod, 2),
                "elev": round(self.score.f_elev, 2),
                "moon": round(self.score.f_moon, 2),
                "lp": round(self.score.f_lp, 2),
                "pwv": round(self.score.f_pwv, 2),
                "horiz": round(self.score.f_horiz, 2),
            },
        }


class AuroraChecker:
    """Fetch all atmospheric factors and compute the visibility score.

    Usage::

        checker = AuroraChecker()
        result = await checker.check(lat=64.2, lon=-21.9)

    If a fitted calibration (data/calibration.json) is present it is loaded and
    used to report a calibrated P(saw aurora); otherwise scoring falls back to the
    hand-tuned weighted product.  Call reload_calibration() after re-fitting.
    """

    def __init__(self) -> None:
        self.calibration: Calibration | None = Calibration.load()
        if self.calibration is not None:
            log.info(
                "Loaded calibration (n=%d, positives=%d) – scoring on P(saw).",
                self.calibration.n_samples,
                self.calibration.n_positive,
            )

    def reload_calibration(self) -> None:
        """Re-read data/calibration.json (e.g. after running aurora-calibrate)."""
        self.calibration = Calibration.load()

    async def check(
        self,
        lat: float,
        lon: float,
        *,
        elevation_m: float | None = None,
        horizon_deg: float | None = None,
        bortle: float | None = None,
    ) -> CheckResult:
        """Run a full aurora visibility check at (lat, lon).

        If *elevation_m*, *horizon_deg*, or *bortle* are supplied they are
        used directly, avoiding the corresponding API calls.  Pass None to
        force a fresh fetch (e.g. on first check for a new subscription).
        """
        when = dt.datetime.now(dt.timezone.utc)

        need_terrain = elevation_m is None or horizon_deg is None
        need_lp = bortle is None

        async with httpx.AsyncClient() as client:
            # Build the coroutine list dynamically based on what's cached.
            coros = [
                fetch_ovation(client, lat, lon),
                fetch_kp(client),
                fetch_weather(client, lat, lon),
                fetch_aod(client, lat, lon),
            ]
            if need_terrain:
                coros.append(fetch_terrain(client, lat, lon))

            results = await asyncio.gather(*coros)

        ovation: OVATIONResult = results[0]
        kp: KpResult = results[1]
        weather: WeatherResult = results[2]
        aod: AODResult = results[3]

        if need_terrain:
            terrain: TerrainResult = results[4]
        else:
            terrain = TerrainResult(
                elevation_m=elevation_m,  # type: ignore[arg-type]
                horizon_deg=horizon_deg,  # type: ignore[arg-type]
            )

        # Moon is a local calculation – no I/O.  Altitude-gated by (lat, lon).
        moon: MoonResult = fetch_moon(when, lat, lon)

        # Bortle raster lookup is a fast in-process read.
        lp: LightPollutionResult = (
            fetch_light_pollution(lat, lon) if need_lp
            else LightPollutionResult(bortle=bortle)  # type: ignore[arg-type]
        )

        # Project the poleward OVATION oval onto the observer's sky: the aurora
        # they can see is the probability at the nearest poleward point whose
        # emission clears the (poleward) horizon.  This replaces sampling the
        # probability overhead, which under-predicts mid-latitude sightings.
        visible_prob, visible_elev = geometry.visible_aurora(
            ovation.poleward_profile,
            horizon_deg=terrain.horizon_deg,
            height_m=settings.aurora_emission_km * 1000.0,
        )
        ovation.visible_probability = visible_prob
        ovation.visible_elevation_deg = visible_elev

        bundle = FactorBundle(
            ovation_prob=visible_prob,
            kp_index=kp.kp_index,
            cloud_cover=weather.cloud_cover,
            aod=aod.aod,
            elevation_m=terrain.elevation_m,
            moon_illumination=moon.effective_illumination,
            bortle=lp.bortle,
            pwv_mm=weather.pwv_mm,
            horizon_deg=terrain.horizon_deg,
            lat=lat,
            lon=lon,
            when=when,
        )
        score = compute_score(bundle, settings)

        # If a fitted model is loaded, report the calibrated P(saw aurora) as the
        # score (still gated by darkness); the weighted product is kept as
        # heuristic_score.  Falls back to the heuristic when no calibration exists.
        if self.calibration is not None:
            cal_module.apply_calibration(score, bundle, self.calibration)

        return CheckResult(
            lat=lat,
            lon=lon,
            ovation=ovation,
            kp=kp,
            weather=weather,
            aod=aod,
            terrain=terrain,
            moon=moon,
            light_pollution=lp,
            when=when,
            score=score,
        )
