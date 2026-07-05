"""Unit tests for individual factor conversion functions.

Only pure / local calculations are tested here (darkness gate, moon phase,
terrain geometry).  Network-dependent factor functions are covered by
testing the scoring model inputs in test_score.py.
"""

import datetime as dt
import math

import pytest

from aurora.factors.kp import _parse_latest_kp
from aurora.factors.moon import fetch_moon
from aurora.factors.terrain import _offset_point
from aurora.factors.weather import _DEFAULT_PWV_MM, _extract_pwv
from aurora.score import _darkness


# ── SWPC Kp parsing (regression: feed is dicts, not lists) ────────────────────

class TestKpParsing:
    def test_prefers_estimated_kp(self):
        data = [
            {"time_tag": "t1", "kp_index": 2, "estimated_kp": 1.67, "kp": "2M"},
            {"time_tag": "t2", "kp_index": 3, "estimated_kp": 3.33, "kp": "3Z"},
        ]
        assert _parse_latest_kp(data) == pytest.approx(3.33)

    def test_falls_back_to_kp_index(self):
        assert _parse_latest_kp([{"time_tag": "t", "kp_index": 4}]) == pytest.approx(4.0)

    def test_skips_trailing_nulls(self):
        data = [
            {"estimated_kp": 5.0},
            {"estimated_kp": None, "kp_index": None},
        ]
        assert _parse_latest_kp(data) == pytest.approx(5.0)

    def test_empty_is_zero(self):
        assert _parse_latest_kp([]) == 0.0


# ── Weather PWV extraction (regression: missing series must not IndexError) ────

class TestPwvExtraction:
    def test_missing_series_uses_default(self):
        assert _extract_pwv({"cloud_cover": [0, 0]}, idx=1) == _DEFAULT_PWV_MM

    def test_short_series_uses_default(self):
        # idx beyond the (absent) PWV series must not raise.
        assert _extract_pwv({"total_column_integrated_water_vapour": [None]}, idx=5) == _DEFAULT_PWV_MM

    def test_present_value_used(self):
        h = {"total_column_integrated_water_vapour": [10.0, 12.5, 15.0]}
        assert _extract_pwv(h, idx=1) == pytest.approx(12.5)


# ── Moon tests ────────────────────────────────────────────────────────────────

class TestMoon:
    def test_new_moon_illumination_near_zero(self):
        """Known new moon – illumination should be close to 0."""
        # 2025-01-29 was a new moon
        when = dt.datetime(2025, 1, 29, 12, 0, tzinfo=dt.timezone.utc)
        result = fetch_moon(when)
        assert result.illumination < 0.15

    def test_full_moon_illumination_near_one(self):
        """Known full moon – illumination should be close to 1."""
        # 2025-02-12 was a full moon
        when = dt.datetime(2025, 2, 12, 12, 0, tzinfo=dt.timezone.utc)
        result = fetch_moon(when)
        assert result.illumination > 0.85

    def test_illumination_bounded(self):
        """Illumination must always be in [0, 1]."""
        for month in range(1, 13):
            when = dt.datetime(2025, month, 15, 0, 0, tzinfo=dt.timezone.utc)
            result = fetch_moon(when)
            assert 0.0 <= result.illumination <= 1.0

    def test_phase_days_bounded(self):
        """Phase days must be in [0, 29.53)."""
        when = dt.datetime(2025, 6, 15, 0, 0, tzinfo=dt.timezone.utc)
        result = fetch_moon(when)
        assert 0.0 <= result.phase_days < 29.53

    def test_without_location_effective_equals_illumination(self):
        """No lat/lon -> altitude gate skipped, effective == illuminated fraction."""
        when = dt.datetime(2025, 2, 12, 12, 0, tzinfo=dt.timezone.utc)  # full moon
        r = fetch_moon(when)
        assert r.altitude_deg is None
        assert r.effective_illumination == r.illumination

    def test_moon_below_horizon_has_no_effect(self):
        """The July 3 2026 Utah case: bright moon but below the horizon at 11pm MDT.

        05:00 UTC Jul 4 = 23:00 MDT at 41.68N,-112.71 — moon elevation ~ -5.6°.
        """
        when = dt.datetime(2026, 7, 4, 5, 0, tzinfo=dt.timezone.utc)
        r = fetch_moon(when, 41.680567, -112.707793)
        assert r.illumination > 0.8          # bright (waning gibbous)
        assert r.altitude_deg < 0            # but below the horizon
        assert r.effective_illumination == pytest.approx(0.0)  # so no effect

    def test_moon_above_horizon_contributes(self):
        """Two hours later the same moon has risen and does contribute."""
        when = dt.datetime(2026, 7, 4, 8, 0, tzinfo=dt.timezone.utc)  # ~02:00 MDT
        r = fetch_moon(when, 41.680567, -112.707793)
        assert r.altitude_deg > 0
        assert r.effective_illumination > 0.0


# ── Terrain geometry tests ────────────────────────────────────────────────────

class TestTerrainGeometry:
    def test_offset_point_north(self):
        """A northward offset should increase latitude and leave longitude unchanged."""
        new_lat, new_lon = _offset_point(50.0, 10.0, bearing_deg=0.0, distance_m=10_000)
        assert new_lat > 50.0
        assert abs(new_lon - 10.0) < 0.01

    def test_offset_point_east(self):
        """An eastward offset should increase longitude and leave latitude unchanged."""
        new_lat, new_lon = _offset_point(50.0, 10.0, bearing_deg=90.0, distance_m=10_000)
        assert new_lon > 10.0
        assert abs(new_lat - 50.0) < 0.01

    def test_offset_distance_is_consistent(self):
        """The returned point should be approximately *distance_m* from the origin."""
        lat, lon = 45.0, -93.0
        new_lat, new_lon = _offset_point(lat, lon, bearing_deg=45.0, distance_m=20_000)
        R = 6_371_000.0
        dlat = math.radians(new_lat - lat) * R
        dlon = math.radians(new_lon - lon) * R * math.cos(math.radians(lat))
        computed = math.sqrt(dlat**2 + dlon**2)
        assert abs(computed - 20_000) < 500  # within 500 m


# ── Darkness gate tests (extended) ───────────────────────────────────────────

class TestDarknessExtended:
    def test_arctic_summer_midnight_is_bright(self):
        """At 75°N in June, even midnight may not reach astronomical darkness."""
        when = dt.datetime(2025, 6, 21, 0, 0, tzinfo=dt.timezone.utc)
        d = _darkness(75.0, 0.0, when)
        # Midnight sun: darkness should be 0 or very low
        assert d < 0.3

    def test_southern_hemisphere_summer(self):
        """December midnight at 70°S is southern summer – sun barely sets."""
        when = dt.datetime(2025, 12, 21, 0, 0, tzinfo=dt.timezone.utc)
        d = _darkness(-70.0, 0.0, when)
        assert d < 0.3

    def test_equatorial_darkness(self):
        """At the equator the sun sets quickly; midnight should be fully dark."""
        when = dt.datetime(2025, 3, 21, 0, 0, tzinfo=dt.timezone.utc)
        d = _darkness(0.0, 0.0, when)
        assert d == 1.0
