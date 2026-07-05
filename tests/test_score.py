"""Unit tests for the aurora visibility scoring model (aurora.score).

Tests focus on the mathematical properties of the model:
  - boundary conditions (full overcast, clear sky, daylight, darkness)
  - monotonicity (score increases as conditions improve)
  - weight exponents (higher weight = harsher penalty)

No HTTP calls are made; all tests are purely computational.
"""

import datetime as dt

import pytest

from aurora.config import Settings
from aurora.score import FactorBundle, ScoreBreakdown, compute_score, _darkness


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_settings(**overrides) -> Settings:
    """Return a Settings object with valid Twilio stubs and optional overrides."""
    base = dict(
        twilio_account_sid="ACtest",
        twilio_auth_token="token",
        twilio_from_number="+10000000000",
    )
    base.update(overrides)
    return Settings(**base)


def _bundle(**overrides) -> FactorBundle:
    """Return a FactorBundle with ideal conditions and optional overrides.

    Ideal baseline: 100% OVATION probability, perfectly clear sky, Kp=9,
    no aerosols, high elevation, new moon, Bortle 1, dry air, flat terrain,
    full astronomical night at a high-latitude location.
    """
    base = dict(
        ovation_prob=100.0,
        kp_index=9.0,
        cloud_cover=0.0,
        aod=0.0,
        elevation_m=3000.0,
        moon_illumination=0.0,
        bortle=1.0,
        pwv_mm=5.0,
        horizon_deg=0.0,
        lat=65.0,
        lon=0.0,
        when=dt.datetime(2025, 12, 21, 0, 0, tzinfo=dt.timezone.utc),  # winter midnight
    )
    base.update(overrides)
    return FactorBundle(**base)


def _settings() -> Settings:
    return _default_settings()


# ── Darkness gate tests ────────────────────────────────────────────────────────

class TestDarkness:
    def test_full_night(self):
        """Sun well below −18°: should return 1.0."""
        # 65°N on winter solstice at midnight – sun is far below horizon
        when = dt.datetime(2025, 12, 21, 0, 0, tzinfo=dt.timezone.utc)
        assert _darkness(65.0, 0.0, when) == 1.0

    def test_midday(self):
        """Sun above horizon: should return 0.0."""
        when = dt.datetime(2025, 6, 21, 12, 0, tzinfo=dt.timezone.utc)
        assert _darkness(65.0, 0.0, when) == 0.0

    def test_twilight_between_zero_and_one(self):
        """During twilight the darkness value is strictly between 0 and 1."""
        # Astronomical twilight at 52°N on the equinox ends around 20:05 UTC;
        # 19:30 UTC falls in the nautical-to-astronomical twilight window.
        when = dt.datetime(2025, 9, 21, 19, 30, tzinfo=dt.timezone.utc)
        d = _darkness(52.0, 0.0, when)
        assert 0.0 < d < 1.0


# ── Score boundary conditions ─────────────────────────────────────────────────

class TestScoreBoundaries:
    def test_daytime_score_is_zero(self):
        """Score must be 0 when the sun is up."""
        bundle = _bundle(
            lat=52.0,
            lon=0.0,
            when=dt.datetime(2025, 6, 21, 12, 0, tzinfo=dt.timezone.utc),
        )
        result = compute_score(bundle, _settings())
        assert result.visibility_score == 0.0

    def test_total_overcast_score_is_zero(self):
        """100% cloud cover means zero transmittance → score = 0."""
        bundle = _bundle(cloud_cover=100.0)
        result = compute_score(bundle, _settings())
        assert result.visibility_score == 0.0

    def test_zero_ovation_score_is_zero(self):
        """Zero OVATION probability → no aurora → score = 0."""
        bundle = _bundle(ovation_prob=0.0)
        result = compute_score(bundle, _settings())
        assert result.visibility_score == 0.0

    def test_ideal_conditions_score_high(self):
        """Under ideal conditions the score should be well above the default 30 threshold."""
        bundle = _bundle()
        result = compute_score(bundle, _settings())
        assert result.visibility_score > 60.0

    def test_score_bounded_0_to_100(self):
        """Score must always be in [0, 100]."""
        bundle = _bundle()
        result = compute_score(bundle, _settings())
        assert 0.0 <= result.visibility_score <= 100.0


# ── Monotonicity tests ────────────────────────────────────────────────────────

class TestMonotonicity:
    def test_more_cloud_lowers_score(self):
        s = _settings()
        low = compute_score(_bundle(cloud_cover=10.0), s).visibility_score
        high = compute_score(_bundle(cloud_cover=80.0), s).visibility_score
        assert low > high

    def test_higher_ovation_raises_score(self):
        s = _settings()
        low = compute_score(_bundle(ovation_prob=10.0), s).visibility_score
        high = compute_score(_bundle(ovation_prob=90.0), s).visibility_score
        assert high > low

    def test_higher_kp_raises_score(self):
        s = _settings()
        low = compute_score(_bundle(kp_index=0.0), s).visibility_score
        high = compute_score(_bundle(kp_index=7.0), s).visibility_score
        assert high > low

    def test_higher_aod_lowers_score(self):
        s = _settings()
        low = compute_score(_bundle(aod=0.1), s).visibility_score
        high = compute_score(_bundle(aod=2.0), s).visibility_score
        assert low > high

    def test_full_moon_lowers_score(self):
        s = _settings()
        new = compute_score(_bundle(moon_illumination=0.0), s).visibility_score
        full = compute_score(_bundle(moon_illumination=1.0), s).visibility_score
        assert new > full

    def test_high_bortle_lowers_score(self):
        s = _settings()
        dark = compute_score(_bundle(bortle=1.0), s).visibility_score
        city = compute_score(_bundle(bortle=9.0), s).visibility_score
        assert dark > city


# ── Weight exponent tests ─────────────────────────────────────────────────────

class TestWeights:
    def test_higher_cloud_weight_increases_penalty(self):
        """Doubling the cloud weight should lower the score when clouds are present."""
        bundle = _bundle(cloud_cover=50.0)
        low_w = compute_score(bundle, _default_settings(weight_cloud=0.5)).visibility_score
        high_w = compute_score(bundle, _default_settings(weight_cloud=3.0)).visibility_score
        assert low_w > high_w

    def test_zero_weight_disables_factor(self):
        """Setting weight=0 for a factor should make that factor irrelevant."""
        s_with = _default_settings(weight_moon=1.0)
        s_without = _default_settings(weight_moon=0.0)
        full_moon = _bundle(moon_illumination=1.0)
        new_moon = _bundle(moon_illumination=0.0)

        # With weight=0, moon illumination should not change the score.
        score_full = compute_score(full_moon, s_without).visibility_score
        score_new = compute_score(new_moon, s_without).visibility_score
        assert abs(score_full - score_new) < 1e-6


# ── Breakdown fields ──────────────────────────────────────────────────────────

class TestBreakdown:
    def test_all_factors_in_0_1(self):
        """Every intermediate factor in ScoreBreakdown must be in [0, 1]."""
        result = compute_score(_bundle(), _settings())
        for field in ("f_dark", "f_ovation", "f_kp", "f_cloud", "f_aod",
                      "f_elev", "f_moon", "f_lp", "f_pwv", "f_horiz"):
            val = getattr(result, field)
            assert 0.0 <= val <= 1.0, f"{field}={val} out of [0, 1]"
