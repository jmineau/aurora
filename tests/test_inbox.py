"""Mailbox processing: each Garmin email's token is resolved (fake resolver, no
network) to its conversation; Y/N replies become observations, the newest
token/guid is stored, unknown conversations are pending pairings.  IMAP is a
stub server.
"""

import datetime as dt
from email.message import EmailMessage

import httpx
import pytest

from aurora import inbox, inreach
from aurora.db import AlertLog, InReachConversation, Observation, Subscription, utcnow
from aurora.notify_base import NotifierError

from .garmin_fake import (
    BASE, CONV, GUID1, GUID2, OTHER_CONV, OTHER_GUID, OTHER_TOKEN, TOKEN1, TOKEN2, message_page,
)

KEY = CONV["ConversationId"]
OTHER_KEY = OTHER_CONV["ConversationId"]


class FakeResolver:
    """token → ReplyPage, as Garmin's page would answer; unknown → expired."""

    def __init__(self):
        self.pages = {
            TOKEN1: message_page(GUID1),
            TOKEN2: message_page(GUID2),
            OTHER_TOKEN: message_page(OTHER_GUID, OTHER_CONV),
        }
        self.calls = []
        self.error: Exception | None = None

    def __call__(self, token):
        self.calls.append(token)
        if self.error:
            raise self.error
        if token not in self.pages:
            raise NotifierError("Garmin reply page format changed (or token expired): missing Guid")
        return inreach.parse_reply_page(self.pages[token], f"{BASE}/textmessage/txtmsg?extId={token}")


@pytest.fixture
def resolver():
    return FakeResolver()


def garmin_mail(text: str, token: str = TOKEN1, *, html: bool = False, date: str | None = None) -> EmailMessage:
    """An email shaped like the ones an inReach / Garmin Messenger sends."""
    msg = EmailMessage()
    msg["From"] = "no.reply.inreach@garmin.com"
    msg["Subject"] = "inReach message from James"
    if date:
        msg["Date"] = date
    if html:
        msg.set_content(
            f"<html><body><p>{text}</p><p><a href=\"https://messenger.garmin.com/r?extId={token}&amp;a=1\">"
            "reply</a></p></body></html>",
            subtype="html",
        )
    else:
        msg.set_content(
            f"{text}\n\nView the location or send a reply to James:\nhttps://inreachlink.com/{token}\n\n"
            "James sent this message from: Lat 45.8 Lon -78.4\n\nDo not reply directly to this message."
        )
    return msg


def _sub(db, key=KEY, active=True):
    sub = Subscription(channel="inreach", recipient=key, address="Algonquin", lat=45.8, lon=-78.4,
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


def _conv(db, key=KEY):
    db.expire_all()
    return db.query(InReachConversation).filter_by(conversation_key=key).one()


class TestMessageText:
    def test_plain(self):
        assert inbox.message_text(garmin_mail("Y saw it")).startswith("Y saw it")

    def test_html_keeps_link(self):
        text = inbox.message_text(garmin_mail("N", html=True))
        assert text.startswith("N")
        assert f"extId={TOKEN1}" in text


class TestProcessMessage:
    def test_yes_reply_records_linked_observation(self, memory_db, resolver):
        sub = _sub(memory_db)
        snap = _alert(memory_db, sub)
        res = inbox.process_message(memory_db, garmin_mail("Yes! green arcs"), resolver)
        assert res == inbox.InboxResult("observation", KEY, True)
        obs = memory_db.query(Observation).one()
        assert obs.source == "inreach_reply" and obs.saw_aurora is True
        assert obs.recipient == KEY and obs.phone is None
        assert obs.alert_log_id == snap.id and obs.subscription_id == sub.id

    def test_reply_with_a_new_token_matches_by_conversation(self, memory_db, resolver):
        # Tokens change per message: the second message has TOKEN2 but the same ConversationId.
        sub = _sub(memory_db)
        _alert(memory_db, sub)
        inbox.process_message(memory_db, garmin_mail("hello", TOKEN1), resolver)
        res = inbox.process_message(memory_db, garmin_mail("N", TOKEN2), resolver)
        assert res == inbox.InboxResult("observation", KEY, False)
        conv = _conv(memory_db)
        assert conv.latest_token == TOKEN2 and conv.latest_message_guid == GUID2

    def test_every_message_updates_newest_token(self, memory_db, resolver):
        _sub(memory_db)
        assert inbox.process_message(memory_db, garmin_mail("camp at km 12", TOKEN2), resolver).action == "message"
        conv = _conv(memory_db)
        assert conv.latest_token == TOKEN2 and conv.latest_message_guid == GUID2
        assert conv.last_message == "camp at km 12"
        assert memory_db.query(Observation).count() == 0

    def test_email_date_is_observed_at(self, memory_db, resolver):
        _alert(memory_db, _sub(memory_db))
        inbox.process_message(memory_db, garmin_mail("n", date="Fri, 06 Nov 2026 03:00:00 -0500"), resolver)
        assert memory_db.query(Observation).one().observed_at == dt.datetime(2026, 11, 6, 8, 0)

    def test_html_reply(self, memory_db, resolver):
        _alert(memory_db, _sub(memory_db))
        assert inbox.process_message(memory_db, garmin_mail("Y", html=True), resolver).action == "observation"

    def test_unknown_conversation_becomes_pairing(self, memory_db, resolver, caplog):
        caplog.set_level("INFO")
        res = inbox.process_message(memory_db, garmin_mail("Hi from the canoe", OTHER_TOKEN), resolver)
        assert res == inbox.InboxResult("pairing", OTHER_KEY)
        conv = _conv(memory_db, OTHER_KEY)
        assert conv.latest_token == OTHER_TOKEN and conv.last_message == "Hi from the canoe"
        assert conv.sender == "inReach message from James"
        assert OTHER_TOKEN in caplog.text and OTHER_KEY in caplog.text
        assert memory_db.query(Observation).count() == 0

    def test_y_from_unpaired_conversation_is_a_pairing_not_a_label(self, memory_db, resolver):
        assert inbox.process_message(memory_db, garmin_mail("Yes pair me", OTHER_TOKEN), resolver).action == "pairing"
        assert memory_db.query(Observation).count() == 0

    def test_repeat_pairing_keeps_one_row(self, memory_db, resolver):
        inbox.process_message(memory_db, garmin_mail("first", TOKEN1), resolver)
        inbox.process_message(memory_db, garmin_mail("second", TOKEN2), resolver)
        assert memory_db.query(InReachConversation).count() == 1
        assert _conv(memory_db).last_message == "second"

    def test_inactive_subscription_can_re_pair(self, memory_db, resolver):
        _sub(memory_db, active=False)
        assert inbox.process_message(memory_db, garmin_mail("back again"), resolver).action == "pairing"

    def test_expired_token_matches_stored_token(self, memory_db, resolver, caplog):
        sub = _sub(memory_db)
        _alert(memory_db, sub)
        inbox.process_message(memory_db, garmin_mail("hello", TOKEN1), resolver)
        del resolver.pages[TOKEN1]  # the token no longer resolves
        res = inbox.process_message(memory_db, garmin_mail("Y", TOKEN1), resolver)
        assert res.action == "observation" and res.key == KEY
        assert "matched a stored conversation" in caplog.text

    def test_unresolvable_unknown_token_is_skipped(self, memory_db, resolver):
        res = inbox.process_message(memory_db, garmin_mail("Y", "Unknown_token_00000000"), resolver)
        assert res == inbox.InboxResult("ignored", "Unknown_token_00000000")
        assert memory_db.query(InReachConversation).count() == 0

    def test_network_error_propagates(self, memory_db, resolver):
        resolver.error = httpx.ConnectError("down")
        with pytest.raises(httpx.ConnectError):
            inbox.process_message(memory_db, garmin_mail("Y"), resolver)

    def test_no_link_is_ignored(self, memory_db, resolver):
        msg = EmailMessage()
        msg["Subject"] = "Your Garmin invoice"
        msg.set_content("Thanks for your purchase")
        assert inbox.process_message(memory_db, msg, resolver) == inbox.InboxResult("ignored")
        assert resolver.calls == []


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
    def _poll(self, imap, db, resolver):
        return inbox.poll_once(connect=lambda: imap, session_factory=lambda: db, resolve=resolver)

    def test_processes_and_marks_seen(self, memory_db, monkeypatch, resolver):
        _alert(memory_db, _sub(memory_db))
        monkeypatch.setattr(memory_db, "close", lambda: None)
        imap = FakeImap({b"7": garmin_mail("Y"), b"8": garmin_mail("hello", OTHER_TOKEN)})
        assert [r.action for r in self._poll(imap, memory_db, resolver)] == ["observation", "pairing"]
        assert imap.seen == {b"7", b"8"}
        assert imap.logged_out
        assert imap.searches[0] == (None, "UNSEEN", "OR", "FROM", '"garmin.com"', "BODY", '"inreachlink.com"')
        assert self._poll(imap, memory_db, resolver) == []  # nothing new
        assert memory_db.query(Observation).count() == 1

    def test_garmin_unreachable_leaves_mail_unseen(self, memory_db, monkeypatch, resolver, caplog):
        monkeypatch.setattr(memory_db, "close", lambda: None)
        resolver.error = httpx.ConnectError("down")
        imap = FakeImap({b"1": garmin_mail("Y"), b"2": garmin_mail("N")})
        assert self._poll(imap, memory_db, resolver) == []
        assert imap.seen == set()
        assert len(resolver.calls) == 1  # stopped after the first failure
        assert "unreachable" in caplog.text

    def test_processing_error_leaves_mail_unseen(self, memory_db, monkeypatch, resolver):
        monkeypatch.setattr(memory_db, "close", lambda: None)

        def _boom(db, msg, resolve=None):
            raise RuntimeError("db locked")

        monkeypatch.setattr(inbox, "process_message", _boom)
        imap = FakeImap({b"1": garmin_mail("Y")})
        assert self._poll(imap, memory_db, resolver) == []
        assert imap.seen == set()

    def test_bad_folder_raises_and_logs_out(self, memory_db, resolver):
        imap = FakeImap({}, fail_select=True)
        with pytest.raises(inbox.imaplib.IMAP4.error):
            self._poll(imap, memory_db, resolver)
        assert imap.logged_out

    def test_poll_job_swallows_mailbox_errors(self, monkeypatch, caplog):
        def _down():
            raise OSError("network unreachable")

        monkeypatch.setattr(inbox, "poll_once", lambda: _down())
        inbox.poll_job()
        assert "IMAP poll failed" in caplog.text
