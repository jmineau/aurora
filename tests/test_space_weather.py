"""Tests for the Kp-driven auroral-oval reconstruction (aurora.space_weather).

Offline: the oval model and the GFZ Kp response parser are pure. The archive
fetch itself hits the network and is covered by a manual run.
"""

import datetime as dt

import pytest

from aurora import geometry
from aurora.space_weather import (
    _select_kp,
    modeled_poleward_profile,
    oval_probability,
)


class TestOvalProbability:
    def test_higher_kp_pushes_oval_equatorward(self):
        # At a fixed mid-latitude, more Kp -> higher presence probability.
        low = oval_probability(50.0, kp=1.0)
        high = oval_probability(50.0, kp=7.0)
        assert high > low

    def test_higher_maglat_more_probable(self):
        assert oval_probability(60.0, kp=3.0) > oval_probability(45.0, kp=3.0)

    def test_bounded_0_100(self):
        for mlat in (20, 45, 60, 80):
            for kp in (0, 3, 6, 9):
                assert 0.0 <= oval_probability(mlat, kp) <= 100.0

    def test_at_boundary_is_about_half(self):
        # Boundary at Kp 6 is 66 - 2.5*6 = 51 deg maglat -> ~50%.
        assert oval_probability(51.0, kp=6.0) == pytest.approx(50.0, abs=1.0)


class TestModeledProfile:
    def test_storm_beats_quiet_for_midlat_site(self):
        dists = geometry.sample_distances()
        quiet = modeled_poleward_profile(41.68, -112.71, kp=1.0, distances=dists)
        storm = modeled_poleward_profile(41.68, -112.71, kp=7.0, distances=dists)
        # The best (poleward) probability should be much higher during the storm.
        assert max(p for _, p in storm) > max(p for _, p in quiet)

    def test_probability_rises_poleward(self):
        dists = geometry.sample_distances()
        prof = modeled_poleward_profile(41.68, -112.71, kp=5.0, distances=dists)
        assert prof[-1][1] > prof[0][1]  # farther poleward -> higher maglat -> more likely


class TestSelectKp:
    def _payload(self):
        return {
            "datetime": [
                "2026-07-04T00:00:00Z", "2026-07-04T03:00:00Z",
                "2026-07-04T06:00:00Z", "2026-07-04T09:00:00Z",
            ],
            "Kp": [5.667, 7.333, 6.333, 4.333],
        }

    def test_picks_interval_containing_time(self):
        # 05:00 falls in the 03:00-06:00 interval -> 7.333.
        when = dt.datetime(2026, 7, 4, 5, 0)
        assert _select_kp(self._payload(), when) == pytest.approx(7.333)

    def test_before_first_falls_back_to_first(self):
        when = dt.datetime(2026, 7, 3, 23, 0)
        assert _select_kp(self._payload(), when) == pytest.approx(5.667)

    def test_empty_payload_returns_none(self):
        assert _select_kp({"datetime": [], "Kp": []}, dt.datetime(2026, 7, 4, 5, 0)) is None
