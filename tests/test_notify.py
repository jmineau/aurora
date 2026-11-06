"""Notifier interface: registry dispatch, SMS recipient validation, deliver()
failure reporting, and the check-loop call site.  InReachNotifier's HTTP flow is
in test_inreach.py.
"""

import httpx
import pytest

from aurora import notify
from aurora.config import Settings

TOKEN = "Zx9_kQ2-ab7LmN4pR8sT1uV"  # synthetic reply-link token


# ── Registry / dispatch ───────────────────────────────────────────────────────

class TestRegistry:
    def test_channels(self):
        assert notify.CHANNELS == ("sms", "inreach")
        assert notify.DEFAULT_CHANNEL == "sms"

    def test_get_notifier_dispatches_by_channel(self):
        assert isinstance(notify.get_notifier("sms"), notify.TwilioSmsNotifier)
        assert isinstance(notify.get_notifier("inreach"), notify.InReachNotifier)
        assert notify.get_notifier("sms") is notify.get_notifier("sms")

    def test_unknown_channel(self):
        with pytest.raises(ValueError, match="Unknown channel"):
            notify.get_notifier("pigeon")

    def test_enabled_channels_collect_only_by_default(self):
        # conftest stubs Twilio creds but sets no INREACH_REPLY_ADDRESS.
        assert notify.enabled_channels() == {"sms": True, "inreach": False}

    def test_sms_send_wraps_send_sms(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notify.sms, "send_sms", lambda to, msg: sent.append((to, msg)) or "SM1")
        assert notify.get_notifier("sms").send("+15551234567", "subj", "body") == "SM1"
        assert sent == [("+15551234567", "body")]


class TestSmsRecipient:
    @pytest.mark.parametrize("raw", ["+1 555-123-4567", "+447911123456"])
    def test_e164(self, raw):
        assert notify.normalize_recipient("sms", raw).startswith("+")

    @pytest.mark.parametrize("raw", ["5551234567", "+0123", "hello"])
    def test_bad_phone(self, raw):
        with pytest.raises(ValueError, match="E.164"):
            notify.normalize_recipient("sms", raw)


# ── deliver(): failure reporting + ntfy fallback ──────────────────────────────

class _Boom:
    channel = "boom"
    enabled = True

    def send(self, recipient, subject, body):
        raise notify.NotifierError("Garmin returned HTTP 500")


class TestDeliver:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(notify.sms, "send_sms", lambda to, msg: "SM9")
        d = notify.deliver("sms", "+15551234567", "s", "b")
        assert d == notify.Delivery(ok=True, message_id="SM9")

    def test_failure_logs_error_and_posts_ntfy(self, monkeypatch, caplog):
        monkeypatch.setitem(notify._instances, "inreach", _Boom())
        monkeypatch.setattr(
            notify, "settings", Settings(_env_file=None, ntfy_url="https://ntfy.test/secret")
        )
        posts = []
        monkeypatch.setattr(
            notify.httpx, "post", lambda url, content, timeout: posts.append((url, content))
        )
        d = notify.deliver("inreach", TOKEN, "s", "b")
        assert d.ok is False and "HTTP 500" in d.error
        assert any(r.levelname == "ERROR" for r in caplog.records)
        assert len(posts) == 1
        url, content = posts[0]
        assert url == "https://ntfy.test/secret"
        line = content.decode()
        assert "\n" not in line and "inreach" in line
        assert TOKEN not in line and TOKEN[-4:] in line  # TOKEN masked

    def test_failure_without_ntfy_posts_nothing(self, monkeypatch):
        monkeypatch.setitem(notify._instances, "inreach", _Boom())
        monkeypatch.setattr(notify, "settings", Settings(_env_file=None))
        monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: pytest.fail("no ntfy"))
        assert notify.deliver("inreach", TOKEN, "s", "b").ok is False

    def test_ntfy_error_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(notify, "settings", Settings(_env_file=None, ntfy_url="https://ntfy.test/x"))

        def _raise(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(notify.httpx, "post", _raise)
        notify.notify_failure("x")  # does not raise


# ── The check-loop call site ──────────────────────────────────────────────────

class _Recorder:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, recipient, subject, body):
        self.sent.append((recipient, subject, body))
        return "id-1"


async def test_send_alert_goes_through_channel_notifier(monkeypatch):
    import aurora.main as main
    from aurora.db import Subscription

    rec = _Recorder()
    monkeypatch.setitem(notify._instances, "inreach", rec)
    sub = Subscription(channel="inreach", recipient=TOKEN, address="Algonquin Park", lat=45.8, lon=-78.4)
    d = {
        "lat": 45.8, "visibility_score": 72.0, "calibrated": True, "kp_index": 6.3,
        "aurora_elevation_deg": 15.0, "cloud_cover_pct": 5.0, "moon_altitude_deg": -3.0,
        "moon_illumination": 0.4, "checked_at": "2026-11-06T06:10:00+00:00",
    }
    delivery = await main._send_alert(sub, d)
    assert delivery.ok and delivery.message_id == "id-1"
    (recipient, subject, body), = rec.sent
    assert recipient == TOKEN and subject == "Aurora alert"
    # Nov 6 2026 is after the DST change: Algonquin is on EST.
    assert body.startswith("Aurora alert, Algonquin Park: 72% chance. Kp 6.3")
    assert "01:10 EST" in body and body.endswith("Reply Y or N")
    assert len(body) <= 160 and body.isascii()


async def test_sms_gets_the_same_compact_body(monkeypatch):
    import aurora.main as main
    from aurora.db import Subscription

    rec = _Recorder()
    monkeypatch.setitem(notify._instances, "sms", rec)
    sub = Subscription(channel="sms", recipient="+15551234567", address="N Utah", lat=41.7, lon=-111.8)
    d = {"visibility_score": 50.0, "calibrated": False, "kp_index": 5.0, "cloud_cover_pct": 10.0,
         "checked_at": "2026-11-06T08:10:00+00:00"}
    await main._send_alert(sub, d)
    (_, _, body), = rec.sent
    assert body == "Aurora alert, N Utah: score 50/100. Kp 5.0, cloud 10%. 01:10 MST. Saw it? Reply Y or N"


async def test_failed_send_still_logs_snapshot_and_starts_cooldown(memory_db, monkeypatch):
    """A failed inReach send must not be retried every cycle (each costs money)."""
    import aurora.main as main
    from aurora.db import AlertLog, Subscription

    sub = Subscription(channel="inreach", recipient=TOKEN, address="x", lat=45.8, lon=-78.4,
                       threshold=10.0, elevation_m=1, horizon_deg=1, bortle=2)
    memory_db.add(sub)
    memory_db.commit()

    class _Result:
        def __init__(self):
            from types import SimpleNamespace as NS
            self.terrain = NS(elevation_m=1.0, horizon_deg=1.0)
            self.light_pollution = NS(bortle=2.0)
            self.score = NS(visibility_score=90.0)
            self.ovation = NS(visible_probability=50.0, probability=40.0)
            self.kp = NS(kp_index=6.0)
            self.weather = NS(cloud_cover=0.0, pwv_mm=5.0)
            self.aod = NS(aod=0.1)
            self.moon = NS(illumination=0.1)

        def to_dict(self):
            return {}

    async def _check(*a, **k):
        return _Result()

    attempts = []

    async def _fail(s, d):
        attempts.append(s.recipient)
        return notify.Delivery(ok=False, error="boom")

    monkeypatch.setattr(main.checker, "check", _check)
    monkeypatch.setattr(main, "_send_alert", _fail)

    def _db():
        yield memory_db

    monkeypatch.setattr(main, "get_db", _db)
    monkeypatch.setattr(memory_db, "close", lambda: None)

    await main.check_all_subscriptions()
    await main.check_all_subscriptions()  # second cycle: on cooldown, no new attempt

    assert attempts == [TOKEN]
    memory_db.refresh(sub)
    assert sub.last_alerted_at is not None
    logs = memory_db.query(AlertLog).all()
    assert [r.alerted for r in logs] == [True, False]
