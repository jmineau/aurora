"""Aurora Alert Server – FastAPI application entry point.

Endpoints
---------
POST   /subscribe                  – add a recipient (phone, email or inReach token) + locations
DELETE /unsubscribe/{recipient}    – deactivate all subscriptions for a recipient
GET    /subscriptions/{recipient}  – list active subscriptions
GET    /check?lat=&lon=            – ad-hoc visibility check at a coordinate
POST   /report                     – ground-truth sighting
POST   /sms/inbound                – Twilio webhook for SMS Y/N replies
GET    /pairings                   – inReach conversations waiting for /subscribe
GET    /health                     – scheduler, channel and calibration status

The APScheduler background job fires every CHECK_INTERVAL_MINUTES and loops
over every active Subscription.  If the composite visibility score meets the
user's threshold AND the cooldown period has elapsed, an alert goes out through
the subscription's channel (``notify.deliver``: Twilio SMS, email or Garmin inReach).
With IMAP configured a second job polls the aurora mailbox (``inbox.py``).

Static factors (elevation, horizon elevation, Bortle class) are fetched once
on the first check and cached in the database row so subsequent checks only
hit the dynamic-data APIs (OVATION, Kp, weather, AOD).
"""

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape as xml_escape

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from aurora import inbox, messages, notify
from aurora.aurora import AuroraChecker
from aurora.config import settings
from aurora.db import AlertLog, InReachConversation, Subscription, get_db, init_db, utcnow
from aurora.feedback import parse_reply, record_observation
from aurora.geocoding import geocode, init_cache
from aurora.inreach import find_conversation
from aurora.sms import sms_keyword, validate_twilio_signature

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
                    await _send_alert(sub, result.to_dict())
                    # Cooldown starts on every attempt, failed or not: a failure
                    # is reported (log + ntfy), never retried each cycle, since an
                    # ambiguous inReach failure may still have been delivered.
                    sub.last_alerted_at = utcnow()

                db.commit()

            except Exception:
                log.exception("Error processing subscription id=%d (%s)", sub.id, sub.address)
                db.rollback()

    finally:
        db.close()

    log.info("Check cycle complete.")


async def _send_alert(sub: Subscription, d: dict) -> notify.Delivery:
    """The one delivery call site: the compact alert, sent via the channel's Notifier."""
    body = _format_alert(sub.address, d, messages.site_timezone(sub.lat, sub.lon))
    # Sends are blocking HTTP calls; keep them off the event loop.
    return await asyncio.to_thread(
        notify.deliver, sub.channel, sub.recipient, messages.ALERT_SUBJECT, body
    )


def _format_alert(address: str, d: dict, tz_name: str | None = None) -> str:
    """The alert body for every channel: one 160-char GSM-7/ASCII line."""
    return messages.format_alert(address, d, tz_name)


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
    if settings.imap_enabled:
        scheduler.add_job(
            inbox.poll_job,
            "interval",
            seconds=settings.imap_poll_seconds,
            id="imap_poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    log.info(
        "Aurora alert server started – checking every %d min.",
        settings.check_interval_minutes,
    )
    for channel, enabled in notify.enabled_channels().items():
        if not enabled:
            log.warning("Channel %r not configured – collect-only: alerts are logged, not sent.", channel)
    if settings.imap_enabled:
        log.info("Polling mailbox %s every %d s.", settings.imap_user, settings.imap_poll_seconds)
    yield
    scheduler.shutdown(wait=False)
    log.info("Scheduler stopped.")


app = FastAPI(
    title="Aurora Alert Server",
    version="1.0.0",
    description=(
        "Alerts subscribers (SMS or Garmin inReach) when aurora viewing conditions are favourable "
        "based on OVATION, cloud cover, AOD, Kp, moon phase, light pollution, "
        "terrain, and precipitable water vapour."
    ),
    lifespan=lifespan,
)


# ── Request / response schemas ────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    """Body for POST /subscribe.

    SMS (the default): ``{"phone": "+12125551234", ...}`` or
    ``{"channel": "sms", "recipient": "+12125551234", ...}``.
    inReach: ``{"channel": "inreach", "recipient": "<token>", ...}``.
    """

    channel: str = notify.DEFAULT_CHANNEL
    """Delivery channel: "sms" (default), "email" or "inreach"."""

    recipient: str | None = None
    """Channel address: E.164 phone for sms, an address for email, the extId token for inreach."""

    phone: str | None = None
    """Phone number in E.164 format (SMS shorthand for recipient)."""

    locations: list[str]
    """One or more place names or "lat,lon" strings."""

    threshold: float = 30.0
    """Visibility score (0–100) that must be met to trigger an alert."""

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str) -> str:
        v = v.strip().lower()
        notify.notifier_class(v)  # raises ValueError for an unknown channel
        return v

    @model_validator(mode="after")
    def validate_recipient(self) -> "SubscribeRequest":
        if self.recipient is None:
            if self.phone is None:
                raise ValueError("Give a recipient (or phone, for SMS).")
            if self.channel != "sms":
                raise ValueError("phone is only for channel 'sms'; use recipient.")
            self.recipient = self.phone
        self.recipient = notify.normalize_recipient(self.channel, self.recipient)
        if self.channel == "sms":
            self.phone = self.recipient
        return self

    @field_validator("threshold")
    @classmethod
    def validate_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 100.0:
            raise ValueError("Threshold must be between 0 and 100.")
        return v


class SubscriptionOut(BaseModel):
    """Serialised Subscription row returned from the API."""

    id: int
    channel: str
    recipient: str
    phone: str | None
    """Same as recipient for SMS subscriptions, else null."""
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

    recipient: str | None = None
    """Any subscriber address (phone or inReach token); alternative to phone."""

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
    recipient: str | None
    lat: float | None
    lon: float | None
    subscription_id: int | None
    alert_log_id: int | None

    model_config = {"from_attributes": True}


class PendingPairingOut(BaseModel):
    """An inReach conversation with the aurora mailbox that no subscription uses yet.

    Subscribe with ``channel="inreach"`` and ``recipient`` = ``token`` (or
    ``conversation_key``).
    """

    conversation_key: str
    token: str = Field(validation_alias="latest_token")
    sender: str | None
    last_message: str | None
    first_seen_at: dt.datetime | None
    last_seen_at: dt.datetime | None

    model_config = {"from_attributes": True}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post(
    "/subscribe",
    response_model=list[SubscriptionOut],
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe a phone (SMS) or inReach to aurora alerts at one or more locations.",
)
def subscribe(req: SubscribeRequest, db: Session = Depends(get_db)):
    # The stored recipient may differ from what was typed: an inReach token is
    # resolved to its (stable) conversation key.
    try:
        recipient = notify.get_notifier(req.channel).register_recipient(db, req.recipient)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    created: list[Subscription] = []
    for address in req.locations:
        try:
            lat, lon = geocode(address)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        sub = Subscription(
            channel=req.channel,
            recipient=recipient,
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


def _recipient_key(db: Session, recipient: str) -> str:
    """A recipient in its stored form.

    A pasted Garmin reply link becomes its token, and a known inReach token
    becomes its conversation key; an email address is lower-cased; anything else
    (a phone number) is unchanged.
    """
    if "@" in recipient:
        return recipient.strip().lower()
    value = notify.extract_token(recipient) or recipient
    conv = find_conversation(db, value)
    return conv.conversation_key if conv else value


@app.delete(
    "/unsubscribe/{recipient}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate all subscriptions for a recipient (phone number, email address or inReach token).",
)
def unsubscribe(recipient: str, db: Session = Depends(get_db)):
    subs = (
        db.query(Subscription)
        .filter(Subscription.recipient == _recipient_key(db, recipient), Subscription.active.is_(True))
        .all()
    )
    if not subs:
        raise HTTPException(
            status_code=404,
            detail="No active subscriptions found for this recipient.",
        )
    for sub in subs:
        sub.active = False
    db.commit()


@app.get(
    "/subscriptions/{recipient}",
    response_model=list[SubscriptionOut],
    summary="List active subscriptions for a recipient (phone number or inReach token).",
)
def get_subscriptions(recipient: str, db: Session = Depends(get_db)):
    return (
        db.query(Subscription)
        .filter(Subscription.recipient == _recipient_key(db, recipient), Subscription.active.is_(True))
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

    Provide *phone* or *recipient* (to link to a subscriber's check snapshots)
    and/or *location*.  The observation is linked to the nearest logged snapshot so it
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
        recipient=_recipient_key(db, req.recipient) if req.recipient else None,
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
    """Handle a subscriber's SMS: carrier keywords, then Y/N replies as labels.

    Twilio POSTs form-encoded data here.  STOP-type keywords deactivate (never
    delete) every SMS subscription for the number, START/UNSTOP reactivate them
    (YES too, but only when all are stopped, since YES is otherwise a "saw it"
    reply), and HELP gets a one-line description.  Anything else is classified
    as saw/didn't-see and linked to the most recent alert sent to that number.
    Responds with TwiML so Twilio can relay a confirmation back to the user.
    """
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validate_twilio_signature(str(request.url), dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    keyword = sms_keyword(Body)
    db = next(get_db())
    try:
        subs = db.query(Subscription).filter(
            Subscription.channel == "sms", Subscription.recipient == From
        ).all()
        if keyword == "stop":
            for sub in subs:
                sub.active = False
            db.commit()
            log.info("SMS opt-out from %s: %d subscription(s) deactivated.", From, len(subs))
            # Twilio sends its own STOP confirmation; reply with nothing.
            return _twiml(None)
        if keyword == "start" or (keyword == "yes" and subs and not any(s.active for s in subs)):
            for sub in subs:
                sub.active = True
            db.commit()
            log.info("SMS opt-in from %s: %d subscription(s) reactivated.", From, len(subs))
            return _twiml(None)
        if keyword == "help":
            return _twiml(
                "Aurora alerts: texts when the aurora may be visible. "
                "Reply Y/N after an alert, STOP to end, START to resume."
            )

        saw = parse_reply(Body)
        if saw is None:
            return _twiml(
                "Sorry, I didn't catch that. Reply Y if you saw the aurora or N if you didn't."
            )
        record_observation(
            db,
            saw_aurora=saw,
            phone=From,
            recipient=From,
            source="sms_reply",
            link="reply",
        )
    finally:
        db.close()

    # GSM-7 only: every confirmation is billed as an outbound SMS segment.
    return _twiml(
        "Thanks - logged that you saw it!" if saw else "Thanks - logged. Better luck next time!"
    )


def _twiml(message: str | None) -> Response:
    """A TwiML reply with an optional message (escaped)."""
    body = f"<Message>{xml_escape(message)}</Message>" if message else ""
    return Response(content=f"<Response>{body}</Response>", media_type="application/xml")


@app.get(
    "/pairings",
    response_model=list[PendingPairingOut],
    summary="inReach conversations with the aurora mailbox that await /subscribe.",
)
def list_pairings(db: Session = Depends(get_db)):
    subscribed = (
        db.query(Subscription.recipient)
        .filter(Subscription.channel == notify.InReachNotifier.channel, Subscription.active.is_(True))
    )
    return (
        db.query(InReachConversation)
        .filter(InReachConversation.conversation_key.not_in(subscribed))
        .order_by(InReachConversation.last_seen_at.desc())
        .all()
    )


@app.get("/health", summary="Server health and scheduler status.")
def health():
    cal = checker.calibration
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "check_interval_minutes": settings.check_interval_minutes,
        "sms_enabled": settings.sms_enabled,
        # {channel: True if it really sends, False if collect-only (logs only)}
        "channels": notify.enabled_channels(),
        "mailbox_enabled": settings.imap_enabled,
        "ntfy_enabled": bool(settings.ntfy_url),
        "calibration": (
            {"active": True, "n_samples": cal.n_samples, "n_positive": cal.n_positive,
             "trained_at": cal.trained_at}
            if cal is not None else {"active": False}
        ),
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def start() -> None:
    """Entry point for the `aurora-server` console script."""
    uvicorn.run("aurora.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    start()
