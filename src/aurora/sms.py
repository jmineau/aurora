"""Twilio SMS wrapper.

The client is created lazily on first use so the server starts even if
Twilio credentials haven't been validated yet.
"""

from twilio.request_validator import RequestValidator
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


def validate_twilio_signature(url: str, params: dict, signature: str) -> bool:
    """Verify an inbound webhook really came from Twilio.

    *url* is the full public URL Twilio POSTed to, *params* the form fields, and
    *signature* the ``X-Twilio-Signature`` header.  Returns True when validation
    is disabled (``TWILIO_VALIDATE_SIGNATURE=false``) so local testing works.
    """
    if not settings.twilio_validate_signature:
        return True
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.validate(url, params, signature or "")
