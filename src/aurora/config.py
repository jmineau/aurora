"""Application settings loaded from a .env file.

All factor weights are exponents in the weighted-product visibility model.
A weight of 1.0 applies the factor linearly; higher values penalise that
factor more harshly; 0.0 disables it entirely.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Twilio ────────────────────────────────────────────────────────────────
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str  # E.164, e.g. +12125551234

    # ── Scheduling ────────────────────────────────────────────────────────────
    check_interval_minutes: int = 30
    # Minimum gap between SMS alerts for the same subscription (anti-spam).
    alert_cooldown_hours: int = 6

    # Validate the X-Twilio-Signature on the inbound-SMS webhook.  The endpoint
    # writes to the DB from a public URL, so this MUST be True in production;
    # left False by default so local/testing works without a signed request.
    twilio_validate_signature: bool = False

    # ── Persistence ───────────────────────────────────────────────────────────
    database_url: str = "sqlite:///aurora.db"

    # ── External API keys ─────────────────────────────────────────────────────
    # Get a free key at https://portal.opentopography.org/requestApiKey
    opentopography_api_key: str = ""

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


settings = Settings()
