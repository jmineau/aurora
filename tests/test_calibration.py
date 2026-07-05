"""Tests for the calibration fit (aurora.calibration).

Offline: the fit/metrics are exercised on synthetic data with a known ground
truth, and dataset assembly on an in-memory DB.
"""

import datetime as dt

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aurora.calibration import (
    Calibration,
    assemble_dataset,
    calibrate,
    cross_val_metrics,
    evaluate,
    features_from_snapshot,
    fit,
    hand_weight_prior,
    predict_proba,
    roc_auc,
)
from aurora.calibration import apply_calibration, probability_from_bundle
from aurora.config import Settings
from aurora.db import AlertLog, Base, Observation, Subscription
from aurora.score import FACTOR_NAMES, FactorBundle, compute_score


@pytest.fixture()
def db():
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


def _rng():
    return np.random.default_rng(42)


# ── Fit behaviour ──────────────────────────────────────────────────────────────

class TestFit:
    def test_zero_data_returns_prior(self):
        """With no labels the MAP solution sits exactly at (0, prior)."""
        prior = np.array([1.0, 0.5, 1.5, 1.0, 0.3, 0.5, 0.5, 0.3, 0.5])
        X = np.empty((0, len(prior)))
        y = np.empty((0,))
        b0, b = fit(X, y, prior=prior, l2=1.0)
        assert b0 == pytest.approx(0.0, abs=1e-6)
        np.testing.assert_allclose(b, prior, atol=1e-6)

    def test_recovers_signal_from_synthetic_labels(self):
        """A strong true coefficient should be recovered with the right sign."""
        rng = _rng()
        n, p = 400, len(FACTOR_NAMES)
        X = rng.normal(size=(n, p))
        true_b0 = -0.5
        true_beta = np.zeros(p)
        true_beta[0] = 3.0   # ovation strongly positive
        true_beta[2] = -2.0  # cloud strongly negative
        probs = 1.0 / (1.0 + np.exp(-(true_b0 + X @ true_beta)))
        y = (rng.uniform(size=n) < probs).astype(float)

        # Weak prior so the data dominates.
        prior = np.zeros(p)
        b0, b = fit(X, y, prior=prior, l2=0.01)

        assert b[0] > 1.0            # recovered positive ovation effect
        assert b[2] < -0.5           # recovered negative cloud effect
        auc = roc_auc(y, predict_proba(X, b0, b))
        assert auc > 0.85

    def test_strong_prior_shrinks_toward_hand_weights(self):
        """A large l2 keeps coefficients near the prior even with some data."""
        rng = _rng()
        prior = np.full(len(FACTOR_NAMES), 0.7)
        X = rng.normal(size=(30, len(FACTOR_NAMES)))
        y = (rng.uniform(size=30) < 0.5).astype(float)
        _, b = fit(X, y, prior=prior, l2=1000.0)
        np.testing.assert_allclose(b, prior, atol=0.05)


# ── Metrics ────────────────────────────────────────────────────────────────────

class TestMetrics:
    def test_auc_perfect_separation(self):
        y = np.array([0, 0, 1, 1.0])
        p = np.array([0.1, 0.2, 0.8, 0.9])
        assert roc_auc(y, p) == pytest.approx(1.0)

    def test_auc_reversed_is_zero(self):
        y = np.array([0, 0, 1, 1.0])
        p = np.array([0.9, 0.8, 0.2, 0.1])
        assert roc_auc(y, p) == pytest.approx(0.0)

    def test_auc_single_class_is_nan(self):
        assert np.isnan(roc_auc(np.array([1, 1.0]), np.array([0.5, 0.6])))

    def test_evaluate_perfect_predictions(self):
        y = np.array([0, 1, 0, 1.0])
        p = np.array([0.0, 1.0, 0.0, 1.0])
        m = evaluate(y, p)
        assert m["brier"] == pytest.approx(0.0)
        assert m["roc_auc"] == pytest.approx(1.0)
        assert m["precision"] == pytest.approx(1.0)
        assert m["recall"] == pytest.approx(1.0)
        assert m["confusion"] == {"tp": 2.0, "fp": 0.0, "fn": 0.0, "tn": 2.0}

    def test_reliability_bins_sum_to_n(self):
        rng = _rng()
        y = (rng.uniform(size=100) < 0.5).astype(float)
        p = rng.uniform(size=100)
        rel = evaluate(y, p)["reliability"]
        assert sum(r["count"] for r in rel) == 100

    def test_cross_val_runs(self):
        rng = _rng()
        p = len(FACTOR_NAMES)
        X = rng.normal(size=(40, p))
        beta = np.zeros(p); beta[0] = 2.0
        y = (rng.uniform(size=40) < 1 / (1 + np.exp(-X @ beta))).astype(float)
        cv = cross_val_metrics(X, y, prior=np.zeros(p), l2=0.1, k=5)
        assert 0.0 <= cv["brier"] <= 1.0
        assert cv["roc_auc"] > 0.5


# ── Feature construction & DB assembly ─────────────────────────────────────────

def _snapshot(**overrides) -> AlertLog:
    base = dict(
        ovation_prob=50.0, kp_index=5.0, cloud_cover=0.0, aod=0.05,
        elevation_m=1000.0, horizon_deg=0.0, bortle=2.0,
        moon_illumination=0.0, pwv_mm=5.0, visibility_score=40.0, alerted=True,
    )
    base.update(overrides)
    return AlertLog(**base)


class TestFeatures:
    def test_feature_vector_length_and_order(self):
        x = features_from_snapshot(_snapshot())
        assert x.shape == (len(FACTOR_NAMES),)

    def test_clear_sky_cloud_feature_is_zero_log(self):
        """Clear sky: f_cloud=1 so log(f_cloud)=0; overcast is strongly negative."""
        clear = features_from_snapshot(_snapshot(cloud_cover=0.0))
        overcast = features_from_snapshot(_snapshot(cloud_cover=100.0))
        i = FACTOR_NAMES.index("cloud")
        assert clear[i] == pytest.approx(0.0, abs=1e-9)
        assert overcast[i] < -5.0  # floored, strongly negative

    def test_hand_weight_prior_order(self):
        prior = hand_weight_prior()
        assert prior.shape == (len(FACTOR_NAMES),)
        # Cloud weight (1.5) is the largest by default.
        assert prior[FACTOR_NAMES.index("cloud")] == pytest.approx(1.5)


class TestCalibratedScoring:
    def _cal(self, intercept=0.0, **coef):
        base = {n: 0.0 for n in FACTOR_NAMES}
        base.update(coef)
        return Calibration(
            intercept=intercept, coefficients=base, prior=base, l2=1.0,
            n_samples=20, n_positive=10,
        )

    def _bundle(self, **overrides):
        base = dict(
            ovation_prob=50.0, kp_index=5.0, cloud_cover=0.0, aod=0.05,
            elevation_m=1000.0, moon_illumination=0.1, bortle=3.0, pwv_mm=5.0,
            horizon_deg=0.0, lat=65.0, lon=0.0,
            when=dt.datetime(2025, 12, 21, 0, 0, tzinfo=dt.timezone.utc),  # night at 65N
        )
        base.update(overrides)
        return FactorBundle(**base)

    def test_zero_model_gives_half(self):
        # intercept 0, all coefficients 0 -> sigmoid(0) = 0.5 for any bundle.
        assert probability_from_bundle(self._bundle(), self._cal()) == pytest.approx(0.5)

    def test_clearer_sky_more_probable(self):
        cal = self._cal(cloud=1.0)  # positive weight on log(f_cloud); clear -> higher
        clear = probability_from_bundle(self._bundle(cloud_cover=0.0), cal)
        overcast = probability_from_bundle(self._bundle(cloud_cover=95.0), cal)
        assert clear > overcast

    def test_apply_at_night_uses_probability(self):
        cal = self._cal(intercept=5.0)  # p ~ 0.99
        s = Settings(twilio_account_sid="AC", twilio_auth_token="t", twilio_from_number="+10000000000")
        bundle = self._bundle()
        breakdown = compute_score(bundle, s)
        heuristic = breakdown.visibility_score
        apply_calibration(breakdown, bundle, cal)
        assert breakdown.is_calibrated
        assert breakdown.probability > 0.9
        assert breakdown.heuristic_score == heuristic
        assert breakdown.visibility_score == pytest.approx(100.0 * breakdown.probability, abs=0.5)

    def test_apply_in_daylight_is_gated_to_zero(self):
        cal = self._cal(intercept=5.0)  # would be ~99% at night
        s = Settings(twilio_account_sid="AC", twilio_auth_token="t", twilio_from_number="+10000000000")
        # Midday at mid-latitude -> f_dark = 0.
        bundle = self._bundle(lat=52.0, when=dt.datetime(2025, 6, 21, 12, 0, tzinfo=dt.timezone.utc))
        breakdown = compute_score(bundle, s)
        apply_calibration(breakdown, bundle, cal)
        assert breakdown.f_dark == 0.0
        assert breakdown.visibility_score == 0.0      # gated despite high probability
        assert breakdown.probability > 0.9            # the raw probability is still reported


class TestAssembleAndCalibrate:
    def _seed(self, db, labels):
        """labels: list of (cloud_cover, saw_aurora). One sub, one snapshot each."""
        sub = Subscription(phone="+1", address="x", lat=64.0, lon=-147.0, threshold=30.0)
        db.add(sub); db.commit(); db.refresh(sub)
        for i, (cloud, saw) in enumerate(labels):
            snap = _snapshot(cloud_cover=cloud, alerted=saw)
            snap.subscription_id = sub.id
            snap.checked_at = dt.datetime(2026, 1, 1) + dt.timedelta(hours=i)
            db.add(snap); db.commit(); db.refresh(snap)
            db.add(Observation(
                observed_at=snap.checked_at, saw_aurora=saw, source="manual",
                subscription_id=sub.id, alert_log_id=snap.id,
            ))
        db.commit()

    def test_assemble_only_linked_observations(self, db):
        self._seed(db, [(0.0, True), (100.0, False)])
        # An unlinked observation must be ignored.
        db.add(Observation(observed_at=dt.datetime(2026, 1, 2), saw_aurora=True, source="manual"))
        db.commit()
        X, y = assemble_dataset(db)
        assert X.shape == (2, len(FACTOR_NAMES))
        assert set(y.tolist()) == {0.0, 1.0}

    def test_assemble_empty(self, db):
        X, y = assemble_dataset(db)
        assert X.shape == (0, len(FACTOR_NAMES))
        assert y.shape == (0,)

    def test_calibrate_end_to_end(self, db):
        # Clear skies -> saw; overcast -> didn't. x_cloud = log(f_cloud), and
        # f_cloud=1 is clear, so a positive cloud coefficient means "clearer ->
        # more likely" — the data should keep it positive and separate perfectly.
        self._seed(db, [(0.0, True)] * 8 + [(100.0, False)] * 8)
        cal = calibrate(db, l2=0.1)
        assert isinstance(cal, Calibration)
        assert cal.n_samples == 16
        assert cal.n_positive == 8
        assert cal.coefficients["cloud"] > 0
        assert cal.metrics["roc_auc"] == pytest.approx(1.0)

    def test_calibration_json_roundtrip(self, db, tmp_path):
        self._seed(db, [(0.0, True), (100.0, False)])
        cal = calibrate(db)
        path = cal.to_json(tmp_path / "calibration.json")
        loaded = Calibration.load(path)
        assert loaded.coefficients == cal.coefficients
        assert loaded.intercept == pytest.approx(cal.intercept)
