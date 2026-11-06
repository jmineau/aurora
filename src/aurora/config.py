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
