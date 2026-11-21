"""Email alert channel: plain SMTP, free with any mailbox (e.g. a Gmail app password).

The same compact alert line every channel gets goes out as the body of a short
email; ``Reply-To`` is the aurora mailbox, so a Y/N reply lands where the IMAP
poller (``inbox.py``) reads it and becomes an ``Observation``
(``source="email_reply"``).  With no ``SMTP_HOST`` the notifier is collect-only:
``send`` logs "would have sent" and returns None, like the other channels.
"""

from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from typing import ClassVar

from sqlalchemy.orm import Session

from aurora.config import settings
from aurora.notify_base import NotifierError

log = logging.getLogger(__name__)

# Deliberately plain: one address, no display name, no quoting.  Whitespace and
# control characters are excluded, which also rules out header injection.
_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def normalize_address(value: str) -> str:
    """Lower-cased email address, or ValueError."""
    cleaned = (value or "").strip().lower()
    if not _ADDRESS.fullmatch(cleaned):
        raise ValueError("Email recipient must be a plain address like name@example.com.")
    return cleaned


class EmailNotifier:
    """SMTP email: the free channel while SMS (10DLC) is still being sorted out."""

    channel: ClassVar[str] = "email"

    @staticmethod
    def normalize(recipient: str) -> str:
        return normalize_address(recipient)

    def register_recipient(self, db: Session, recipient: str) -> str:
        return recipient

    @property
    def enabled(self) -> bool:
        return settings.email_enabled

    def _connect(self) -> smtplib.SMTP:
        timeout = settings.smtp_timeout_seconds
        if settings.smtp_port == 465:
            return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
        conn = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        conn.starttls()
        return conn

    def send(self, recipient: str, subject: str, body: str) -> str | None:
        if not self.enabled:
            log.info("Email not configured – collect-only. Would have sent to %s: %s", recipient, body)
            return None
        sender = settings.smtp_from or settings.smtp_user
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Reply-To"] = sender
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
        msg.set_content(body)
        try:
            with self._connect() as conn:
                conn.login(settings.smtp_user, settings.smtp_password)
                conn.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifierError(f"SMTP send failed: {exc}") from exc
        return msg["Message-ID"]
