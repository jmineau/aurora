"""SQLAlchemy ORM models.

Tables:
  Subscription – one row per (channel, recipient, location).  ``channel`` names
                 the Notifier ("sms" or "inreach", see notify.py) and
                 ``recipient`` is its address (an E.164 phone number, or an
                 inReach conversation key).  Static atmospheric
                 factors (elevation, horizon, Bortle) are cached here after
                 the first check so they don't need to be re-fetched.
  AlertLog     – one row per check cycle per subscription, whether or not an
                 alert was sent.  This is the feature store for calibration:
                 every row carries the full factor vector + score + outcome.
  Observation  – a ground-truth label: did a user actually see the aurora at a
                 place and time?  Linked to the nearest AlertLog snapshot so it
                 inherits that check's factor vector.  See docs/roadmap.md and
                 AGENTS.md (Calibration).
  InReachConversation – one Garmin conversation (device ↔ aurora mailbox):
                 the stable ids scraped from Garmin's reply page plus the
                 newest per-message token/guid.  inReach subscriptions point
                 at it by ``conversation_key``; one with no active
                 subscription is a pending pairing (GET /pairings).

There is no migration framework: ``init_db`` runs small, idempotent SQLite
migrations in ``_migrate`` before ``create_all``.  Add a step there for any
change to an existing table (``create_all`` only creates missing tables).
"""

import datetime as dt
import logging

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    create_engine,
    inspect,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from sqlalchemy.schema import CreateTable

from aurora.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine)

log = logging.getLogger(__name__)


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
    # Delivery channel (a key of notify._REGISTRY) and that channel's address.
    channel = Column(String, nullable=False, default="sms", server_default="sms")
    recipient = Column(String, nullable=False, index=True)
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

    @property
    def phone(self) -> str | None:
        """The phone number for SMS subscriptions (kept for API compatibility)."""
        return self.recipient if self.channel == "sms" else None


class AlertLog(Base):
    """Records every condition check so users can audit what triggered an alert."""

    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, index=True)
    # Nullable: backfilled snapshots reconstruct conditions for an ad-hoc sighting
    # that has no subscription behind it.
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
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
    # True for snapshots reconstructed from reanalysis by the backfill importer
    # (space-weather factors ovation_prob/kp_index are NULL on these).
    backfilled = Column(Boolean, default=False)

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
    # How the label arrived: "sms_reply", "inreach_reply", "report_api", or "manual".
    source = Column(String, nullable=False, default="report_api")
    note = Column(String, nullable=True)

    # Who/where.  phone and lat/lon are kept even when a subscription is linked,
    # so ad-hoc reports (no subscription) are still usable.
    phone = Column(String, nullable=True, index=True)
    # Channel-neutral sender (phone number or inReach token); used for linking.
    recipient = Column(String, nullable=True, index=True)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)

    # Links to the feature snapshot and originating subscription.
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    alert_log_id = Column(Integer, ForeignKey("alert_log.id"), nullable=True)

    subscription = relationship("Subscription", back_populates="observations")
    alert_log = relationship("AlertLog", back_populates="observations")


class InReachConversation(Base):
    """A Garmin inReach / Messenger conversation with the aurora mailbox.

    Garmin's reply link token and the page's message ``Guid`` change with every
    message the device sends; ``ConversationId`` and the two Messenger account
    ids do not.  So the conversation is the stable identity: an inReach
    ``Subscription.recipient`` is this row's ``conversation_key``, and the
    mailbox poller refreshes ``latest_token``/``latest_message_guid`` on every
    message so alerts reply to the newest one.  ``resolved_at`` is when the ids
    were last read from Garmin's page (the send path re-reads after 24 h).
    """

    __tablename__ = "inreach_conversations"

    id = Column(Integer, primary_key=True)
    # ConversationId, or "legacy:<Guid>" for an old-style page without one.
    conversation_key = Column(String, nullable=False, unique=True, index=True)
    messenger = Column(Boolean, nullable=False, default=True)  # page's isGSR56C flag
    conversation_id = Column(String, nullable=True)
    messenger_account_id = Column(String, nullable=True)
    sender_messenger_account_id = Column(String, nullable=True)
    reply_address = Column(String, nullable=True)
    page_url = Column(String, nullable=True)  # the reply page; Origin/Referer for the POST
    latest_token = Column(String, nullable=False, index=True)
    latest_message_guid = Column(String, nullable=False)
    message_id = Column(String, nullable=False, default="0")
    resolved_at = Column(DateTime, nullable=True)
    # From the email: shown at GET /pairings.
    sender = Column(String, nullable=True)
    last_message = Column(String, nullable=True)
    first_seen_at = Column(DateTime, default=utcnow)
    last_seen_at = Column(DateTime, default=utcnow)


# ── Schema migrations (SQLite, idempotent) ────────────────────────────────────

def _columns(bind: Engine, table: str) -> set[str] | None:
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return {c["name"] for c in insp.get_columns(table)}


def _migrate_subscription_channel(bind: Engine) -> None:
    """subscriptions.phone → (channel="sms", recipient=phone).

    SQLite can't drop the old NOT NULL ``phone`` column in place, so the table
    is rebuilt (create new, copy, drop old, rename) with foreign keys off;
    ids are preserved so alert_log/observations links stay valid.
    """
    cols = _columns(bind, "subscriptions")
    if cols is None or "recipient" in cols:
        return
    table = Subscription.__table__
    new = table.to_metadata(MetaData(), name="subscriptions_new")
    keep = [c.name for c in table.columns if c.name in cols]
    with bind.connect() as conn:
        # PRAGMA foreign_keys is ignored inside a transaction: set it, then commit
        # SQLAlchemy's autobegun one.  (SQLite's default, and this app's, is OFF.)
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        with conn.begin():
            # Leftover from an interrupted earlier attempt (pysqlite runs DDL
            # outside the transaction, so a failed copy can leave it behind).
            conn.exec_driver_sql("DROP TABLE IF EXISTS subscriptions_new")
            conn.execute(CreateTable(new))
            conn.exec_driver_sql(
                f"INSERT INTO subscriptions_new (channel, recipient, {', '.join(keep)}) "
                f"SELECT 'sms', phone, {', '.join(keep)} FROM subscriptions"
            )
            conn.exec_driver_sql("DROP TABLE subscriptions")
            conn.exec_driver_sql("ALTER TABLE subscriptions_new RENAME TO subscriptions")
            for index in table.indexes:
                index.create(conn, checkfirst=True)
    log.info("Migrated subscriptions: phone -> channel='sms', recipient.")


def _migrate_observation_recipient(bind: Engine) -> None:
    """Add observations.recipient, filled from phone for existing rows."""
    cols = _columns(bind, "observations")
    if cols is None or "recipient" in cols:
        return
    with bind.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE observations ADD COLUMN recipient VARCHAR")
        conn.exec_driver_sql("UPDATE observations SET recipient = phone WHERE recipient IS NULL")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_observations_recipient ON observations (recipient)"
        )
    log.info("Migrated observations: added recipient.")


def _migrate(bind: Engine) -> None:
    _migrate_subscription_channel(bind)
    _migrate_observation_recipient(bind)


def init_db(bind: Engine | None = None) -> None:
    """Migrate existing tables, then create any that don't exist yet."""
    bind = bind or engine
    _migrate(bind)
    Base.metadata.create_all(bind=bind)


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
