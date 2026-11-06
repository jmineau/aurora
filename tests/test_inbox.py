"""Mailbox processing: inReach Y/N replies → observations, other messages →
pending pairings, with a stubbed IMAP server (no network).
"""

import datetime as dt
from email.message import EmailMessage

import pytest

from aurora import inbox
from aurora.db import AlertLog, Observation, PendingPairing, Subscription, utcnow

GUID = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
OTHER = "ffffffff-0000-1111-2222-333333333333"


def garmin_mail(text: str, guid: str = GUID, *, html: bool = False, date: str | None = None) -> EmailMessage:
    """An email shaped like the ones an inReach sends."""
    link = f"https://inreach.garmin.com/textmessage/txtmsg?extId={guid}&adr=aurora%40example.com"
    msg = EmailMessage()
    msg["From"] = "no.reply.inreach@garmin.com"
    msg["Subject"] = "inReach message from James"
    if date:
        msg["Date"] = date
    body = (
        f"{text}\n\nView the location or send a reply to James:\n{link}\n\n"
        "James sent this message from: Lat 45.8 Lon -78.4\n\nDo not reply directly to this message."
    )
    if html:
        msg.set_content(
            f"<html><body><p>{text}</p><p><a href=\"{link.replace('&', '&amp;')}\">reply</a></p></body></html>",
            subtype="html",
        )
    else:
        msg.set_content(body)
    return msg


def _sub(db, guid=GUID, active=True):
    sub = Subscription(channel="inreach", recipient=guid, address="Algonquin", lat=45.8, lon=-78.4,
                       active=active)
    db.add(sub)
    db.commit()
    return sub


def _alert(db, sub, minutes_ago=30):
    row = AlertLog(subscription_id=sub.id, checked_at=utcnow() - dt.timedelta(minutes=minutes_ago),
                   visibility_score=70.0, alerted=True)
    db.add(row)
    db.commit()
    return row


class TestMessageText:
    def test_plain(self):
        assert inbox.message_text(garmin_mail("Y saw it")).startswith("Y saw it")

    def test_html_keeps_link(self):
        text = inbox.message_text(garmin_mail("N", html=True))
        assert text.startswith("N")
        assert f"extId={GUID}" in text


class TestProcessMessage:
    def test_yes_reply_records_linked_observation(self, memory_db):
        sub = _sub(memory_db)
        snap = _alert(memory_db, sub)
        res = inbox.process_message(memory_db, garmin_mail("Yes! green arcs"))
        assert res == inbox.InboxResult("observation", GUID, True)
        obs = memory_db.query(Observation).one()
        assert obs.source == "inreach_reply"
        assert obs.saw_aurora is True
        assert obs.recipient == GUID and obs.phone is None
        assert obs.alert_log_id == snap.id and obs.subscription_id == sub.id
        assert memory_db.query(PendingPairing).count() == 0

    def test_no_reply_uses_email_date(self, memory_db):
        _alert(memory_db, _sub(memory_db))
        inbox.process_message(memory_db, garmin_mail("n", date="Fri, 06 Nov 2026 03:00:00 -0500"))
        obs = memory_db.query(Observation).one()
        assert obs.saw_aurora is False
        assert obs.observed_at == dt.datetime(2026, 11, 6, 8, 0)

    def test_html_reply(self, memory_db):
        _alert(memory_db, _sub(memory_db))
        assert inbox.process_message(memory_db, garmin_mail("Y", html=True)).action == "observation"

    def test_unknown_guid_becomes_pending_pairing(self, memory_db, caplog):
        caplog.set_level("INFO")
        res = inbox.process_message(memory_db, garmin_mail("Hi from the canoe", OTHER))
        assert res == inbox.InboxResult("pairing", OTHER)
        p = memory_db.query(PendingPairing).one()
        assert p.guid == OTHER and p.last_message == "Hi from the canoe"
        assert p.sender == "inReach message from James"
        assert OTHER in caplog.text
        assert memory_db.query(Observation).count() == 0

    def test_y_from_unpaired_device_is_a_pairing_not_a_label(self, memory_db):
        res = inbox.process_message(memory_db, garmin_mail("Yes pair me", OTHER))
        assert res.action == "pairing"
        assert memory_db.query(Observation).count() == 0
        assert memory_db.query(PendingPairing).count() == 1

    def test_repeat_pairing_updates_one_row(self, memory_db):
        inbox.process_message(memory_db, garmin_mail("first", OTHER))
        inbox.process_message(memory_db, garmin_mail("second", OTHER))
        p = memory_db.query(PendingPairing).one()
        assert p.last_message == "second"

    def test_chatter_from_paired_device_stores_nothing(self, memory_db):
        _sub(memory_db)
        res = inbox.process_message(memory_db, garmin_mail("camp at km 12"))
        assert res.action == "pairing"
        assert memory_db.query(PendingPairing).count() == 0
        assert memory_db.query(Observation).count() == 0

    def test_inactive_subscription_can_re_pair(self, memory_db):
        _sub(memory_db, active=False)
        inbox.process_message(memory_db, garmin_mail("back again"))
        assert memory_db.query(PendingPairing).one().guid == GUID

    def test_no_link_is_ignored(self, memory_db):
        msg = EmailMessage()
        msg["Subject"] = "Your Garmin invoice"
        msg.set_content("Thanks for your purchase")
        assert inbox.process_message(memory_db, msg) == inbox.InboxResult("ignored")


# ── IMAP polling with a stub server ───────────────────────────────────────────

class FakeImap:
    def __init__(self, messages: dict[bytes, EmailMessage], fail_select=False):
        self.messages = {uid: m.as_bytes() for uid, m in messages.items()}
        self.seen: set[bytes] = set()
        self.fail_select = fail_select
        self.searches = []
        self.logged_out = False

    def select(self, folder):
        self.folder = folder
        return ("NO", [b"no such folder"]) if self.fail_select else ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args)
            uids = [u for u in self.messages if u not in self.seen]
            return "OK", [b" ".join(uids)]
        if command == "FETCH":
            uid, spec = args
            assert "PEEK" in spec  # fetching must not mark mail seen by itself
            return "OK", [(b"1 (UID " + uid + b" BODY[] {n}", self.messages[uid]), b")"]
        if command == "STORE":
            uid, op, flags = args
            assert op == "+FLAGS" and "Seen" in flags
            self.seen.add(uid)
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


class TestPollOnce:
    def test_processes_and_marks_seen(self, memory_db, monkeypatch):
        _alert(memory_db, _sub(memory_db))
        monkeypatch.setattr(memory_db, "close", lambda: None)
        imap = FakeImap({b"7": garmin_mail("Y"), b"8": garmin_mail("hello", OTHER)})
        results = inbox.poll_once(connect=lambda: imap, session_factory=lambda: memory_db)
        assert [r.action for r in results] == ["observation", "pairing"]
        assert imap.seen == {b"7", b"8"}
        assert imap.logged_out
        assert imap.searches[0] == (None, "UNSEEN", "FROM", '"garmin.com"')
        # Second poll finds nothing new.
        assert inbox.poll_once(connect=lambda: imap, session_factory=lambda: memory_db) == []
        assert memory_db.query(Observation).count() == 1

    def test_processing_error_leaves_mail_unseen(self, memory_db, monkeypatch):
        monkeypatch.setattr(memory_db, "close", lambda: None)

        def _boom(db, msg):
            raise RuntimeError("db locked")

        monkeypatch.setattr(inbox, "process_message", _boom)
        imap = FakeImap({b"1": garmin_mail("Y")})
        assert inbox.poll_once(connect=lambda: imap, session_factory=lambda: memory_db) == []
        assert imap.seen == set()

    def test_bad_folder_raises_and_logs_out(self, memory_db):
        imap = FakeImap({}, fail_select=True)
        with pytest.raises(inbox.imaplib.IMAP4.error):
            inbox.poll_once(connect=lambda: imap, session_factory=lambda: memory_db)
        assert imap.logged_out

    def test_poll_job_swallows_mailbox_errors(self, monkeypatch, caplog):
        def _down():
            raise OSError("network unreachable")

        monkeypatch.setattr(inbox, "poll_once", lambda: _down())
        inbox.poll_job()
        assert "IMAP poll failed" in caplog.text
