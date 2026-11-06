"""Pytest configuration: set required env vars before any aurora module is imported.

The Settings class validates Twilio credentials at instantiation.  In tests
we supply stub values here so the import succeeds without a real .env file.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_auth_token")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+10000000000")
# Never let a test reach Garmin's real endpoint, and keep inReach/IMAP/ntfy off
# unless a test turns them on with its own Settings.
os.environ["INREACH_BASE_URL"] = "http://127.0.0.1:9"
for _var in ("INREACH_REPLY_ADDRESS", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "NTFY_URL"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@pytest.fixture()
def memory_db():
    """A fresh in-memory SQLite session with every table created."""
    from aurora.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def api(memory_db, monkeypatch):
    """A TestClient on the app, bound to memory_db, with no scheduler or geocode cache."""
    from fastapi.testclient import TestClient

    import aurora.main as main

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "init_cache", lambda: None)
    monkeypatch.setattr(main.scheduler, "start", lambda: None)
    monkeypatch.setattr(main.scheduler, "shutdown", lambda **_: None)

    def _db():
        yield memory_db

    main.app.dependency_overrides[main.get_db] = _db
    # /sms/inbound opens its own session via get_db() rather than Depends.
    monkeypatch.setattr(main, "get_db", _db)
    try:
        with TestClient(main.app) as client:
            yield client
    finally:
        main.app.dependency_overrides.clear()
