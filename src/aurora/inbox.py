"""The aurora mailbox: inReach pairing requests and Y/N replies over IMAP.

An inReach has no address we can push to, but every message it (or the Garmin
Messenger app) sends to an email address arrives with a reply link carrying a
per-message token (``inreach.extract_token``).  The scheduler calls
``poll_job`` every ``IMAP_POLL_SECONDS``; it reads unseen Garmin mail and, per
message, reads Garmin's reply page for the token (a free GET) to learn the
stable conversation, then:

* records the newest token and message guid on that ``InReachConversation``
  (always, so the next alert replies to the most recent message);
* **reply**: a subscription uses the conversation and the first word parses as
  Y/N (``feedback.parse_reply``) → an ``Observation`` with
  ``source="inreach_reply"``, linked to the latest alerted snapshot for the
  conversation, exactly like the SMS webhook does for a phone number;
* **pairing**: no active subscription uses the conversation → it is listed at
  ``GET /pairings`` (the token is logged at INFO) so the owner can finish
  with ``POST /subscribe``.

If the page can't be read (token expired, page changed) the token is matched
against the tokens already stored.  Processed mail is marked seen; other mail
in the folder is left alone.

Email subscribers (``channel="email"``) reply to the alert directly: after the
Garmin pass, ``_poll_email_replies`` reads unseen mail from each active email
subscriber's address, and a leading Y/N becomes an ``Observation`` with
``source="email_reply"``.  Anything else from them is marked seen and logged.  Nothing is ever sent back to the device from here
(each message costs satellite allowance).
"""

from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import html
import imaplib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from aurora.config import settings
from aurora import notify
from aurora.db import SessionLocal, Subscription
from aurora.feedback import parse_reply, record_observation
from aurora.inreach import (
    InReachNotifier,
    ReplyPage,
    extract_token,
    find_conversation,
    record_page,
)
from aurora.notify_base import NotifierError

log = logging.getLogger(__name__)

INREACH_SOURCE = "inreach_reply"
EMAIL_SOURCE = "email_reply"
# Mail from an inReach or the Garmin Messenger app comes from garmin.com and
# carries an inreachlink.com reply link; searching server-side for either keeps
# us from downloading the rest of the mailbox.
_SEARCH = ("UNSEEN", "OR", "FROM", '"garmin.com"', "BODY", '"inreachlink.com"')


@dataclass(frozen=True)
class InboxResult:
    action: str  # "observation" | "message" | "pairing" | "ignored"
    key: str | None = None  # conversation key (or the unresolved token, for "ignored")
    saw_aurora: bool | None = None


# ── Parsing (pure) ────────────────────────────────────────────────────────────

def message_text(msg: email.message.Message) -> str:
    """Plain-text body of an email (HTML stripped if there is no text part)."""
    if isinstance(msg, email.message.EmailMessage):
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content()
        subtype = part.get_content_subtype()
    else:  # compat32 Message
        part = next(
            (p for p in msg.walk() if p.get_content_type() in ("text/plain", "text/html")), None
        )
        if part is None:
            return ""
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", "replace")
        subtype = part.get_content_subtype()
    if subtype == "html":
        # Keep hrefs visible so the reply link survives tag stripping.
        content = re.sub(r"""<a\s[^>]*href=["']([^"']+)["'][^>]*>""", r" \1 ", content, flags=re.I)
        content = re.sub(r"<(br|/p|/div)[^>]*>", "\n", content, flags=re.I)
        content = html.unescape(re.sub(r"<[^>]+>", " ", content))
    return content.strip()


# ── Processing one message ────────────────────────────────────────────────────

def process_message(
    db: Session,
    msg: email.message.Message,
    resolve: Callable[[str], ReplyPage] | None = None,
) -> InboxResult:
    """Turn one Garmin email into an observation, a pairing, or nothing.

    *resolve* reads Garmin's reply page for a token (default: the inReach
    notifier's).  Network errors propagate, so the mail stays unseen and is
    retried on the next poll.
    """
    text = message_text(msg)
    token = extract_token(text)
    subject = str(msg.get("Subject", ""))[:120]
    if token is None:
        log.info("Mail from Garmin without an inReach reply link (subject %r); skipping.", subject)
        return InboxResult("ignored")

    first_line = text.splitlines()[0][:160] if text else ""
    resolve = resolve or notify.get_notifier(InReachNotifier.channel).resolve
    try:
        page = resolve(token)
    except NotifierError as exc:
        conv = find_conversation(db, token)
        if conv is None:
            log.warning("inReach token %s could not be resolved (%s); skipping this mail.", token, exc)
            return InboxResult("ignored", token)
        log.warning("inReach token %s did not resolve (%s); matched a stored conversation.", token, exc)
    else:
        conv = record_page(db, page, token, sender=subject or None, last_message=first_line or None)
    db.commit()
    key = conv.conversation_key

    subs = db.query(Subscription).filter(
        Subscription.channel == InReachNotifier.channel, Subscription.recipient == key
    ).all()
    saw = parse_reply(text)

    if subs and saw is not None:
        record_observation(
            db,
            saw_aurora=saw,
            observed_at=_sent_at(msg),
            recipient=key,
            source=INREACH_SOURCE,
            link="reply",
        )
        log.info("inReach reply in conversation %s recorded: saw_aurora=%s", key, saw)
        return InboxResult("observation", key, saw)

    if any(s.active for s in subs):
        log.info("Message in paired inReach conversation %s (not a Y/N reply): %r", key, first_line)
        return InboxResult("message", key)

    log.info(
        "inReach pairing request: conversation %s, token %s (%s). "
        "Subscribe with channel=inreach and that token.", key, token, subject,
    )
    return InboxResult("pairing", key)


def _sent_at(msg: email.message.Message):
    """The email's Date header as a datetime, or None (→ now)."""
    try:
        return email.utils.parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
    except (TypeError, ValueError):
        return None


# ── IMAP polling ──────────────────────────────────────────────────────────────

def _connect() -> imaplib.IMAP4:
    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
    conn.login(settings.imap_user, settings.imap_password)
    return conn


def _poll_email_replies(
    conn: imaplib.IMAP4,
    session_factory: Callable[[], Session],
    results: list[InboxResult],
) -> None:
    """Y/N replies from email subscribers.  Mail from other senders is untouched."""
    db = session_factory()
    try:
        addresses = [
            r for (r,) in db.query(Subscription.recipient)
            .filter(Subscription.channel == "email", Subscription.active.is_(True))
            .distinct()
        ]
    finally:
        db.close()
    for address in addresses:
        typ, data = conn.uid("SEARCH", None, "UNSEEN", "FROM", f'"{address}"')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        for uid in uids:
            typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ != "OK" or raw is None:
                log.warning("IMAP fetch of message %s failed; leaving it unseen.", uid)
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            # IMAP FROM is a substring match; require the exact sender.
            if email.utils.parseaddr(str(msg.get("From", "")))[1].lower() != address:
                continue
            saw = parse_reply(message_text(msg))
            db = session_factory()
            try:
                if saw is None:
                    log.info("Email from subscriber %s is not a Y/N reply; ignoring.", address)
                    results.append(InboxResult("message", address))
                else:
                    record_observation(
                        db, saw_aurora=saw, observed_at=_sent_at(msg), recipient=address,
                        source=EMAIL_SOURCE, link="reply",
                    )
                    log.info("Email reply from %s recorded: saw_aurora=%s", address, saw)
                    results.append(InboxResult("observation", address, saw))
            except Exception:
                log.exception("Could not process mail %s; leaving it unseen.", uid)
                db.rollback()
                continue
            finally:
                db.close()
            conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")


def poll_once(
    connect: Callable[[], imaplib.IMAP4] = _connect,
    session_factory: Callable[[], Session] = SessionLocal,
    resolve: Callable[[str], ReplyPage] | None = None,
) -> list[InboxResult]:
    """Process every unseen Garmin message in IMAP_FOLDER once.

    A message is marked seen only after it was processed; one that raised stays
    unseen and is tried again on the next poll.
    """
    results: list[InboxResult] = []
    conn = connect()
    try:
        typ, _ = conn.select(settings.imap_folder)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot select folder {settings.imap_folder!r}")
        typ, data = conn.uid("SEARCH", None, *_SEARCH)
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        for uid in uids:
            typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ != "OK" or raw is None:
                log.warning("IMAP fetch of message %s failed; leaving it unseen.", uid)
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            db = session_factory()
            try:
                results.append(process_message(db, msg, resolve))
            except httpx.TransportError as exc:
                # Garmin unreachable: the rest would fail too.  Retry next poll.
                log.warning("Garmin reply page unreachable (%s); leaving mail unseen.", exc)
                db.rollback()
                break
            except Exception:
                log.exception("Could not process mail %s; leaving it unseen.", uid)
                db.rollback()
                continue
            finally:
                db.close()
            conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        _poll_email_replies(conn, session_factory, results)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return results


def poll_job() -> None:
    """Scheduler entry point: poll once, logging (not raising) mailbox errors."""
    try:
        results = poll_once()
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("IMAP poll failed: %s", exc)
        return
    if results:
        log.info("IMAP poll processed %d message(s).", len(results))
