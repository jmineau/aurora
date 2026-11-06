"""Application settings loaded from a .env file.

All factor weights are exponents in the weighted-product visibility model.
A weight of 1.0 applies the factor linearly; higher values penalise that
factor more harshly; 0.0 disables it entirely.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Twilio ────────────────────────────────────────────────────────────────
    # Optional: leave all three blank to run collect-only (snapshots are logged
    # every cycle, alerts are logged instead of texted).  Set all three or none.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""  # E.164, e.g. +12125551234

    # ── Garmin inReach (optional) ─────────────────────────────────────────────
    # Alerts reach an inReach through Garmin's email-reply web form (the link in
    # any email an inReach sends).  INREACH_REPLY_ADDRESS is the aurora mailbox
    # the device messages; leave it blank to log inReach alerts instead of sending.
    # The endpoint is undocumented: see docs/roadmap.md (risks).
    inreach_reply_address: str = ""
    inreach_base_url: str = "https://inreach.garmin.com"
    inreach_timeout_seconds: float = 20.0

    # ── Mailbox (optional, IMAP) ──────────────────────────────────────────────
    # Polled for inReach messages: Y/N replies become observations, anything else
    # is a pairing request (the GUID is logged and listed at GET /pairings).
    # Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD together, or none of them.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 120

    # ── Failure notices (optional) ────────────────────────────────────────────
    # ntfy topic URL (e.g. https://ntfy.sh/<secret-topic>).  When an alert fails
    # to send, a one-line notice is POSTed here.
    ntfy_url: str = ""

    # ── Scheduling ────────────────────────────────────────────────────────────
    check_interval_minutes: int = 30
    # Minimum gap between SMS alerts for the same subscription (anti-spam).
    alert_cooldown_hours: int = 6

    # Validate the X-Twilio-Signature on the inbound-SMS webhook.  The endpoint
    # writes to the DB from a public URL, so this MUST be True in production;
    # left False by default so local/testing works without a signed request.
    twilio_validate_signature: bool = False

    # ── Server ────────────────────────────────────────────────────────────────
    # Bind address for `aurora-server`.  Change the port if 8000 is taken
    # (e.g. by Pi-hole's web UI on a home server).
    host: str = "0.0.0.0"
    port: int = 8000

    # ── Persistence ───────────────────────────────────────────────────────────
    database_url: str = "sqlite:///aurora.db"

    # ── External API keys ─────────────────────────────────────────────────────
    # Get a free key at https://portal.opentopography.org/requestApiKey
    opentopography_api_key: str = ""

    # ── Viewing geometry ──────────────────────────────────────────────────────
    # Aurora emission altitude used to project the poleward oval onto the sky.
    # 110 km ≈ green line (visual); raise toward 200–400 km to weight red aurora.
    aurora_emission_km: float = 110.0

    # ── Factor weights (exponents in the weighted-product model) ──────────────
    # Aurora source (OVATION probability)
    weight_ovation: float = 1.0
    # Geomagnetic activity (Kp index) – complements OVATION
    weight_kp: float = 0.5
    # Cloud cover – raised weight penalises cloud more harshly
    weight_cloud: float = 1.5
    # Aerosol optical depth
    weight_aod: float = 1.0
    # Site elevation (higher = less column, less likely to be below cloud deck)
    weight_elev: float = 0.3
    # Lunar illumination (full moon raises sky background)
    weight_moon: float = 0.5
    # Light pollution (Bortle class)
    weight_lp: float = 0.5
    # Precipitable water vapour (near-IR extinction)
    weight_pwv: float = 0.3
    # Local horizon elevation angle (topographic obstruction)
    weight_horiz: float = 0.5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def sms_enabled(self) -> bool:
        """True when Twilio is configured; False means collect-only mode."""
        return bool(self.twilio_account_sid)

    @property
    def inreach_enabled(self) -> bool:
        """True when inReach alerts are actually POSTed to Garmin."""
        return bool(self.inreach_reply_address)

    @property
    def imap_enabled(self) -> bool:
        """True when the mailbox poller (pairing + inReach replies) should run."""
        return bool(self.imap_host)

    @model_validator(mode="after")
    def _imap_all_or_none(self) -> "Settings":
        creds = [self.imap_host, self.imap_user, self.imap_password]
        if any(creds) and not all(creds):
            raise ValueError(
                "Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD together, or none to disable the mailbox."
            )
        return self

    @model_validator(mode="after")
    def _twilio_all_or_none(self) -> "Settings":
        # A half-filled Twilio config is almost certainly a mistake; fail loudly
        # rather than silently dropping into collect-only mode.
        creds = [self.twilio_account_sid, self.twilio_auth_token, self.twilio_from_number]
        if any(creds) and not all(creds):
            raise ValueError(
                "Set all three TWILIO_* variables to send SMS, or none to run collect-only."
            )
        return self


settings = Settings()
