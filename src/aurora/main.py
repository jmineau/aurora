"""Aurora Alert Server – FastAPI application entry point.

Endpoints
---------
POST   /subscribe                  – add a phone number + locations
DELETE /unsubscribe/{phone}        – deactivate all subscriptions for a number
GET    /subscriptions/{phone}      – list active subscriptions
GET    /check?lat=&lon=            – ad-hoc visibility check at a coordinate
GET    /health                     – scheduler status

The APScheduler background job fires every CHECK_INTERVAL_MINUTES and loops
over every active Subscription.  If the composite visibility score meets the
user's threshold AND the cooldown period has elapsed, a Twilio SMS is sent.

Static factors (elevation, horizon elevation, Bortle class) are fetched once
on the first check and cached in the database row so subsequent checks only
hit the dynamic-data APIs (OVATION, Kp, weather, AOD).
"""

import datetime as dt
import logging
import re
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from aurora.aurora import AuroraChecker
from aurora.config import settings
from aurora.db import AlertLog, Subscription, get_db, init_db, utcnow
from aurora.feedback import parse_reply, record_observation
from aurora.geocoding import geocode, init_cache
from aurora.sms import send_sms, validate_twilio_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)

checker = AuroraChecker()
scheduler = AsyncIOScheduler()


# ── Scheduled job ─────────────────────────────────────────────────────────────

async def check_all_subscriptions() -> None:
    """Check aurora conditions for every active subscription.

    Called by APScheduler every CHECK_INTERVAL_MINUTES.  For each
    subscription the checker is invoked with any cached static factors so
    only dynamic data (OVATION, Kp, weather, AOD) is re-fetched.
    """
    log.info("Starting aurora check cycle.")
    db = next(get_db())
    try:
        subs = db.query(Subscription).filter(Subscription.active.is_(True)).all()
        log.info("Checking %d active subscription(s).", len(subs))

        for sub in subs:
            try:
                result = await checker.check(
                    sub.lat,
                    sub.lon,
                    elevation_m=sub.elevation_m,
                    horizon_deg=sub.horizon_deg,
                    bortle=sub.bortle,
                )

                # Persist static factors on the first check.
                if sub.elevation_m is None:
                    sub.elevation_m = result.terrain.elevation_m
                    sub.horizon_deg = result.terrain.horizon_deg
                if sub.bortle is None:
                    sub.bortle = result.light_pollution.bortle

                cooldown = dt.timedelta(hours=settings.alert_cooldown_hours)
                on_cooldown = (
                    sub.last_alerted_at is not None
                    and utcnow() - sub.last_alerted_at < cooldown
                )
                should_alert = (
                    result.score.visibility_score >= sub.threshold
                    and not on_cooldown
                )

                log.info(
                    "sub=%d  %s  score=%.1f  threshold=%.1f  alert=%s",
                    sub.id,
                    sub.address,
                    result.score.visibility_score,
                    sub.threshold,
                    should_alert,
                )

                db.add(AlertLog(
                    subscription_id=sub.id,
                    # Log the geometry-aware probability — the feature the score
                    # uses and the calibration fit trains on.
                    ovation_prob=(
                        result.ovation.visible_probability
                        if result.ovation.visible_probability is not None
                        else result.ovation.probability
                    ),
                    kp_index=result.kp.kp_index,
                    cloud_cover=result.weather.cloud_cover,
                    aod=result.aod.aod,
                    elevation_m=result.terrain.elevation_m,
                    horizon_deg=result.terrain.horizon_deg,
                    bortle=result.light_pollution.bortle,
                    moon_illumination=result.moon.illumination,
                    pwv_mm=result.weather.pwv_mm,
                    visibility_score=result.score.visibility_score,
                    alerted=should_alert,
                ))

                if should_alert:
                    send_sms(sub.phone, _format_alert(sub.address, result.to_dict()))
                    sub.last_alerted_at = utcnow()

                db.commit()

            except Exception:
                log.exception("Error processing subscription id=%d (%s)", sub.id, sub.address)
                db.rollback()

    finally:
        db.close()

    log.info("Check cycle complete.")


def _elevation_str(d: dict) -> str:
    """' at N°' where the aurora appears above the poleward horizon, if known."""
    elev = d.get("aurora_elevation_deg")
    return f" at {elev:.0f}°" if elev is not None else ""


def _moon_alt_str(d: dict) -> str:
    """Flag when the moon is below the horizon (so its brightness doesn't count)."""
    alt = d.get("moon_altitude_deg")
    return " (down)" if alt is not None and alt <= 0 else ""


def _format_alert(address: str, d: dict) -> str:
    """Compose the SMS body for an aurora alert."""
    headline = (
        f"Chance you see it: {d['visibility_score']:.0f}%"
        if d.get("calibrated")
        else f"Score         : {d['visibility_score']:.0f}/100"
    )
    return (
        f"Aurora Alert! Conditions look favourable at {address}.\n"
        f"{headline}\n"
        f"Aurora        : {d['ovation_probability']:.0f}%"
        f"{_elevation_str(d)}   Kp: {d['kp_index']:.1f}\n"
        f"Cloud         : {d['cloud_cover_pct']:.0f}% overhead / {d['cloud_poleward_pct']:.0f}% toward oval\n"
        f"AOD 550 nm    : {d['aod_550nm']:.2f}   PWV: {d['pwv_mm']:.0f} mm\n"
        f"Moon          : {d['moon_illumination']*100:.0f}%{_moon_alt_str(d)}   "
        f"Bortle: {d['bortle']:.0f}   Horizon: {d['horizon_deg']:.1f}°\n"
        f"Forecast time : {d['forecast_time']}\n"
        f"\nDid you see it? Reply Y or N — it trains the model."
    )


# ── App lifespan (startup / shutdown) ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database, geocode cache, and background scheduler."""
    init_db()
    init_cache()
    scheduler.add_job(
        check_all_subscriptions,
        "interval",
        minutes=settings.check_interval_minutes,
        id="aurora_check",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "Aurora alert server started – checking every %d min.",
        settings.check_interval_minutes,
    )
    yield
    scheduler.shutdown(wait=False)
    log.info("Scheduler stopped.")


app = FastAPI(
    title="Aurora Alert Server",
    version="1.0.0",
    description=(
        "Texts subscribers when aurora viewing conditions are favourable "
        "based on OVATION, cloud cover, AOD, Kp, moon phase, light pollution, "
        "terrain, and precipitable water vapour."
    ),
    lifespan=lifespan,
)


# ── Request / response schemas ────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    """Body for POST /subscribe."""

    phone: str
    """Phone number in E.164 format, e.g. +12125551234."""

    locations: list[str]
    """One or more place names or "lat,lon" strings."""

    threshold: float = 30.0
    """Visibility score (0–100) that must be met to trigger an alert."""

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        cleaned = re.sub(r"[\s\-]", "", v)
        if not re.fullmatch(r"\+[1-9]\d{6,14}", cleaned):
            raise ValueError(
                "Phone must be in E.164 format (e.g. +12125551234 or +447911123456)."
            )
        return cleaned

    @field_validator("threshold")
    @classmethod
    def validate_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 100.0:
            raise ValueError("Threshold must be between 0 and 100.")
        return v


class SubscriptionOut(BaseModel):
    """Serialised Subscription row returned from the API."""

    id: int
    phone: str
    address: str
    lat: float
    lon: float
    threshold: float
    active: bool
    elevation_m: float | None
    horizon_deg: float | None
    bortle: float | None

    model_config = {"from_attributes": True}


class ReportRequest(BaseModel):
    """Body for POST /report – a ground-truth aurora sighting (or non-sighting)."""

    saw_aurora: bool
    """True if the aurora was visible, False if it wasn't."""

    phone: str | None = None
    """Phone of a subscriber, used to link the report to their check snapshots."""

    location: str | None = None
    """Place name or "lat,lon" where the observation was made (for ad-hoc reports)."""

    observed_at: dt.datetime | None = None
    """When it was observed (UTC). Defaults to now."""

    intensity: int | None = None
    """Optional strength: 0 nothing, 1 faint/camera, 2 visible glow, 3 bright."""

    note: str | None = None

    @field_validator("intensity")
    @classmethod
    def validate_intensity(cls, v: int | None) -> int | None:
        if v is not None and not 0 <= v <= 3:
            raise ValueError("intensity must be between 0 and 3.")
        return v


class ObservationOut(BaseModel):
    """Serialised Observation row."""

    id: int
    observed_at: dt.datetime
    saw_aurora: bool
    intensity: int | None
    source: str
    phone: str | None
    lat: float | None
    lon: float | None
    subscription_id: int | None
    alert_log_id: int | None

    model_config = {"from_attributes": True}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post(
    "/subscribe",
    response_model=list[SubscriptionOut],
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe a phone number to aurora alerts at one or more locations.",
)
def subscribe(req: SubscribeRequest, db: Session = Depends(get_db)):
    created: list[Subscription] = []
    for address in req.locations:
        try:
            lat, lon = geocode(address)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        sub = Subscription(
            phone=req.phone,
            address=address,
            lat=lat,
            lon=lon,
            threshold=req.threshold,
        )
        db.add(sub)
        created.append(sub)

    db.commit()
    for sub in created:
        db.refresh(sub)
    return created


@app.delete(
    "/unsubscribe/{phone}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate all subscriptions for a phone number.",
)
def unsubscribe(phone: str, db: Session = Depends(get_db)):
    subs = (
        db.query(Subscription)
        .filter(Subscription.phone == phone, Subscription.active.is_(True))
        .all()
    )
    if not subs:
        raise HTTPException(
            status_code=404,
            detail="No active subscriptions found for this number.",
        )
    for sub in subs:
        sub.active = False
    db.commit()


@app.get(
    "/subscriptions/{phone}",
    response_model=list[SubscriptionOut],
    summary="List active subscriptions for a phone number.",
)
def get_subscriptions(phone: str, db: Session = Depends(get_db)):
    return (
        db.query(Subscription)
        .filter(Subscription.phone == phone, Subscription.active.is_(True))
        .all()
    )


@app.get(
    "/check",
    summary="Check current aurora viewing conditions at a coordinate.",
    description=(
        "Returns all factor values and the composite visibility score. "
        "Useful for exploring conditions at a location before subscribing."
    ),
)
async def check_conditions(lat: float, lon: float):
    try:
        result = await checker.check(lat, lon)
        return result.to_dict()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream API error: {exc}")


@app.post(
    "/report",
    response_model=ObservationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Report whether the aurora was actually visible (ground truth for calibration).",
)
def report_observation(req: ReportRequest, db: Session = Depends(get_db)):
    """Record a ground-truth sighting used to calibrate the model.

    Provide *phone* (to link to a subscriber's check snapshots) and/or
    *location*.  The observation is linked to the nearest logged snapshot so it
    carries the factor vector that was present at the time.
    """
    lat = lon = None
    if req.location:
        try:
            lat, lon = geocode(req.location)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    obs = record_observation(
        db,
        saw_aurora=req.saw_aurora,
        observed_at=req.observed_at,
        phone=req.phone,
        lat=lat,
        lon=lon,
        intensity=req.intensity,
        note=req.note,
        source="report_api",
        link="nearest",
    )
    return obs


@app.post(
    "/sms/inbound",
    summary="Twilio inbound-SMS webhook: capture Y/N replies to alerts as labels.",
    include_in_schema=False,
)
async def sms_inbound(request: Request, From: str = Form(""), Body: str = Form("")):
    """Handle a subscriber's SMS reply to an alert and store it as an observation.

    Twilio POSTs form-encoded data here.  We classify the body as saw/didn't-see
    and link it to the most recent alert sent to that number.  Responds with
    TwiML so Twilio can relay a confirmation back to the user.
    """
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validate_twilio_signature(str(request.url), dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    saw = parse_reply(Body)
    if saw is None:
        return Response(
            content=(
                "<Response><Message>Sorry, I didn't catch that. "
                "Reply Y if you saw the aurora or N if you didn't.</Message></Response>"
            ),
            media_type="application/xml",
        )

    db = next(get_db())
    try:
        record_observation(
            db,
            saw_aurora=saw,
            phone=From,
            source="sms_reply",
            link="reply",
        )
    finally:
        db.close()

    reply = "Thanks — logged that you saw it! 🌌" if saw else "Thanks — logged. Better luck next time!"
    return Response(
        content=f"<Response><Message>{reply}</Message></Response>",
        media_type="application/xml",
    )


@app.get("/health", summary="Server health and scheduler status.")
def health():
    cal = checker.calibration
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "check_interval_minutes": settings.check_interval_minutes,
        "calibration": (
            {"active": True, "n_samples": cal.n_samples, "n_positive": cal.n_positive,
             "trained_at": cal.trained_at}
            if cal is not None else {"active": False}
        ),
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def start() -> None:
    """Entry point for the `aurora-server` console script."""
    uvicorn.run("aurora.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    start()
