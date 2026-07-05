"""SQLAlchemy ORM models.

Two tables:
  Subscription – one row per (phone, location) pair.  Static atmospheric
                 factors (elevation, horizon, Bortle) are cached here after
                 the first check so they don't need to be re-fetched.
  AlertLog     – one row per check cycle per subscription, whether or not an
                 alert was sent.  Useful for debugging and threshold tuning.
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
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    last_alerted_at = Column(DateTime, nullable=True)

    # Static atmospheric factors cached after the first check.
    elevation_m = Column(Float, nullable=True)
    horizon_deg = Column(Float, nullable=True)
    bortle = Column(Float, nullable=True)

    alerts = relationship("AlertLog", back_populates="subscription")


class AlertLog(Base):
    """Records every condition check so users can audit what triggered an alert."""

    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)
    checked_at = Column(DateTime, default=dt.datetime.utcnow)
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
