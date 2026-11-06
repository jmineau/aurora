"""Outbound delivery: one ``Notifier`` per channel behind a small registry.

This is the delivery seam (see AGENTS.md).  The check loop builds the alert
text once (``messages.format_alert``: the same compact line for every channel)
and calls ``deliver()`` with the subscription's channel; it never knows which
provider is behind it.  Each notifier owns two things about its channel:

* ``normalize(recipient)``: validate and canonicalize a recipient address
  (E.164 for SMS, a GUID for inReach).  ``/subscribe`` uses this.
* ``send(recipient, subject, body)``: deliver, returning a provider message id,
  or None in collect-only mode (no credentials for that channel), in which
  case the message is logged instead.

Adding a channel (for example plain email over SMTP, with replies read by the
existing IMAP poller) means writing one class with those members and adding it
to ``_REGISTRY``; the call site, the ``Subscription`` model, ``/subscribe`` and
``/health`` pick it up from there.  ``subject`` is unused by SMS and inReach
and exists for such a channel.

Failures: ``deliver()`` never raises.  It logs at ERROR and, if ``NTFY_URL`` is
set, POSTs a one-line notice there, so a broken channel is noticed without a
retry loop.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import ClassVar, Protocol

import httpx

from aurora import messages, sms
from aurora.config import settings

log = logging.getLogger(__name__)


class NotifierError(RuntimeError):
    """A provider rejected a message or returned something we don't understand."""


class Notifier(Protocol):
    channel: ClassVar[str]

    @staticmethod
    def normalize(recipient: str) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def send(self, recipient: str, subject: str, body: str) -> str | None: ...


# ── SMS (Twilio) ──────────────────────────────────────────────────────────────

_E164 = re.compile(r"\+[1-9]\d{6,14}")


class TwilioSmsNotifier:
    """Twilio SMS: the everyday channel.  Wraps ``sms.send_sms``."""

    channel: ClassVar[str] = "sms"

    @staticmethod
    def normalize(recipient: str) -> str:
        cleaned = re.sub(r"[\s\-]", "", recipient or "")
        if not _E164.fullmatch(cleaned):
            raise ValueError(
                "Phone must be in E.164 format (e.g. +12125551234 or +447911123456)."
            )
        return cleaned

    @property
    def enabled(self) -> bool:
        return sms.settings.sms_enabled

    def send(self, recipient: str, subject: str, body: str) -> str | None:
        # SMS has no subject line; the body already carries the headline.
        return sms.send_sms(recipient, body)


# ── Garmin inReach (email-reply web form) ─────────────────────────────────────

# The reply link Garmin puts in every email an inReach sends, e.g.
#   https://inreach.garmin.com/textmessage/txtmsg?extId=<GUID>&adr=<email>
# (older mail uses explore.garmin.com).  In HTML mail the & is often &amp;.
_GUID_RE = r"[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{12}"
_REPLY_LINK = re.compile(
    r"garmin\.com/textmessage/txtmsg\?[^\s\"'<>]*?extId=(" + _GUID_RE + r")",
    re.IGNORECASE,
)
TXTMSG_PATH = "/TextMessage/TxtMsg"


def normalize_guid(value: str) -> str:
    """Canonical lowercase 8-4-4-4-12 form of an inReach extId GUID.

    Accepts 32 hex digits with or without hyphens (and optional braces).
    Raises ValueError for anything else.
    """
    raw = (value or "").strip().strip("{}")
    if not re.fullmatch(_GUID_RE, raw):
        raise ValueError("inReach recipient must be the 32-hex-digit GUID (extId) from a Garmin reply link.")
    return str(uuid.UUID(raw))


def extract_guid(text: str) -> str | None:
    """Find the extId GUID in a Garmin reply link inside *text*, if any."""
    m = _REPLY_LINK.search(text or "")
    return normalize_guid(m.group(1)) if m else None


def mask_recipient(recipient: str) -> str:
    """Last four characters only, for text that may leave this machine.

    Anyone holding an inReach GUID can message that device (and spend its
    allowance); phone numbers are personal.  ntfy topics are often public.
    """
    return "..." + recipient[-4:] if recipient else "?"


class InReachNotifier:
    """Garmin inReach via the reply form behind Garmin's email links.

    Undocumented endpoint: a form-encoded POST of ``Guid``/``ReplyAddress``/
    ``ReplyMessage`` that answers ``{"Success": true}`` on delivery.  Anything
    else is a failure, logged with the status and the start of the body so a
    change on Garmin's side is visible.  Each delivered message costs the
    owner's satellite allowance, so there is no retry loop: one retry only on a
    connection error, where the request provably never reached Garmin.
    """

    channel: ClassVar[str] = "inreach"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # Tests inject an httpx.MockTransport; production uses the default.
        self._transport = transport

    @staticmethod
    def normalize(recipient: str) -> str:
        return normalize_guid(recipient)

    @property
    def enabled(self) -> bool:
        return settings.inreach_enabled

    def send(self, recipient: str, subject: str, body: str) -> str | None:
        guid = normalize_guid(recipient)
        # Defensive: never put more than one satellite message's worth of
        # ASCII on the wire, whoever built the body.
        text = messages.to_compact_text(body)
        if not self.enabled:
            log.info("inReach disabled (collect-only); would have sent to %s:\n%s", guid, text)
            return None

        url = settings.inreach_base_url.rstrip("/") + TXTMSG_PATH
        form = {
            "Guid": guid,
            "ReplyAddress": settings.inreach_reply_address,
            "ReplyMessage": text,
        }
        headers = {"X-Requested-With": "XMLHttpRequest", "User-Agent": "aurora-alert/1.0"}
        with httpx.Client(
            timeout=settings.inreach_timeout_seconds, transport=self._transport, headers=headers
        ) as client:
            try:
                resp = client.post(url, data=form)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # The request never reached Garmin, so retrying cannot double-send.
                log.warning("inReach connect failed (%s); retrying once.", exc)
                resp = client.post(url, data=form)

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

        log.info("inReach message accepted by Garmin for %s.", guid)
        msg_id = payload.get("MessageId") or payload.get("Id")
        return str(msg_id) if msg_id else "garmin-accepted"


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type] = {
    TwilioSmsNotifier.channel: TwilioSmsNotifier,
    InReachNotifier.channel: InReachNotifier,
}
CHANNELS: tuple[str, ...] = tuple(_REGISTRY)
DEFAULT_CHANNEL = TwilioSmsNotifier.channel

_instances: dict[str, Notifier] = {}


def notifier_class(channel: str) -> type:
    try:
        return _REGISTRY[channel]
    except KeyError:
        raise ValueError(f"Unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}.") from None


def get_notifier(channel: str) -> Notifier:
    """The (shared) notifier for *channel*."""
    if channel not in _instances:
        _instances[channel] = notifier_class(channel)()
    return _instances[channel]


def normalize_recipient(channel: str, recipient: str) -> str:
    return notifier_class(channel).normalize(recipient)


def enabled_channels() -> dict[str, bool]:
    """{channel: really sends?} for /health.  False means collect-only."""
    return {ch: get_notifier(ch).enabled for ch in CHANNELS}


# ── Delivery with failure reporting ───────────────────────────────────────────

@dataclass(frozen=True)
class Delivery:
    ok: bool
    message_id: str | None = None
    error: str | None = None


def deliver(channel: str, recipient: str, subject: str, body: str) -> Delivery:
    """Send through *channel*'s notifier.  Never raises; failures are reported.

    ``ok`` is True for a real send and for a collect-only log (message_id None).
    """
    notifier = get_notifier(channel)
    try:
        return Delivery(ok=True, message_id=notifier.send(recipient, subject, body))
    except Exception as exc:  # noqa: BLE001 - any provider failure is reported the same way
        who = mask_recipient(recipient)
        log.error("Alert via %s to %s failed: %s", channel, recipient, exc, exc_info=True)
        notify_failure(f"aurora: {channel} alert to {who} failed: {exc}. Check the server log.")
        return Delivery(ok=False, error=str(exc))


def notify_failure(line: str) -> None:
    """POST a one-line notice to NTFY_URL, if set.  Best effort, never raises."""
    if not settings.ntfy_url:
        return
    try:
        httpx.post(settings.ntfy_url, content=line.encode("utf-8"), timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        log.error("ntfy failure notice could not be sent: %s", exc)
