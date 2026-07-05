"""SQLAlchemy ORM models.

Three tables:
  Subscription – one row per (phone, location) pair.  Static atmospheric
                 factors (elevation, horizon, Bortle) are cached here after
                 the first check so they don't need to be re-fetched.
  AlertLog     – one row per check cycle per subscription, whether or not an
                 alert was sent.  This is the feature store for calibration:
                 every row carries the full factor vector + score + outcome.
  Observation  – a ground-truth label: did a user actually see the aurora at a
                 place and time?  Linked to the nearest AlertLog snapshot so it
                 inherits that check's factor vector.  See docs/roadmap.md and
                 AGENTS.md (Calibration).
"""

import datetime as dt

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from aurora.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine)


def utcnow() -> dt.datetime:
    """Current UTC time as a naive datetime.

    All timestamps in the DB are naive UTC (SQLite has no timezone support, so
    mixing aware/naive values would raise on comparison). Use this everywhere a
    DB-bound "now" is needed, instead of the deprecated datetime.utcnow().
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, nullable=False, index=True)
    address = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    # Minimum composite visibility score (0–100) that triggers an alert.
    threshold = Column(Float, default=30.0)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    last_alerted_at = Column(DateTime, nullable=True)

    # Static atmospheric factors cached after the first check.
    elevation_m = Column(Float, nullable=True)
    horizon_deg = Column(Float, nullable=True)
    bortle = Column(Float, nullable=True)

    alerts = relationship("AlertLog", back_populates="subscription")
    observations = relationship("Observation", back_populates="subscription")


class AlertLog(Base):
    """Records every condition check so users can audit what triggered an alert."""

    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)
    checked_at = Column(DateTime, default=utcnow)
    # Raw factor values
    ovation_prob = Column(Float)
    kp_index = Column(Float)
    cloud_cover = Column(Float)
    aod = Column(Float)
    elevation_m = Column(Float)
    horizon_deg = Column(Float)
    bortle = Column(Float)
    moon_illumination = Column(Float)
    pwv_mm = Column(Float)
    # Final score and outcome
    visibility_score = Column(Float)
    alerted = Column(Boolean, default=False)

    subscription = relationship("Subscription", back_populates="alerts")
    observations = relationship("Observation", back_populates="alert_log")


class Observation(Base):
    """A ground-truth aurora sighting report used to calibrate the model.

    Each observation records whether the aurora was actually visible
    (``saw_aurora``) at a place and time, plus an optional intensity.  It is
    linked to the nearest ``AlertLog`` snapshot (``alert_log_id``) so the
    calibration fit can recover the factor vector that was present when the
    observation was made.  The confusion-matrix class (TP/FP/FN/TN) is *derived*
    at fit time from (``alert_log.alerted``, ``saw_aurora``) — it is not stored.

    ``subscription_id``/``alert_log_id`` are nullable so ad-hoc reports (a
    sighting at an arbitrary location, or a reply we couldn't link) are still
    captured; such rows just lack a factor vector until one is attached.
    """

    __tablename__ = "observations"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=utcnow)

    # What was observed.
    observed_at = Column(DateTime, nullable=False)
    saw_aurora = Column(Boolean, nullable=False)
    # Optional ordinal strength: 0 nothing, 1 camera-only/faint, 2 visible glow,
    # 3 structured/bright.  Enables a graded model later; None if unspecified.
    intensity = Column(Integer, nullable=True)
    # How the label arrived: "sms_reply", "report_api", or "manual".
    source = Column(String, nullable=False, default="report_api")
    note = Column(String, nullable=True)

    # Who/where.  phone and lat/lon are kept even when a subscription is linked,
    # so ad-hoc reports (no subscription) are still usable.
    phone = Column(String, nullable=True, index=True)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)

    # Links to the feature snapshot and originating subscription.
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    alert_log_id = Column(Integer, ForeignKey("alert_log.id"), nullable=True)

    subscription = relationship("Subscription", back_populates="observations")
    alert_log = relationship("AlertLog", back_populates="observations")


def init_db() -> None:
    """Create all tables if they don't already exist."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
