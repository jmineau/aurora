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
from dataclasses import dataclass

import httpx

from aurora.config import settings
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
            "ovation_probability": round(self.ovation.probability, 1),
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
    """

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

        # Moon is a local calculation – no I/O.
        moon: MoonResult = fetch_moon(when)

        # Bortle raster lookup is a fast in-process read.
        lp: LightPollutionResult = (
            fetch_light_pollution(lat, lon) if need_lp
            else LightPollutionResult(bortle=bortle)  # type: ignore[arg-type]
        )

        bundle = FactorBundle(
            ovation_prob=ovation.probability,
            kp_index=kp.kp_index,
            cloud_cover=weather.cloud_cover,
            aod=aod.aod,
            elevation_m=terrain.elevation_m,
            moon_illumination=moon.illumination,
            bortle=lp.bortle,
            pwv_mm=weather.pwv_mm,
            horizon_deg=terrain.horizon_deg,
            lat=lat,
            lon=lon,
            when=when,
        )
        score = compute_score(bundle, settings)

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
