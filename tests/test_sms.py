"""Tests for optional Twilio config (collect-only mode) and the SMS wrapper.

No HTTP calls are made; the Twilio client is never constructed.
"""

import pytest
from pydantic import ValidationError

from aurora import sms
from aurora.config import Settings

TWILIO_VARS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")


@pytest.fixture
def no_twilio_env(monkeypatch):
    """Remove the conftest Twilio stubs so Settings sees no credentials."""
    for var in TWILIO_VARS:
        monkeypatch.delenv(var, raising=False)


def test_no_twilio_is_collect_only(no_twilio_env):
    s = Settings(_env_file=None)
    assert s.sms_enabled is False


def test_full_twilio_enables_sms(no_twilio_env):
    s = Settings(
        _env_file=None,
        twilio_account_sid="ACtest",
        twilio_auth_token="token",
        twilio_from_number="+10000000000",
    )
    assert s.sms_enabled is True


def test_partial_twilio_is_rejected(no_twilio_env):
    with pytest.raises(ValidationError, match="TWILIO_"):
        Settings(_env_file=None, twilio_account_sid="ACtest")


def test_send_sms_disabled_does_not_touch_twilio(no_twilio_env, monkeypatch):
    monkeypatch.setattr(sms, "settings", Settings(_env_file=None))

    def _fail():
        raise AssertionError("Twilio client must not be created in collect-only mode")

    monkeypatch.setattr(sms, "_get_client", _fail)
    assert sms.send_sms("+10000000000", "test") is None
