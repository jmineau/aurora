"""Recording and linking ground-truth aurora observations for calibration.

An :class:`~aurora.db.Observation` is a labelled data point: did a user actually
see the aurora at a place and time?  To be useful for the calibration fit, each
label needs the *factor vector* that was present when it was made.  We don't
re-fetch that (conditions drift); instead we link the observation to the nearest
``AlertLog`` snapshot, since the check loop logs one snapshot per cycle per
subscription regardless of whether an alert fired.

Two linking strategies:

* **reply** – an SMS "Y/N" answers a specific alert, so we link to the most
  recent *alerted* snapshot for that phone.
* **nearest** – an unsolicited report at some ``observed_at`` links to the
  closest snapshot in time (alerted or not) within a tolerance window.

Datetimes are normalised to naive UTC to match the rest of the DB (see the
datetime-hygiene item in docs/roadmap.md).
"""

import datetime as dt

from sqlalchemy.orm import Session

from aurora.db import AlertLog, Observation, Subscription, utcnow

# How far from observed_at a snapshot may be and still be considered the same
# night's conditions.
_LINK_WINDOW = dt.timedelta(hours=3)
# How far back an SMS reply may reach to find the alert it is answering.
_REPLY_WINDOW = dt.timedelta(hours=24)

_YES = {"y", "yes", "yeah", "yep", "yup", "saw", "seen", "visible", "1"}
_NO = {"n", "no", "nope", "nada", "nothing", "clouds", "cloudy", "0"}


def parse_reply(body: str) -> bool | None:
    """Interpret an inbound SMS body as saw-aurora yes/no.

    Returns True for an affirmative, False for a negative, None if the message
    can't be classified (caller should ask the user to reply Y or N).
    """
    if not body:
        return None
    first = body.strip().lower().split()
    if not first:
        return None
    token = first[0].strip(".!,;:")
    if token in _YES:
        return True
    if token in _NO:
        return False
    return None


def _naive_utc(when: dt.datetime | None) -> dt.datetime:
    """Return *when* as naive UTC; default to now if None."""
    if when is None:
        return utcnow()
    if when.tzinfo is not None:
        when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return when


def _subscription_ids(db: Session, *, subscription_id: int | None, phone: str | None) -> list[int]:
    if subscription_id is not None:
        return [subscription_id]
    if phone is not None:
        return [s.id for s in db.query(Subscription).filter(Subscription.phone == phone).all()]
    return []


def _nearest_snapshot(
    db: Session, sub_ids: list[int], observed_at: dt.datetime
) -> AlertLog | None:
    """Closest AlertLog (any outcome) to *observed_at* within _LINK_WINDOW."""
    if not sub_ids:
        return None
    rows = db.query(AlertLog).filter(AlertLog.subscription_id.in_(sub_ids)).all()
    best: AlertLog | None = None
    best_delta = _LINK_WINDOW.total_seconds()
    for row in rows:
        if row.checked_at is None:
            continue
        delta = abs((row.checked_at - observed_at).total_seconds())
        if delta <= best_delta:
            best, best_delta = row, delta
    return best


def _latest_alerted_snapshot(db: Session, sub_ids: list[int]) -> AlertLog | None:
    """Most recent snapshot that actually fired an alert, within _REPLY_WINDOW.

    This is the alert an SMS reply is answering.
    """
    if not sub_ids:
        return None
    cutoff = utcnow() - _REPLY_WINDOW
    return (
        db.query(AlertLog)
        .filter(
            AlertLog.subscription_id.in_(sub_ids),
            AlertLog.alerted.is_(True),
            AlertLog.checked_at >= cutoff,
        )
        .order_by(AlertLog.checked_at.desc())
        .first()
    )


def record_observation(
    db: Session,
    *,
    saw_aurora: bool,
    observed_at: dt.datetime | None = None,
    phone: str | None = None,
    subscription_id: int | None = None,
    lat: float | None = None,
    lon: float | None = None,
    intensity: int | None = None,
    source: str = "report_api",
    note: str | None = None,
    link: str = "nearest",
) -> Observation:
    """Persist an observation, linking it to the best AlertLog snapshot.

    *link* selects the strategy: ``"reply"`` links to the most recent alerted
    snapshot for the phone (for SMS answers); ``"nearest"`` links to the closest
    snapshot in time to *observed_at* (for unsolicited reports).
    """
    observed_at = _naive_utc(observed_at)
    sub_ids = _subscription_ids(db, subscription_id=subscription_id, phone=phone)

    if link == "reply":
        snapshot = _latest_alerted_snapshot(db, sub_ids)
    else:
        snapshot = _nearest_snapshot(db, sub_ids, observed_at)

    obs = Observation(
        observed_at=observed_at,
        saw_aurora=saw_aurora,
        intensity=intensity,
        source=source,
        note=note,
        phone=phone,
        lat=lat,
        lon=lon,
        subscription_id=subscription_id or (snapshot.subscription_id if snapshot else None),
        alert_log_id=snapshot.id if snapshot else None,
    )
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs
