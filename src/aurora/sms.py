"""Twilio SMS wrapper.

The client is created lazily on first use so the server starts even if
Twilio credentials haven't been validated yet.
"""

from twilio.rest import Client

from aurora.config import settings

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def send_sms(to: str, message: str) -> str:
    """Send an SMS via Twilio and return the Twilio message SID."""
    client = _get_client()
    msg = client.messages.create(
        body=message,
        from_=settings.twilio_from_number,
        to=to,
    )
    return msg.sid
