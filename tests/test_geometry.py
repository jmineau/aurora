"""Tests for the aurora viewing geometry (aurora.geometry).

Pure math — the scientific core of the poleward-sampling model.  Also covers the
OVATION poleward-profile sampler using a synthetic interpolator (no network).
"""

import math

import numpy as np
import pytest
from scipy.interpolate import RegularGridInterpolator

from aurora import geometry
from aurora.factors.ovation import sample_poleward_profile


class TestElevationAngle:
    def test_overhead_is_90(self):
        assert geometry.elevation_angle(0.0) == pytest.approx(90.0)

    def test_decreases_with_distance(self):
        angles = [geometry.elevation_angle(d) for d in (0, 100e3, 300e3, 600e3, 900e3)]
        assert all(a > b for a, b in zip(angles, angles[1:]))

    def test_zero_at_geometric_horizon(self):
        d_max = geometry.max_visible_distance()
        assert geometry.elevation_angle(d_max) == pytest.approx(0.0, abs=1e-6)

    def test_negative_beyond_horizon(self):
        d_max = geometry.max_visible_distance()
        assert geometry.elevation_angle(d_max * 1.2) < 0.0

    def test_higher_emission_visible_farther(self):
        assert geometry.max_visible_distance(400e3) > geometry.max_visible_distance(110e3)

    def test_horizon_distance_magnitude(self):
        # ~1180 km for a 110 km emission height.
        assert 1_100e3 < geometry.max_visible_distance(110e3) < 1_250e3


class TestPoleward:
    def test_bearing_by_hemisphere(self):
        assert geometry.poleward_bearing(45.0) == 0.0
        assert geometry.poleward_bearing(-45.0) == 180.0

    def test_north_increases_latitude(self):
        lat2, lon2 = geometry.destination_point(45.0, -110.0, 0.0, 111_000)
        assert lat2 == pytest.approx(46.0, abs=0.05)   # ~1° per 111 km
        assert lon2 == pytest.approx(-110.0, abs=1e-6)  # due north keeps longitude

    def test_south_decreases_latitude(self):
        lat2, _ = geometry.destination_point(-45.0, 0.0, 180.0, 111_000)
        assert lat2 == pytest.approx(-46.0, abs=0.05)

    def test_destination_distance_roundtrip(self):
        lat1, lon1 = 40.0, -111.0
        lat2, lon2 = geometry.destination_point(lat1, lon1, 0.0, 500_000)
        # Haversine back to the origin should recover ~500 km.
        p1, p2 = map(math.radians, (lat1, lat2))
        dphi = p2 - p1
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
        d = 2 * geometry.R_EARTH_M * math.asin(math.sqrt(a))
        assert d == pytest.approx(500_000, rel=1e-3)

    def test_initial_bearing_cardinals(self):
        assert geometry.initial_bearing(0, 0, 10, 0) == pytest.approx(0.0)      # due N
        assert geometry.initial_bearing(0, 0, 0, 10) == pytest.approx(90.0)     # due E
        assert geometry.initial_bearing(0, 0, 0, -10) == pytest.approx(270.0)   # due W

    def test_geomagnetic_bearing_western_us_is_east_of_north(self):
        # From Utah the geomagnetic pole (~-72.7 lon) is east of north.
        b = geometry.geomagnetic_pole_bearing(41.68, -112.71)
        assert 0.0 < b < 45.0

    def test_geomagnetic_bearing_east_of_pole_is_west_of_north(self):
        # London is east of the pole's longitude, so the bearing tips west of north.
        b = geometry.geomagnetic_pole_bearing(51.5, -0.13)
        assert 315.0 < b < 360.0

    def test_geomagnetic_bearing_on_pole_meridian_is_due_north(self):
        # Same longitude as the north geomagnetic pole -> straight north.
        b = geometry.geomagnetic_pole_bearing(30.0, geometry.NORTH_GEOMAGNETIC_POLE[1])
        assert b == pytest.approx(0.0, abs=0.5)

    def test_geomagnetic_bearing_southern_hemisphere_points_south(self):
        b = geometry.geomagnetic_pole_bearing(-40.0, geometry.SOUTH_GEOMAGNETIC_POLE[1])
        assert b == pytest.approx(180.0, abs=0.5)

    def test_sample_distances_span_horizon(self):
        dists = geometry.sample_distances(110e3, step_m=100e3)
        assert dists[0] == 0.0
        assert dists[-1] == pytest.approx(geometry.max_visible_distance(110e3))
        assert all(b >= a for a, b in zip(dists, dists[1:]))


class TestVisibleAurora:
    def test_empty_profile(self):
        assert geometry.visible_aurora([]) == (0.0, None)

    def test_overhead_aurora_is_visible_high(self):
        prob, elev = geometry.visible_aurora([(0.0, 80.0)])
        assert prob == 80.0
        assert elev == pytest.approx(90.0)

    def test_far_aurora_below_horizon_excluded(self):
        # A strong band well beyond the geometric horizon contributes nothing.
        far = geometry.max_visible_distance() * 1.3
        prob, elev = geometry.visible_aurora([(far, 90.0)])
        assert prob == 0.0
        assert elev is None

    def test_picks_highest_visible_probability(self):
        profile = [(0.0, 5.0), (300e3, 70.0), (600e3, 40.0)]
        prob, elev = geometry.visible_aurora(profile)
        assert prob == 70.0
        assert 0.0 < elev < 90.0

    def test_terrain_horizon_gates_low_aurora(self):
        # Aurora at ~600 km sits low; a 10° ridge to the north blocks it.
        profile = [(600e3, 90.0)]
        low_elev = geometry.elevation_angle(600e3)
        assert low_elev < 10.0
        prob, _ = geometry.visible_aurora(profile, horizon_deg=10.0)
        assert prob == 0.0
        # With a flat horizon the same aurora is visible.
        prob_flat, _ = geometry.visible_aurora(profile, horizon_deg=0.0)
        assert prob_flat == 90.0


class TestLineOfSightCloud:
    def test_overhead_at_zenith(self):
        # Aurora overhead -> overhead cloud only.
        assert geometry.line_of_sight_cloud(80.0, 10.0, elevation_deg=90.0) == pytest.approx(80.0)

    def test_poleward_at_horizon(self):
        # Aurora on the horizon -> poleward cloud only.
        assert geometry.line_of_sight_cloud(80.0, 10.0, elevation_deg=0.0) == pytest.approx(10.0)

    def test_unknown_elevation_leans_poleward(self):
        # Default low elevation weights the poleward sky heavily.
        val = geometry.line_of_sight_cloud(100.0, 0.0, elevation_deg=None)
        assert val < 20.0

    def test_monotonic_in_elevation(self):
        # overhead=100, poleward=0: higher aurora -> more overhead cloud counts.
        vals = [geometry.line_of_sight_cloud(100.0, 0.0, e) for e in (0, 15, 45, 90)]
        assert all(b >= a for a, b in zip(vals, vals[1:]))
        assert vals[0] == pytest.approx(0.0) and vals[-1] == pytest.approx(100.0)


class TestPolewardSampling:
    """The OVATION sampler walking poleward across a synthetic oval."""

    def _interp(self):
        # A probability band centred at 67°N (a poleward oval), all longitudes.
        lons = np.arange(0, 360, dtype=float)
        lats = np.arange(-90, 91, dtype=float)
        grid = np.zeros((360, 181))
        for i, la in enumerate(lats):
            grid[:, i] = 90.0 * math.exp(-((la - 67.0) ** 2) / (2 * 2.5**2))
        return RegularGridInterpolator((lons, lats), grid, bounds_error=False, fill_value=0.0)

    def test_profile_peaks_near_the_oval(self):
        interp = self._interp()
        # Observer at 55°N: the oval (67°N) is ~1330 km poleward.
        dists = geometry.sample_distances()
        profile = sample_poleward_profile(interp, 55.0, -110.0, dists)
        probs = [p for _, p in profile]
        # Overhead (55°N) is off the band -> low; a poleward sample -> high.
        assert probs[0] < 20.0
        assert max(probs) > 60.0
        # The oval at 67°N is near/just beyond the 110 km horizon (~1175 km) from
        # 55°N, so the peak visible sample sits far out, at/under that horizon.
        peak_d = max(profile, key=lambda dp: dp[1])[0]
        assert 900e3 < peak_d <= geometry.max_visible_distance()

    def test_overhead_observer_sees_it_overhead(self):
        interp = self._interp()
        dists = geometry.sample_distances()
        profile = sample_poleward_profile(interp, 67.0, 20.0, dists)
        assert profile[0][1] > 80.0  # standing under the oval
