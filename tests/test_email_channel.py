"""Email channel: address validation, SMTP send (stub server, no network),
collect-only mode, settings validation, and Y/N replies read from the mailbox.
"""

import datetime as dt
import smtplib
from email.message import EmailMessage

import pytest

from aurora import email_channel, inbox, notify
from aurora.config import Settings
from aurora.db import AlertLog, Observation, Subscription, utcnow
from aurora.notify_base import NotifierError

ADDR = "friend@example.com"


# ── Address handling ──────────────────────────────────────────────────────────

class TestNormalize:
    def test_lowercases_and_strips(self):
        assert notify.normalize_recipient("email", "  Friend@Example.COM ") == ADDR

    @pytest.mark.parametrize(
        "raw", ["", "friend", "friend@", "@example.com", "a@b", "a b@example.com",
                "a@example.com\nBcc: x@y.com", "Name <a@example.com>", "a@@example.com"],
    )
    def test_rejects(self, raw):
        with pytest.raises(ValueError, match="plain address"):
            notify.normalize_recipient("email", raw)

    def test_mask_keeps_first_letter_and_domain(self):
        assert notify.mask_recipient(ADDR) == "f...@example.com"


# ── Sending ───────────────────────────────────────────────────────────────────

class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_login = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.logged_in = None
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr(email_channel.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_channel.smtplib, "SMTP_SSL", FakeSMTPSSL)
    cfg = Settings(smtp_host="smtp.example.com", smtp_user="aurora@example.com", smtp_password="pw")
    monkeypatch.setattr(email_channel, "settings", cfg)
    return cfg


class TestSend:
    def test_sends_alert_with_reply_to(self, smtp):
        mid = notify.get_notifier("email").send(ADDR, "Aurora alert", "Aurora alert, N Utah: 72% chance.")
        conn = FakeSMTP.instances[0]
        assert (conn.host, conn.port) == ("smtp.example.com", 587)
        assert conn.started_tls and conn.logged_in == ("aurora@example.com", "pw")
        (msg,) = conn.sent
        assert msg["To"] == ADDR and msg["From"] == "aurora@example.com"
        assert msg["Reply-To"] == "aurora@example.com" and msg["Subject"] == "Aurora alert"
        assert msg.get_content().strip() == "Aurora alert, N Utah: 72% chance."
        assert mid == msg["Message-ID"] and mid.endswith("@example.com>")

    def test_smtp_from_overrides_user(self, smtp, monkeypatch):
        monkeypatch.setattr(email_channel, "settings", smtp.model_copy(update={"smtp_from": "alerts@example.com"}))
        notify.get_notifier("email").send(ADDR, "s", "b")
        assert FakeSMTP.instances[0].sent[0]["From"] == "alerts@example.com"

    def test_port_465_uses_implicit_tls(self, smtp, monkeypatch):
        monkeypatch.setattr(email_channel, "settings", smtp.model_copy(update={"smtp_port": 465}))
        notify.get_notifier("email").send(ADDR, "s", "b")
        conn = FakeSMTP.instances[0]
        assert isinstance(conn, FakeSMTPSSL) and not conn.started_tls

    def test_smtp_failure_is_a_notifier_error(self, smtp):
        FakeSMTP.fail_login = True
        with pytest.raises(NotifierError, match="SMTP send failed"):
            notify.get_notifier("email").send(ADDR, "s", "b")

    def test_deliver_reports_failure_without_raising(self, smtp):
        FakeSMTP.fail_login = True
        result = notify.deliver("email", ADDR, "s", "b")
        assert not result.ok and "SMTP send failed" in result.error

    def test_collect_only_logs_and_returns_none(self, monkeypatch, caplog):
        monkeypatch.setattr(email_channel, "settings", Settings())
        monkeypatch.setattr(email_channel.smtplib, "SMTP", lambda *a, **k: pytest.fail("must not connect"))
        with caplog.at_level("INFO"):
            assert notify.get_notifier("email").send(ADDR, "s", "the body") is None
        assert "Would have sent" in caplog.text and "the body" in caplog.text
        assert notify.deliver("email", ADDR, "s", "b").ok


class TestSettings:
    def test_all_or_none(self):
        assert not Settings().email_enabled
        assert Settings(smtp_host="h", smtp_user="u", smtp_password="p").email_enabled
        with pytest.raises(ValueError, match="SMTP_HOST, SMTP_USER and SMTP_PASSWORD"):
            Settings(smtp_host="h")


# ── Replies ───────────────────────────────────────────────────────────────────

class FakeImap:
    """Filters SEARCH ... FROM "<addr>" so only that sender's unseen mail matches."""

    def __init__(self, messages: dict[bytes, EmailMessage]):
        self.messages = {uid: m.as_bytes() for uid, m in messages.items()}
        self.senders = {uid: m["From"] for uid, m in messages.items()}
        self.seen: set[bytes] = set()

    def select(self, folder):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            if "garmin.com" in " ".join(str(a) for a in args):
                return "OK", [b""]
            wanted = args[-1].strip('"')
            uids = [u for u, frm in self.senders.items() if wanted in frm and u not in self.seen]
            return "OK", [b" ".join(uids)]
        if command == "FETCH":
            return "OK", [(b"1 (UID " + args[0] + b" BODY[] {n}", self.messages[args[0]]), b")"]
        if command == "STORE":
            self.seen.add(args[0])
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        pass


def reply(text: str, sender: str = ADDR, date: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"Friend <{sender}>"
    msg["To"] = "aurora@example.com"
    msg["Subject"] = "Re: Aurora alert"
    if date:
        msg["Date"] = date
    msg.set_content(f"{text}\n\nOn Sat, Aurora wrote:\n> Aurora alert, N Utah: 72% chance.")
    return msg


@pytest.fixture
def poll(memory_db, monkeypatch):
    monkeypatch.setattr(memory_db, "close", lambda: None)

    def run(imap):
        return inbox.poll_once(connect=lambda: imap, session_factory=lambda: memory_db)

    return run


def _email_sub(db, address=ADDR, active=True):
    sub = Subscription(channel="email", recipient=address, address="N Utah", lat=41.7, lon=-111.8, active=active)
    db.add(sub)
    db.commit()
    snap = AlertLog(subscription_id=sub.id, checked_at=utcnow() - dt.timedelta(minutes=30),
                    visibility_score=70.0, alerted=True)
    db.add(snap)
    db.commit()
    return sub, snap


class TestEmailReplies:
    def test_yes_reply_records_linked_observation(self, memory_db, poll):
        sub, snap = _email_sub(memory_db)
        imap = FakeImap({b"3": reply("Y - saw green arcs")})
        assert [r.action for r in poll(imap)] == ["observation"]
        obs = memory_db.query(Observation).one()
        assert obs.source == "email_reply" and obs.saw_aurora is True
        assert obs.recipient == ADDR and obs.phone is None
        assert obs.alert_log_id == snap.id and obs.subscription_id == sub.id
        assert imap.seen == {b"3"}

    def test_no_reply(self, memory_db, poll):
        _email_sub(memory_db)
        poll(FakeImap({b"3": reply("no, clouded out")}))
        assert memory_db.query(Observation).one().saw_aurora is False

    def test_reply_date_is_observed_at(self, memory_db, poll):
        _email_sub(memory_db)
        poll(FakeImap({b"3": reply("Y", date="Sat, 07 Nov 2026 03:10:00 +0000")}))
        assert memory_db.query(Observation).one().observed_at == dt.datetime(2026, 11, 7, 3, 10)

    def test_not_a_yes_no_is_marked_seen_without_a_label(self, memory_db, poll):
        _email_sub(memory_db)
        imap = FakeImap({b"3": reply("thanks, will look tonight")})
        assert [r.action for r in poll(imap)] == ["message"]
        assert memory_db.query(Observation).count() == 0 and imap.seen == {b"3"}

    def test_other_senders_are_left_alone(self, memory_db, poll):
        _email_sub(memory_db)
        imap = FakeImap({b"3": reply("Y", sender="stranger@example.org")})
        assert poll(imap) == []
        assert memory_db.query(Observation).count() == 0 and imap.seen == set()

    def test_lookalike_sender_is_not_trusted(self, memory_db, poll):
        # IMAP FROM matches substrings: notfriend@example.com contains friend@example.com.
        _email_sub(memory_db)
        imap = FakeImap({b"3": reply("Y", sender="notfriend@example.com")})
        assert poll(imap) == []
        assert memory_db.query(Observation).count() == 0 and imap.seen == set()

    def test_inactive_subscription_is_not_polled(self, memory_db, poll):
        _email_sub(memory_db, active=False)
        assert poll(FakeImap({b"3": reply("Y")})) == []
        assert memory_db.query(Observation).count() == 0
