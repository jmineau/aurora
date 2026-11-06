"""Outbound delivery: one ``Notifier`` per channel behind a small registry.

This is the delivery seam (see AGENTS.md).  The check loop builds the alert
text once (``messages.format_alert``: the same compact line for every channel)
and calls ``deliver()`` with the subscription's channel; it never knows which
provider is behind it.  Each notifier (interface: ``notify_base.Notifier``)
owns these things about its channel:

* ``normalize(recipient)`` + ``register_recipient(db, recipient)``: turn what
  ``/subscribe`` was given into the stored recipient (an E.164 phone for SMS;
  for inReach, a reply-link token resolved to its conversation key).
* ``send(recipient, subject, body)``: deliver, returning a provider message id,
  or None in collect-only mode (no credentials for that channel), in which
  case the message is logged instead.

Adding a channel (for example plain email over SMTP, with replies read by the
existing IMAP poller) means writing one class with those members (inReach
lives in its own module, ``inreach.py``) and adding it to ``_REGISTRY``; the
call site, the ``Subscription`` model, ``/subscribe`` and ``/health`` pick it
up from there.  ``subject`` is unused by SMS and inReach
and exists for such a channel.

Failures: ``deliver()`` never raises.  It logs at ERROR and, if ``NTFY_URL`` is
set, POSTs a one-line notice there, so a broken channel is noticed without a
retry loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import ClassVar

import httpx
from sqlalchemy.orm import Session

from aurora import sms
from aurora.config import settings
from aurora.inreach import InReachNotifier, extract_token, normalize_token
from aurora.notify_base import Notifier, NotifierError

__all__ = [
    "CHANNELS", "DEFAULT_CHANNEL", "Delivery", "InReachNotifier", "Notifier", "NotifierError",
    "TwilioSmsNotifier", "deliver", "enabled_channels", "extract_token", "get_notifier",
    "mask_recipient", "normalize_recipient", "normalize_token", "notifier_class", "notify_failure",
]

log = logging.getLogger(__name__)


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

    def register_recipient(self, db: Session, recipient: str) -> str:
        return recipient

    @property
    def enabled(self) -> bool:
        return sms.settings.sms_enabled

    def send(self, recipient: str, subject: str, body: str) -> str | None:
        # SMS has no subject line; the body already carries the headline.
        return sms.send_sms(recipient, body)


# ── Helpers ───────────────────────────────────────────────────────────────────

def mask_recipient(recipient: str) -> str:
    """Last four characters only, for text that may leave this machine.

    inReach tokens and conversation ids let anyone message the device (and
    spend its allowance); phone numbers are personal.  ntfy topics are often
    public.
    """
    return "..." + recipient[-4:] if recipient else "?"


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
