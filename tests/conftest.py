"""Pytest configuration: set required env vars before any aurora module is imported.

The Settings class validates Twilio credentials at instantiation.  In tests
we supply stub values here so the import succeeds without a real .env file.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_auth_token")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+10000000000")
