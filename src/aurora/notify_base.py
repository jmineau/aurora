"""The Notifier interface, shared by ``notify.py`` (registry) and each channel module."""

from __future__ import annotations

from typing import ClassVar, Protocol

from sqlalchemy.orm import Session


class NotifierError(RuntimeError):
    """A provider rejected a message or returned something we don't understand."""


class Notifier(Protocol):
    """One delivery channel.

    * ``normalize(recipient)``: validate/canonicalize what a user typed at
      ``/subscribe``; raise ValueError if it can't be this channel's address.
    * ``register_recipient(db, recipient)``: turn the normalized value into the
      stored ``Subscription.recipient`` (identity for SMS; the inReach
      conversation key, resolving it if needed).  ValueError → HTTP 422.
    * ``enabled``: False means collect-only (``send`` logs, returns None).
    * ``send(recipient, subject, body)``: deliver; return a provider message id,
      or None in collect-only mode.  Raise on failure.
    """

    channel: ClassVar[str]

    @staticmethod
    def normalize(recipient: str) -> str: ...

    def register_recipient(self, db: Session, recipient: str) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def send(self, recipient: str, subject: str, body: str) -> str | None: ...
