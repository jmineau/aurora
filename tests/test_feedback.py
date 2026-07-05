"""Tests for calibration ground-truth capture (aurora.feedback + db.Observation).

All offline: an in-memory SQLite DB is built per test and the linking logic and
SMS reply parser are exercised directly.  The /report endpoint is covered with a
FastAPI TestClient against the same in-memory DB (no network: "lat,lon" locations
skip geocoding).
"""

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aurora.db import AlertLog, Base, Observation, Subscription
from aurora.feedback import parse_reply, record_observation


@pytest.fixture()
def db():
    """A fresh in-memory database session with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _sub(db, phone="+15551234567") -> Subscription:
    sub = Subscription(phone=phone, address="Fairbanks, AK", lat=64.8, lon=-147.7, threshold=30.0)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _snapshot(db, sub, *, checked_at, alerted, score=50.0) -> AlertLog:
    row = AlertLog(
        subscription_id=sub.id,
        checked_at=checked_at,
        ovation_prob=40.0,
        kp_index=5.0,
        cloud_cover=10.0,
        aod=0.1,
        elevation_m=136.0,
        horizon_deg=2.0,
        bortle=4.0,
        moon_illumination=0.2,
        pwv_mm=8.0,
        visibility_score=score,
        alerted=alerted,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── Reply parser ──────────────────────────────────────────────────────────────

class TestParseReply:
    @pytest.mark.parametrize("body", ["Y", "yes", "Yeah!", "  SAW it ", "seen", "1"])
    def test_affirmative(self, body):
        assert parse_reply(body) is True

    @pytest.mark.parametrize("body", ["N", "no", "Nope.", "nothing", "clouds", "0"])
    def test_negative(self, body):
        assert parse_reply(body) is False

    @pytest.mark.parametrize("body", ["", "   ", "maybe", "what?", None])
    def test_unclassifiable(self, body):
        assert parse_reply(body) is None


# ── Linking strategies ────────────────────────────────────────────────────────

class TestLinking:
    def test_reply_links_to_latest_alerted_snapshot(self, db):
        sub = _sub(db)
        now = dt.datetime.utcnow()
        # An older alerted snapshot and a newer non-alerted one.
        old_alert = _snapshot(db, sub, checked_at=now - dt.timedelta(hours=2), alerted=True)
        _snapshot(db, sub, checked_at=now - dt.timedelta(minutes=10), alerted=False)

        obs = record_observation(db, saw_aurora=True, phone=sub.phone, link="reply")

        # Should attach to the alert that was actually sent, not the latest check.
        assert obs.alert_log_id == old_alert.id
        assert obs.subscription_id == sub.id
        assert obs.saw_aurora is True

    def test_nearest_links_to_closest_in_time(self, db):
        sub = _sub(db)
        base = dt.datetime(2026, 1, 15, 6, 0)
        far = _snapshot(db, sub, checked_at=base - dt.timedelta(hours=2), alerted=False)
        near = _snapshot(db, sub, checked_at=base + dt.timedelta(minutes=20), alerted=False)

        obs = record_observation(
            db, saw_aurora=True, phone=sub.phone, observed_at=base, link="nearest"
        )
        assert obs.alert_log_id == near.id
        assert obs.alert_log_id != far.id

    def test_nearest_outside_window_stays_unlinked(self, db):
        sub = _sub(db)
        base = dt.datetime(2026, 1, 15, 6, 0)
        _snapshot(db, sub, checked_at=base - dt.timedelta(hours=10), alerted=False)

        obs = record_observation(
            db, saw_aurora=False, phone=sub.phone, observed_at=base, link="nearest"
        )
        assert obs.alert_log_id is None

    def test_reply_ignores_alerts_older_than_window(self, db):
        sub = _sub(db)
        now = dt.datetime.utcnow()
        _snapshot(db, sub, checked_at=now - dt.timedelta(days=3), alerted=True)

        obs = record_observation(db, saw_aurora=True, phone=sub.phone, link="reply")
        assert obs.alert_log_id is None

    def test_adhoc_report_without_subscription_is_stored(self, db):
        obs = record_observation(
            db, saw_aurora=True, lat=61.2, lon=-149.9, link="nearest", source="report_api"
        )
        assert obs.id is not None
        assert obs.alert_log_id is None
        assert obs.lat == 61.2

    def test_aware_datetime_is_normalised_to_naive(self, db):
        sub = _sub(db)
        aware = dt.datetime(2026, 1, 15, 6, 0, tzinfo=dt.timezone.utc)
        obs = record_observation(db, saw_aurora=True, phone=sub.phone, observed_at=aware)
        assert obs.observed_at.tzinfo is None


# ── /report endpoint ──────────────────────────────────────────────────────────

class TestReportEndpoint:
    def test_report_creates_observation(self, db, monkeypatch):
        from fastapi.testclient import TestClient

        import aurora.main as main

        # Point the app's DB dependency at the in-memory session and disable the
        # scheduler/geocode-cache side effects of the lifespan.
        monkeypatch.setattr(main, "init_db", lambda: None)
        monkeypatch.setattr(main, "init_cache", lambda: None)
        monkeypatch.setattr(main.scheduler, "start", lambda: None)
        monkeypatch.setattr(main.scheduler, "shutdown", lambda **_: None)

        def _override_db():
            yield db

        main.app.dependency_overrides[main.get_db] = _override_db

        try:
            with TestClient(main.app) as client:
                resp = client.post(
                    "/report",
                    json={"saw_aurora": True, "location": "64.8,-147.7", "intensity": 2},
                )
            assert resp.status_code == 201
            body = resp.json()
            assert body["saw_aurora"] is True
            assert body["intensity"] == 2
            assert body["lat"] == pytest.approx(64.8)
            assert db.query(Observation).count() == 1
        finally:
            main.app.dependency_overrides.clear()
