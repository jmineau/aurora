"""InReachNotifier against a fake Garmin reply site (mocked transport, never Garmin).

Flow, as the legacy page's own script does it: GET
``/textmessage/txtmsg?extId=<token>``, read the hidden inputs and
``replyCommon.init('<reply address>', '', '<isGSR56C>')``, POST JSON to
``/TextMessage/NonExploreUserTxtMsg`` (Messenger) or ``/TextMessage/TxtMsg``
(legacy).  Tokens and message guids change per message; the conversation is the
stable identity stored in ``InReachConversation``.  All ids are synthetic.
"""

import datetime as dt
import json

import httpx
import pytest

from aurora import inreach, notify
from aurora.config import Settings
from aurora.db import InReachConversation, utcnow
from aurora.notify_base import NotifierError

from .garmin_fake import (
    BASE, CONV, GUID1, GUID2, OTHER_CONV, TOKEN1, TOKEN2, FakeGarmin, fake_page, message_page,
)

TOKEN = TOKEN1
KEY = CONV["ConversationId"]


@pytest.fixture
def inreach_on(monkeypatch):
    s = Settings(_env_file=None, inreach_reply_address="aurora@example.com", inreach_base_url=BASE)
    monkeypatch.setattr(inreach, "settings", s)
    return s


@pytest.fixture
def garmin():
    return FakeGarmin()


@pytest.fixture
def notifier(garmin, memory_db):
    return inreach.InReachNotifier(transport=garmin.transport(), session_factory=lambda: memory_db)


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _conv(db) -> InReachConversation:
    db.expire_all()
    return db.query(InReachConversation).filter_by(conversation_key=KEY).one()


# ── Link / token parsing ──────────────────────────────────────────────────────

class TestTokens:
    @pytest.mark.parametrize(
        "raw",
        [
            TOKEN,
            f"  {TOKEN} ",
            f"https://inreachlink.com/{TOKEN}",
            f"inreachlink.com/{TOKEN}",
            f"https://messenger.garmin.com/r?extId={TOKEN}",
            f"https://explore.garmin.com/textmessage/txtmsg?extId={TOKEN}&adr=aurora%40example.com",
            f"https://inreach.garmin.com/TextMessage/TxtMsg?adr=a%40b.com&amp;extId={TOKEN}",
        ],
    )
    def test_normalize_accepts_token_and_links(self, raw):
        assert notify.normalize_recipient("inreach", raw) == TOKEN

    def test_legacy_uuid_extid_is_a_token_too(self):
        uuid = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
        link = f"https://explore.garmin.com/textmessage/txtmsg?extId={uuid}&adr=x"
        assert inreach.normalize_token(link) == uuid

    def test_case_is_preserved(self):
        assert inreach.normalize_token(TOKEN.upper()) == TOKEN.upper()

    @pytest.mark.parametrize(
        "raw", ["", "short", "+15551234567", "has spaces in it ok", "x" * 65, "https://example.com/" + TOKEN]
    )
    def test_normalize_rejects(self, raw):
        with pytest.raises(ValueError, match="token"):
            notify.normalize_recipient("inreach", raw)

    def test_extract_from_plain_email_body(self):
        body = (
            "Y saw it\n\nView the location or send a reply to James:\n"
            f"https://inreachlink.com/{TOKEN}\n\nJames sent this message from: Lat 45.8 Lon -78.4"
        )
        assert inreach.extract_token(body) == TOKEN

    def test_extract_from_messenger_and_legacy_links(self):
        assert inreach.extract_token(f'href="https://messenger.garmin.com/r?extId={TOKEN}"') == TOKEN
        assert inreach.extract_token(
            f"https://explore.garmin.com/textmessage/txtmsg?extId={TOKEN}&amp;adr=a"
        ) == TOKEN

    def test_extract_none(self):
        assert inreach.extract_token(f"https://example.com/?extId={TOKEN}") is None
        assert inreach.extract_token("") is None


class TestParsePage:
    def test_messenger_page(self):
        page = inreach.parse_reply_page(fake_page("True"), BASE + "/textmessage/txtmsg?extId=x")
        assert page.messenger is True
        assert page.reply_address == "aurora@example.com"
        assert page.message_guid == GUID1
        assert page.conversation_key == CONV["ConversationId"]

    def test_legacy_page_and_attribute_order_and_entities(self):
        html = (
            "<input value='22222222-0000-4000-8000-000000000000' type='hidden' name='Guid'>"
            '<input type="hidden" value="0" id="MessageId">'
            "<script>replyCommon.init(\"aurora&#64;example.com\", '', 'False')</script>"
        )
        page = inreach.parse_reply_page(html, "u")
        assert page.messenger is False
        assert page.reply_address == "aurora@example.com"
        assert page.fields == {"Guid": "22222222-0000-4000-8000-000000000000", "MessageId": "0"}
        assert page.conversation_key == "legacy:22222222-0000-4000-8000-000000000000"

    @pytest.mark.parametrize(
        "html",
        [
            fake_page(fields={}),
            fake_page("True", fields={"Guid": GUID1, "MessageId": "0"}),  # no conversation ids
            fake_page(init=False),
            "<html>Sign in to Garmin</html>",
        ],
    )
    def test_format_changed(self, html):
        with pytest.raises(NotifierError, match="page format changed"):
            inreach.parse_reply_page(html, "u")


# ── Pairing: token → conversation ─────────────────────────────────────────────

class TestRegister:
    def test_resolves_token_to_conversation(self, inreach_on, garmin, notifier, memory_db):
        assert notifier.register_recipient(memory_db, TOKEN) == KEY
        (get,) = garmin.gets
        assert get.url.params["extId"] == TOKEN
        conv = _conv(memory_db)
        assert conv.messenger is True
        assert conv.latest_token == TOKEN and conv.latest_message_guid == GUID1
        assert conv.messenger_account_id == CONV["MessengerAccountId"]
        assert conv.reply_address == "aurora@example.com"
        assert conv.resolved_at is not None
        # Known token or key: no second GET.
        assert notifier.register_recipient(memory_db, TOKEN) == KEY
        assert notifier.register_recipient(memory_db, KEY) == KEY
        assert len(garmin.gets) == 1

    def test_unresolvable_token_is_a_value_error(self, inreach_on, garmin, notifier, memory_db):
        with pytest.raises(ValueError, match="GET /pairings"):
            notifier.register_recipient(memory_db, "NoSuchToken_000000000")

    def test_page_http_error_is_a_value_error(self, inreach_on, garmin, notifier, memory_db):
        garmin.page_status = 404
        with pytest.raises(ValueError, match="HTTP 404"):
            notifier.register_recipient(memory_db, TOKEN)

    def test_legacy_conversation_key(self, inreach_on, garmin, notifier, memory_db):
        garmin.pages = {TOKEN: fake_page("False", fields={"Guid": GUID1, "MessageId": "0"})}
        assert notifier.register_recipient(memory_db, TOKEN) == f"legacy:{GUID1}"

    def test_get_retries_once(self, inreach_on, garmin, notifier, memory_db):
        garmin.get_errors = [httpx.ReadTimeout("slow")]
        assert notifier.register_recipient(memory_db, TOKEN) == KEY
        assert len(garmin.gets) == 2

    def test_follows_redirect_to_explore(self, monkeypatch, memory_db):
        monkeypatch.setattr(inreach, "settings", Settings(
            _env_file=None, inreach_reply_address="aurora@example.com", inreach_base_url="http://inreach.test"
        ))
        g = FakeGarmin()

        def handler(request):
            if request.url.host == "inreach.test":
                return httpx.Response(301, headers={"Location": f"{BASE}{request.url.raw_path.decode()}"})
            return g(request)

        n = inreach.InReachNotifier(transport=httpx.MockTransport(handler), session_factory=lambda: memory_db)
        n.register_recipient(memory_db, TOKEN)
        n.send(KEY, "s", "hi")
        (post,) = g.posts
        assert str(post.url) == BASE + "/TextMessage/NonExploreUserTxtMsg"
        assert post.headers["origin"] == BASE


# ── Sending ───────────────────────────────────────────────────────────────────

class TestSend:
    def test_messenger_payload_uses_stored_ids_without_get(self, inreach_on, garmin, notifier, memory_db):
        notifier.register_recipient(memory_db, TOKEN)
        text = "Aurora alert, x: 70% chance. Saw it? Reply Y or N"
        assert notifier.send(KEY, "Aurora alert", text) == "garmin-accepted"
        assert len(garmin.gets) == 1  # only the pairing GET; the cache was fresh
        (post,) = garmin.posts
        assert str(post.url) == BASE + "/TextMessage/NonExploreUserTxtMsg"
        assert _json(post) == {
            "ReplyAddress": "aurora@example.com",
            "ReplyMessage": text,
            "MessageGuid": GUID1,
            "ConversationId": CONV["ConversationId"],
            "MessengerAccountId": CONV["MessengerAccountId"],
            "SenderMessengerAccountId": CONV["SenderMessengerAccountId"],
        }

    def test_legacy_payload(self, inreach_on, garmin, notifier, memory_db):
        garmin.pages = {TOKEN: fake_page("False", fields={"Guid": GUID1, "MessageId": "0"})}
        key = notifier.register_recipient(memory_db, TOKEN)
        notifier.send(key, "s", "hello")
        (post,) = garmin.posts
        assert str(post.url) == BASE + "/TextMessage/TxtMsg"
        assert _json(post) == {
            "ReplyAddress": "aurora@example.com", "ReplyMessage": "hello", "Guid": GUID1, "MessageId": "0",
        }

    def test_headers_and_cookies(self, inreach_on, garmin, notifier, memory_db):
        notifier.register_recipient(memory_db, TOKEN)
        notifier.send(KEY, "s", "hello")
        (post,) = garmin.posts
        assert post.headers["content-type"] == "application/json"
        assert post.headers["accept"] == "application/json, text/javascript, */*; q=0.01"
        assert post.headers["x-requested-with"] == "XMLHttpRequest"
        assert post.headers["origin"] == BASE
        assert post.headers["referer"] == f"{BASE}/textmessage/txtmsg?extId={TOKEN}"
        assert "ASP.NET_SessionId=abc123" in post.headers["cookie"]

    def test_replies_to_newest_message(self, inreach_on, garmin, notifier, memory_db):
        notifier.register_recipient(memory_db, TOKEN)
        # The poller saw a newer message in the same conversation.
        page = inreach.parse_reply_page(message_page(GUID2), f"{BASE}/textmessage/txtmsg?extId={TOKEN2}")
        inreach.record_page(memory_db, page, TOKEN2)
        memory_db.commit()
        notifier.send(KEY, "s", "hi")
        assert _json(garmin.posts[0])["MessageGuid"] == GUID2

    def test_stale_cache_rereads_newest_token(self, inreach_on, garmin, notifier, memory_db):
        notifier.register_recipient(memory_db, TOKEN)
        conv = _conv(memory_db)
        conv.latest_token = TOKEN2
        conv.resolved_at = utcnow() - dt.timedelta(hours=25)
        memory_db.commit()
        garmin.pages[TOKEN2] = message_page(GUID2)
        notifier.send(KEY, "s", "hi")
        assert garmin.gets[-1].url.params["extId"] == TOKEN2
        assert _json(garmin.posts[0])["MessageGuid"] == GUID2
        assert utcnow() - _conv(memory_db).resolved_at < dt.timedelta(minutes=1)

    def test_expired_token_falls_back_to_stored_ids(self, inreach_on, garmin, notifier, memory_db, caplog):
        notifier.register_recipient(memory_db, TOKEN)
        conv = _conv(memory_db)
        conv.resolved_at = utcnow() - dt.timedelta(hours=25)
        memory_db.commit()
        garmin.pages = {}  # every token now gets a page without inputs
        notifier.send(KEY, "s", "hi")
        assert "token expired" in caplog.text
        assert _json(garmin.posts[0])["MessageGuid"] == GUID1

    def test_token_resolving_to_another_conversation_is_ignored(self, inreach_on, garmin, notifier, memory_db, caplog):
        notifier.register_recipient(memory_db, TOKEN)
        conv = _conv(memory_db)
        conv.resolved_at = None
        memory_db.commit()
        garmin.pages[TOKEN] = message_page(GUID2, OTHER_CONV)
        notifier.send(KEY, "s", "hi")
        assert "keeping stored ids" in caplog.text
        assert _json(garmin.posts[0])["ConversationId"] == CONV["ConversationId"]

    def test_page_reply_address_wins_with_warning(self, inreach_on, garmin, notifier, memory_db, caplog):
        garmin.pages = {TOKEN: message_page(GUID1, address="other@example.org")}
        notifier.register_recipient(memory_db, TOKEN)
        notifier.send(KEY, "s", "hi")
        assert _json(garmin.posts[0])["ReplyAddress"] == "other@example.org"
        assert "not INREACH_REPLY_ADDRESS" in caplog.text

    def test_setting_is_fallback_when_page_has_no_address(self, inreach_on, garmin, notifier, memory_db):
        garmin.pages = {TOKEN: message_page(GUID1, address="")}
        notifier.register_recipient(memory_db, TOKEN)
        notifier.send(KEY, "s", "hi")
        assert _json(garmin.posts[0])["ReplyAddress"] == "aurora@example.com"

    def test_body_sanitized_on_the_wire(self, inreach_on, garmin, notifier, memory_db):
        notifier.register_recipient(memory_db, TOKEN)
        notifier.send(KEY, "s", "at 15° — " + "x" * 300)
        sent = _json(garmin.posts[0])["ReplyMessage"]
        assert len(sent) <= 160 and sent.isascii() and sent.startswith("at 15deg - ")

    def test_unknown_conversation(self, inreach_on, notifier):
        with pytest.raises(NotifierError, match="pair the device first"):
            notifier.send("nope-nope-nope-nope", "s", "hi")

    def test_collect_only_never_touches_http(self, monkeypatch, memory_db, caplog):
        monkeypatch.setattr(inreach, "settings", Settings(_env_file=None, inreach_base_url=BASE))

        def handler(request):
            raise AssertionError("must not touch Garmin in collect-only mode")

        caplog.set_level("INFO")
        n = inreach.InReachNotifier(transport=httpx.MockTransport(handler), session_factory=lambda: memory_db)
        assert n.enabled is False
        assert n.send(KEY, "s", "hello") is None
        assert "would have sent" in caplog.text


class TestRetryRules:
    """At most one retry, and only when the first POST provably wasn't delivered."""

    def _paired(self, notifier, memory_db):
        notifier.register_recipient(memory_db, TOKEN)

    @pytest.mark.parametrize("status", [500, 503, 302, 404])
    def test_non_2xx_rereads_page_and_retries_once(self, inreach_on, garmin, notifier, memory_db, status):
        self._paired(notifier, memory_db)
        garmin.pages[TOKEN] = message_page(GUID2)  # the re-read sees fresh ids
        garmin.post_responses = [httpx.Response(status, text="nope")]
        assert notifier.send(KEY, "s", "hi") == "garmin-accepted"
        assert len(garmin.posts) == 2
        assert len(garmin.gets) == 2  # pairing + one re-read
        assert _json(garmin.posts[1])["MessageGuid"] == GUID2

    def test_second_failure_gives_up(self, inreach_on, garmin, notifier, memory_db, caplog):
        self._paired(notifier, memory_db)
        garmin.post_responses = [httpx.Response(500, text="E" * 5000), httpx.Response(500, text="again")]
        with pytest.raises(NotifierError, match="HTTP 500"):
            notifier.send(KEY, "s", "hi")
        assert len(garmin.posts) == 2
        assert _conv(memory_db).resolved_at is None  # re-read before the next attempt
        first = next(r.getMessage() for r in caplog.records if "retrying once" in r.getMessage())
        assert first.count("E") <= 510

    @pytest.mark.parametrize(
        "response, match",
        [
            (httpx.Response(200, text="<html>Sign in</html>"), "non-JSON"),
            (httpx.Response(200, json={"Success": False, "Error": "bad"}), "Success"),
            (httpx.Response(200, json={"Success": "true"}), "Success"),
            (httpx.Response(200, json=[True]), "Success"),
        ],
    )
    def test_never_retried_after_http_200(self, inreach_on, garmin, notifier, memory_db, response, match, caplog):
        self._paired(notifier, memory_db)
        garmin.post_responses = [response]
        with pytest.raises(NotifierError, match=match):
            notifier.send(KEY, "s", "hi")
        assert len(garmin.posts) == 1
        assert any("inReach send failed" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")

    def test_connect_error_retries_once(self, inreach_on, garmin, notifier, memory_db):
        self._paired(notifier, memory_db)
        garmin.post_responses = [httpx.ConnectError("refused")]
        assert notifier.send(KEY, "s", "hi") == "garmin-accepted"
        assert len(garmin.posts) == 2

    def test_read_timeout_is_not_retried(self, inreach_on, garmin, notifier, memory_db):
        # Garmin may already have delivered (and billed) the message.
        self._paired(notifier, memory_db)
        garmin.post_responses = [httpx.ReadTimeout("slow")]
        with pytest.raises(httpx.ReadTimeout):
            notifier.send(KEY, "s", "hi")
        assert len(garmin.posts) == 1
