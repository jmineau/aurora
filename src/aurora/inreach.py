"""Garmin inReach channel, delivered through Garmin's legacy email-reply page.

Garmin gives an inReach no address we can push to, but every email from the
device (or the Garmin Messenger app) carries a reply link whose ``extId`` is a
short token, e.g. ``https://inreachlink.com/<token>``.  The token opens the
legacy reply page ``{INREACH_BASE_URL}/textmessage/txtmsg?extId=<token>``,
whose hidden inputs hold the ids the page's own script POSTs as JSON:

* Messenger conversations (``replyCommon.init(addr, '', 'True')``):
  ``/TextMessage/NonExploreUserTxtMsg`` with ``MessageGuid``,
  ``ConversationId``, ``MessengerAccountId``, ``SenderMessengerAccountId``.
* Legacy (``'False'``): ``/TextMessage/TxtMsg`` with ``Guid`` and ``MessageId``.

Both answer HTTP 200 ``{"Success": true}`` on acceptance.

**Identity.** The token and the page's message ``Guid`` change with every
message; ``ConversationId`` and the account ids don't.  So a subscription's
recipient is the *conversation key* (``db.InReachConversation``), and the
mailbox poller (``inbox.py``) refreshes the conversation's newest token and
message guid on every email, so alerts reply to the latest message.

**Cost rules.** Each message the device receives counts against the owner's
Garmin plan.  The page GET is free and may retry once.  The POST is retried at
most once, and only when it can't have been delivered: a connection error, or
a non-2xx answer (after re-reading the page from the newest token).  Never
after an HTTP 200, even one without ``Success: true``.

All of this is undocumented and scraped; see docs/roadmap.md (Risks).
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import httpx
from sqlalchemy.orm import Session

from aurora import messages
from aurora.config import settings
from aurora.db import InReachConversation, SessionLocal, utcnow
from aurora.notify_base import NotifierError

log = logging.getLogger(__name__)

# ── Reply-link tokens ─────────────────────────────────────────────────────────

# Forms seen in Garmin mail (older mail used a UUID as the extId; it matches too):
#   https://inreachlink.com/<token>                              (current short link)
#   https://messenger.garmin.com/r?extId=<token>                 (where it redirects)
#   https://explore.garmin.com/textmessage/txtmsg?extId=<token>  (legacy page)
_TOKEN = r"[A-Za-z0-9_-]{16,64}"
_LINK_RE = re.compile(
    r"(?:inreachlink\.com/|garmin\.com/(?:r|textmessage/txtmsg)\?[^\s\"'<>]*?extId=)"
    r"(" + _TOKEN + r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def normalize_token(value: str) -> str:
    """The extId token from a bare token or any Garmin reply-link URL.

    Tokens are case-sensitive and returned unchanged.  A conversation key (a
    UUID) passes too.  Raises ValueError for anything else.
    """
    raw = (value or "").strip()
    m = _LINK_RE.search(raw)
    if m:
        return m.group(1)
    if re.fullmatch(_TOKEN, raw) or raw.startswith("legacy:"):
        return raw
    raise ValueError(
        "inReach recipient must be the reply-link token (e.g. from https://inreachlink.com/<token>) "
        "or the link itself."
    )


def extract_token(text: str) -> str | None:
    """Find the extId token of a Garmin reply link inside *text*, if any."""
    m = _LINK_RE.search(text or "")
    return m.group(1) if m else None


# ── The reply page ────────────────────────────────────────────────────────────

TXTMSG_PAGE = "/textmessage/txtmsg"
LEGACY_POST = "/TextMessage/TxtMsg"
MESSENGER_POST = "/TextMessage/NonExploreUserTxtMsg"
RESOLVE_TTL = dt.timedelta(hours=24)


@dataclass(frozen=True)
class ReplyPage:
    """What one txtmsg page says about its conversation and message."""

    url: str  # final page URL (after redirects)
    reply_address: str
    messenger: bool  # replyCommon.init's isGSR56C flag
    fields: dict[str, str]  # hidden inputs by name

    @property
    def message_guid(self) -> str:
        return self.fields["Guid"]

    @property
    def conversation_key(self) -> str:
        return self.fields.get("ConversationId") or f"legacy:{self.message_guid}"


_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b(name|id|value)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_INIT_RE = re.compile(
    r"""replyCommon\.init\(\s*(['"])(.*?)\1\s*,\s*(['"])(.*?)\3\s*,\s*(['"])(.*?)\5""",
    re.DOTALL,
)
_REQUIRED = {
    False: ("Guid",),
    True: ("Guid", "ConversationId", "MessengerAccountId", "SenderMessengerAccountId"),
}


def parse_reply_page(page_html: str, url: str) -> ReplyPage:
    """Read the hidden inputs and ``replyCommon.init(...)`` from a txtmsg page.

    Raises NotifierError("page format changed ...") when the pieces we rely on
    are missing: an expired token, or a redesign on Garmin's side.
    """
    fields: dict[str, str] = {}
    for tag in _INPUT_RE.findall(page_html or ""):
        attrs = {k.lower(): html.unescape(a if a else b) for k, a, b in _ATTR_RE.findall(tag)}
        key = attrs.get("name") or attrs.get("id")
        if key and "value" in attrs:
            fields[key] = attrs["value"]
    init = _INIT_RE.search(page_html or "")
    if init is None:
        raise NotifierError("Garmin reply page format changed (or token expired): no replyCommon.init(...)")
    messenger = init.group(6).strip().lower() == "true"
    missing = [k for k in _REQUIRED[messenger] if not fields.get(k)]
    if missing:
        raise NotifierError(
            f"Garmin reply page format changed (or token expired): missing {', '.join(missing)}"
        )
    return ReplyPage(
        url=url,
        reply_address=html.unescape(init.group(2)).strip(),
        messenger=messenger,
        fields=fields,
    )


def record_page(
    db: Session,
    page: ReplyPage,
    token: str,
    *,
    sender: str | None = None,
    last_message: str | None = None,
) -> InReachConversation:
    """Create or refresh the conversation row from a freshly read page (no commit)."""
    key = page.conversation_key
    conv = db.query(InReachConversation).filter(InReachConversation.conversation_key == key).first()
    if conv is None:
        conv = InReachConversation(conversation_key=key, first_seen_at=utcnow())
        db.add(conv)
        log.info("New inReach conversation %s (token %s).", key, token)
    f = page.fields
    conv.messenger = page.messenger
    conv.conversation_id = f.get("ConversationId")
    conv.messenger_account_id = f.get("MessengerAccountId")
    conv.sender_messenger_account_id = f.get("SenderMessengerAccountId")
    conv.reply_address = page.reply_address or conv.reply_address
    conv.page_url = page.url
    conv.latest_token = token
    conv.latest_message_guid = page.message_guid
    conv.message_id = f.get("MessageId") or "0"
    conv.resolved_at = utcnow()
    conv.last_seen_at = utcnow()
    if sender is not None:
        conv.sender = sender
    if last_message is not None:
        conv.last_message = last_message
    return conv


def find_conversation(db: Session, value: str) -> InReachConversation | None:
    """A stored conversation by key or by its latest token."""
    q = db.query(InReachConversation)
    return (
        q.filter(InReachConversation.conversation_key == value).first()
        or q.filter(InReachConversation.latest_token == value).first()
    )


def reply_payload(conv: InReachConversation, reply_address: str, text: str) -> tuple[str, dict]:
    """(path, JSON body) for the conversation type, as the page's own script builds it."""
    if conv.messenger:
        return MESSENGER_POST, {
            "ReplyAddress": reply_address,
            "ReplyMessage": text,
            "MessageGuid": conv.latest_message_guid,
            "ConversationId": conv.conversation_id,
            "MessengerAccountId": conv.messenger_account_id,
            "SenderMessengerAccountId": conv.sender_messenger_account_id,
        }
    return LEGACY_POST, {
        "ReplyAddress": reply_address,
        "ReplyMessage": text,
        "Guid": conv.latest_message_guid,
        "MessageId": conv.message_id or "0",
    }


# ── Notifier ──────────────────────────────────────────────────────────────────

class InReachNotifier:
    """Garmin inReach: recipient is an ``InReachConversation.conversation_key``."""

    channel: ClassVar[str] = "inreach"

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        # Tests inject an httpx.MockTransport and an in-memory session.
        self._transport = transport
        self._session_factory = session_factory
        self._client: httpx.Client | None = None

    @staticmethod
    def normalize(recipient: str) -> str:
        return normalize_token(recipient)

    @property
    def enabled(self) -> bool:
        return settings.inreach_enabled

    def _http(self) -> httpx.Client:
        # One long-lived client: its cookie jar carries page cookies to the POST.
        if self._client is None:
            self._client = httpx.Client(
                timeout=settings.inreach_timeout_seconds,
                transport=self._transport,
                follow_redirects=True,  # inreach.garmin.com now 301s to explore.garmin.com
                headers={"User-Agent": "Mozilla/5.0 (compatible; aurora-alert/1.0)"},
            )
        return self._client

    # Reading Garmin's page (free; never sends anything to the device).

    def resolve(self, token: str) -> ReplyPage:
        """GET and parse the reply page for *token* (one retry on transport errors)."""
        url = settings.inreach_base_url.rstrip("/") + TXTMSG_PAGE
        client = self._http()
        try:
            resp = client.get(url, params={"extId": token})
        except httpx.TransportError as exc:
            log.warning("inReach reply page GET failed (%s); retrying once.", exc)
            resp = client.get(url, params={"extId": token})
        if resp.status_code != 200:
            log.warning(
                "inReach reply page failed: HTTP %d, body: %s", resp.status_code, resp.text[:500]
            )
            raise NotifierError(f"Garmin reply page returned HTTP {resp.status_code}")
        try:
            return parse_reply_page(resp.text, str(resp.url))
        except NotifierError:
            log.warning("inReach reply page for token %s not understood, body: %s", token, resp.text[:500])
            raise

    def register_recipient(self, db: Session, recipient: str) -> str:
        """Conversation key for a token/link (or key) given at /subscribe."""
        conv = find_conversation(db, recipient)
        if conv is None:
            try:
                conv = record_page(db, self.resolve(recipient), recipient)
            except (NotifierError, httpx.TransportError) as exc:
                raise ValueError(
                    f"Could not read Garmin's reply page for that token ({exc}). Send a message "
                    "from the device to the aurora mailbox and use the token from GET /pairings."
                ) from None
            db.commit()
        return conv.conversation_key

    def _refresh(self, db: Session, conv: InReachConversation) -> None:
        """Re-read the page from the newest token; keep the stored ids if that fails."""
        try:
            page = self.resolve(conv.latest_token)
        except (NotifierError, httpx.TransportError) as exc:
            log.warning(
                "inReach token %s for conversation %s did not resolve (%s); token expired? "
                "Using the stored ids from the newest message seen.",
                conv.latest_token, conv.conversation_key, exc,
            )
            return
        if page.conversation_key != conv.conversation_key:
            log.warning(
                "inReach token %s now resolves to conversation %s, not %s; keeping stored ids.",
                conv.latest_token, page.conversation_key, conv.conversation_key,
            )
            return
        record_page(db, page, conv.latest_token)

    # Sending (each delivered message costs money).

    def _post(self, conv: InReachConversation, text: str) -> httpx.Response:
        reply_address = conv.reply_address or settings.inreach_reply_address
        if conv.reply_address and settings.inreach_reply_address and (
            conv.reply_address.lower() != settings.inreach_reply_address.lower()
        ):
            log.warning(
                "inReach conversation replies as %s, not INREACH_REPLY_ADDRESS %s; using the page's.",
                conv.reply_address, settings.inreach_reply_address,
            )
        path, payload = reply_payload(conv, reply_address, text)
        page_url = conv.page_url or ""
        parts = urllib.parse.urlsplit(page_url)
        if not (parts.scheme and parts.netloc):
            page_url = settings.inreach_base_url.rstrip("/") + TXTMSG_PAGE + "?extId=" + conv.latest_token
            parts = urllib.parse.urlsplit(page_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": origin,
            "Referer": page_url,
        }
        return self._http().post(origin + path, json=payload, headers=headers, follow_redirects=False)

    def send(self, recipient: str, subject: str, body: str) -> str | None:
        # Defensive: never put more than one satellite message's worth of
        # ASCII on the wire, whoever built the body.
        text = messages.to_compact_text(body)
        if not self.enabled:
            log.info("inReach disabled (collect-only); would have sent to %s:\n%s", recipient, text)
            return None

        with self._session_factory() as db:
            conv = find_conversation(db, recipient)
            if conv is None:
                raise NotifierError(f"No inReach conversation {recipient!r}; pair the device first.")
            try:
                if conv.resolved_at is None or utcnow() - conv.resolved_at >= RESOLVE_TTL:
                    self._refresh(db, conv)
                try:
                    resp: httpx.Response | None = self._post(conv, text)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    log.warning("inReach POST could not connect (%s).", exc)
                    resp = None
                if resp is None or not resp.is_success:
                    # Not delivered: re-read the page from the newest token, retry once.
                    if resp is not None:
                        log.warning(
                            "inReach send failed: HTTP %d, body: %s; re-resolving and retrying once.",
                            resp.status_code, resp.text[:500],
                        )
                    self._refresh(db, conv)
                    resp = self._post(conv, text)
                msg_id = _check(resp)
            except Exception:
                conv.resolved_at = None  # re-read the page before the next attempt
                db.commit()
                raise
            db.commit()
        log.info("inReach message accepted by Garmin for conversation %s.", recipient)
        return msg_id


def _check(resp: httpx.Response) -> str:
    """Message id for an accepted send; NotifierError (logged) for anything else."""
    snippet = resp.text[:500]
    if resp.status_code != 200:
        log.warning("inReach send failed: HTTP %d, body: %s", resp.status_code, snippet)
        raise NotifierError(f"Garmin returned HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        log.warning("inReach send failed: HTTP 200 but not JSON, body: %s", snippet)
        raise NotifierError("Garmin returned a non-JSON response") from None
    if not (isinstance(payload, dict) and payload.get("Success") is True):
        log.warning("inReach send failed: Success is not true, body: %s", snippet)
        raise NotifierError("Garmin did not report Success")
    msg_id = payload.get("MessageId") or payload.get("Id")
    return str(msg_id) if msg_id else "garmin-accepted"
