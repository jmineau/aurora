"""The aurora mailbox: inReach pairing requests and Y/N replies over IMAP.

An inReach has no address we can push to, but every message it sends to an
email address arrives with a reply link carrying the device's GUID (``extId``,
see ``notify.extract_guid``).  The scheduler calls ``poll_job`` every
``IMAP_POLL_SECONDS``; it reads unseen mail from Garmin and, per message:

* **Reply**: the GUID belongs to a subscription and the first word parses as
  Y/N (``feedback.parse_reply``) → an ``Observation`` with
  ``source="inreach_reply"``, linked to the latest alerted snapshot for that
  GUID, exactly like the SMS webhook does for a phone number.
* **Pairing**: anything else.  The GUID is logged at INFO and, if no active
  subscription uses it, kept in ``pending_pairings`` (listed at
  ``GET /pairings``) so the owner can finish with ``POST /subscribe``.

Processed mail is marked seen; other mail in the folder is left alone.  Nothing
is ever sent back to the device from here (each message costs satellite
allowance).
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

from sqlalchemy.orm import Session

from aurora.config import settings
from aurora.db import PendingPairing, SessionLocal, Subscription, utcnow
from aurora.feedback import parse_reply, record_observation
from aurora.notify import InReachNotifier, extract_guid

log = logging.getLogger(__name__)

INREACH_SOURCE = "inreach_reply"
# Garmin sends inReach email from no.reply.inreach@garmin.com; filtering on the
# domain server-side keeps us from downloading the rest of the mailbox.
_SEARCH = ("UNSEEN", "FROM", '"garmin.com"')


@dataclass(frozen=True)
class InboxResult:
    action: str  # "observation" | "pairing" | "ignored"
    guid: str | None = None
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

def process_message(db: Session, msg: email.message.Message) -> InboxResult:
    """Turn one Garmin email into an observation, a pending pairing, or nothing."""
    text = message_text(msg)
    guid = extract_guid(text)
    subject = str(msg.get("Subject", ""))[:120]
    if guid is None:
        log.info("Mail from Garmin without an inReach reply link (subject %r); skipping.", subject)
        return InboxResult("ignored")

    subs = db.query(Subscription).filter(
        Subscription.channel == InReachNotifier.channel, Subscription.recipient == guid
    ).all()
    saw = parse_reply(text)

    if subs and saw is not None:
        observed_at = _sent_at(msg)
        record_observation(
            db,
            saw_aurora=saw,
            observed_at=observed_at,
            recipient=guid,
            source=INREACH_SOURCE,
            link="reply",
        )
        log.info("inReach reply from %s recorded: saw_aurora=%s", guid, saw)
        return InboxResult("observation", guid, saw)

    first_line = text.splitlines()[0][:160] if text else ""
    if any(s.active for s in subs):
        log.info("Message from paired inReach %s (not a Y/N reply): %r", guid, first_line)
        return InboxResult("pairing", guid)

    log.info("inReach pairing request: GUID %s (%s). Subscribe with channel=inreach.", guid, subject)
    pending = db.query(PendingPairing).filter(PendingPairing.guid == guid).first()
    if pending is None:
        pending = PendingPairing(guid=guid)
        db.add(pending)
    pending.sender = subject or None
    pending.last_message = first_line or None
    pending.last_seen_at = utcnow()
    db.commit()
    return InboxResult("pairing", guid)


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


def poll_once(
    connect: Callable[[], imaplib.IMAP4] = _connect,
    session_factory: Callable[[], Session] = SessionLocal,
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
                results.append(process_message(db, msg))
            except Exception:
                log.exception("Could not process mail %s; leaving it unseen.", uid)
                db.rollback()
                continue
            finally:
                db.close()
            conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
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
        log.info("IMAP poll processed %d Garmin message(s).", len(results))
