"""Calibrate the visibility model against ground-truth observations.

The hand-tuned scorer is, in log space, a linear model:

    log(score / 100) = log(f_dark) + Σ_i w_i · log(f_i)

so the weights ``w_i`` are the coefficients of a linear model over the
log-transmittances ``x_i = log(f_i)``.  This module fits those coefficients to
real labels instead of setting them by hand, by MAP logistic regression:

    P(saw aurora) = sigmoid( β₀ + Σ_i β_i · x_i )

with a Gaussian prior centred on the current hand weights.  With zero labels the
fit returns the hand weights exactly; as labels accumulate it moves off the prior
only where the data supports it.  Because ``x_i`` are the same log-transmittances
the scorer uses, each fitted ``β_i`` is directly comparable to the hand weight
``w_i`` — the model structure is preserved, only the numbers are learned.

The confusion-matrix framing (TP/FP/FN/TN) is just the joint distribution of
(did we alert, did they see it); here we model the more informative quantity —
P(saw | conditions) — and let the alert threshold be chosen on that probability.

This module is pure/offline: assembly reads the DB, everything else is numpy +
scipy.  Nothing here is wired into live scoring yet (that is the next step); run
``aurora-calibrate`` to fit and inspect a report.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from sqlalchemy.orm import Session

from aurora.config import Settings, settings
from aurora.db import AlertLog, Observation
from aurora.score import (
    FACTOR_NAMES,
    f_aod,
    f_cloud,
    f_elev,
    f_horiz,
    f_kp,
    f_lp,
    f_moon,
    f_ovation,
    f_pwv,
)

# Transmittances of 0 would send log() to -inf; floor them.
_FLOOR = 1e-3
# Weak ridge on the intercept so it stays finite under separable data.
_INTERCEPT_L2 = 1e-2
_DEFAULT_L2 = 1.0
_CALIBRATION_PATH = Path(__file__).parent.parent.parent / "data" / "calibration.json"


@dataclass
class Calibration:
    """A fitted model: intercept + one coefficient per factor, plus provenance."""

    intercept: float
    coefficients: dict[str, float]      # factor name -> β_i
    prior: dict[str, float]             # factor name -> hand weight used as prior mean
    l2: float
    n_samples: int
    n_positive: int
    metrics: dict = field(default_factory=dict)
    trained_at: str | None = None

    def to_json(self, path: Path | str = _CALIBRATION_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    @classmethod
    def load(cls, path: Path | str = _CALIBRATION_PATH) -> "Calibration | None":
        path = Path(path)
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text()))


# ── Feature construction ──────────────────────────────────────────────────────

def _raw_transmittances(row: AlertLog) -> dict[str, float]:
    """The nine [0, 1] transmittances for a logged snapshot (same math as scoring)."""
    return {
        "ovation": f_ovation(row.ovation_prob),
        "kp": f_kp(row.kp_index),
        "cloud": f_cloud(row.cloud_cover),
        "aod": f_aod(row.aod),
        "elev": f_elev(row.elevation_m),
        "moon": f_moon(row.moon_illumination),
        "lp": f_lp(row.bortle),
        "pwv": f_pwv(row.pwv_mm),
        "horiz": f_horiz(row.horizon_deg),
    }


def features_from_snapshot(row: AlertLog) -> np.ndarray:
    """Log-transmittance feature vector x_i = log(f_i), ordered by FACTOR_NAMES."""
    t = _raw_transmittances(row)
    return np.array([np.log(max(t[name], _FLOOR)) for name in FACTOR_NAMES], dtype=float)


def hand_weight_prior(cfg: Settings = settings) -> np.ndarray:
    """The current hand-tuned weights, ordered by FACTOR_NAMES — the prior mean."""
    return np.array([getattr(cfg, f"weight_{name}") for name in FACTOR_NAMES], dtype=float)


def assemble_dataset(db: Session) -> tuple[np.ndarray, np.ndarray]:
    """Build (X, y) from observations that are linked to a feature snapshot.

    Only observations with a linked ``AlertLog`` contribute (they are the ones
    that carry a factor vector).  y = 1 if the aurora was seen.
    """
    rows = (
        db.query(Observation, AlertLog)
        .join(AlertLog, Observation.alert_log_id == AlertLog.id)
        .all()
    )
    if not rows:
        return np.empty((0, len(FACTOR_NAMES))), np.empty((0,))
    X = np.vstack([features_from_snapshot(alert) for _, alert in rows])
    y = np.array([1.0 if obs.saw_aurora else 0.0 for obs, _ in rows])
    return X, y


# ── Fit ───────────────────────────────────────────────────────────────────────

def _sigmoid(z: np.ndarray) -> np.ndarray:
    return np.where(z >= 0, 1.0 / (1.0 + np.exp(-z)), np.exp(z) / (1.0 + np.exp(z)))


def fit(
    X: np.ndarray,
    y: np.ndarray,
    *,
    prior: np.ndarray,
    l2: float = _DEFAULT_L2,
) -> tuple[float, np.ndarray]:
    """MAP logistic regression with a Gaussian prior centred on *prior*.

    Minimises  −loglik + (l2/2)·‖β − prior‖²  (+ a weak ridge on the intercept).
    With no data the minimiser sits exactly at (0, prior).  Returns (β₀, β).
    """
    n, p = X.shape
    b0_prior = float(np.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6))) if n else 0.0

    def objective(theta: np.ndarray) -> tuple[float, np.ndarray]:
        b0, b = theta[0], theta[1:]
        z = b0 + X @ b if n else np.zeros(0)
        pr = _sigmoid(z)
        # Negative log-likelihood (0 when there is no data).
        eps = 1e-12
        nll = -np.sum(y * np.log(pr + eps) + (1 - y) * np.log(1 - pr + eps)) if n else 0.0
        # Gaussian priors: slopes toward `prior`, intercept toward base-rate logit.
        reg = 0.5 * l2 * np.sum((b - prior) ** 2) + 0.5 * _INTERCEPT_L2 * (b0 - b0_prior) ** 2
        loss = nll + reg

        resid = (pr - y) if n else np.zeros(0)
        grad_b0 = (np.sum(resid) if n else 0.0) + _INTERCEPT_L2 * (b0 - b0_prior)
        grad_b = (X.T @ resid if n else np.zeros(p)) + l2 * (b - prior)
        return loss, np.concatenate([[grad_b0], grad_b])

    theta0 = np.concatenate([[b0_prior], prior])
    res = minimize(objective, theta0, jac=True, method="L-BFGS-B")
    return float(res.x[0]), res.x[1:]


def predict_proba(X: np.ndarray, intercept: float, coefficients: np.ndarray) -> np.ndarray:
    """P(saw aurora) for each row of log-transmittance features X."""
    X = np.atleast_2d(X)
    return _sigmoid(intercept + X @ np.asarray(coefficients))


def calibrate(
    db: Session, *, cfg: Settings = settings, l2: float = _DEFAULT_L2
) -> Calibration:
    """Assemble labels, fit, evaluate, and return a Calibration (unsaved)."""
    X, y = assemble_dataset(db)
    prior = hand_weight_prior(cfg)
    intercept, coef = fit(X, y, prior=prior, l2=l2)

    metrics = evaluate(y, predict_proba(X, intercept, coef)) if len(y) else {}
    if len(y) >= 10:
        metrics["cv"] = cross_val_metrics(X, y, prior=prior, l2=l2)

    return Calibration(
        intercept=intercept,
        coefficients={name: float(c) for name, c in zip(FACTOR_NAMES, coef)},
        prior={name: float(w) for name, w in zip(FACTOR_NAMES, prior)},
        l2=l2,
        n_samples=int(len(y)),
        n_positive=int(y.sum()),
        metrics=metrics,
    )


# ── Metrics ───────────────────────────────────────────────────────────────────

def roc_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """AUC via the Mann–Whitney U statistic (rank-based). NaN if only one class."""
    pos = y_prob[y_true == 1]
    neg = y_prob[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    ranks = np.argsort(np.argsort(np.concatenate([pos, neg]))) + 1
    r_pos = ranks[: len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def reliability(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> list[dict]:
    """Reliability-diagram data: per-bin count, mean predicted, observed frequency."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi if hi < 1.0 else y_prob <= hi)
        if not mask.any():
            continue
        out.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "count": int(mask.sum()),
            "mean_predicted": float(y_prob[mask].mean()),
            "observed_freq": float(y_true[mask].mean()),
        })
    return out


def evaluate(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> dict:
    """In-sample metrics: Brier, AUC, precision/recall/confusion at *threshold*."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    pred = (y_prob >= threshold).astype(float)
    tp = float(np.sum((pred == 1) & (y_true == 1)))
    fp = float(np.sum((pred == 1) & (y_true == 0)))
    fn = float(np.sum((pred == 0) & (y_true == 1)))
    tn = float(np.sum((pred == 0) & (y_true == 0)))
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    return {
        "n": int(len(y_true)),
        "n_positive": int(y_true.sum()),
        "brier": float(np.mean((y_prob - y_true) ** 2)),
        "roc_auc": roc_auc(y_true, y_prob),
        "threshold": threshold,
        "precision": precision,
        "recall": recall,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "reliability": reliability(y_true, y_prob),
    }


def cross_val_metrics(
    X: np.ndarray, y: np.ndarray, *, prior: np.ndarray, l2: float, k: int = 5
) -> dict:
    """k-fold out-of-sample Brier and AUC — the honest generalisation estimate.

    Deterministic folds (no shuffling) so results are reproducible.
    """
    n = len(y)
    folds = np.array_split(np.arange(n), min(k, n))
    oof = np.full(n, np.nan)
    for test_idx in folds:
        train_idx = np.setdiff1d(np.arange(n), test_idx)
        if len(train_idx) == 0:
            continue
        b0, b = fit(X[train_idx], y[train_idx], prior=prior, l2=l2)
        oof[test_idx] = predict_proba(X[test_idx], b0, b)
    return {
        "brier": float(np.mean((oof - y) ** 2)),
        "roc_auc": roc_auc(y, oof),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _format_report(cal: Calibration) -> str:
    lines = [
        "Aurora model calibration",
        "=" * 60,
        f"labels: {cal.n_samples}  (positive: {cal.n_positive}, "
        f"negative: {cal.n_samples - cal.n_positive})   l2={cal.l2}",
        "",
        f"{'factor':<9}{'prior_w':>10}{'fitted':>10}{'delta':>9}",
    ]
    for name in FACTOR_NAMES:
        w = cal.prior[name]
        b = cal.coefficients[name]
        lines.append(f"{name:<9}{w:>10.3f}{b:>10.3f}{b - w:>9.3f}")
    lines.append(f"{'intercept':<9}{'':>10}{cal.intercept:>10.3f}")

    m = cal.metrics
    if m:
        lines += ["", f"in-sample: Brier={m['brier']:.3f}  AUC={m['roc_auc']:.3f}  "
                      f"P={m['precision']:.2f}  R={m['recall']:.2f} @thr={m['threshold']}"]
        if "cv" in m:
            lines.append(f"{5}-fold CV: Brier={m['cv']['brier']:.3f}  AUC={m['cv']['roc_auc']:.3f}")
        if m.get("reliability"):
            lines += ["", "reliability (bin: count  predicted -> observed):"]
            for r in m["reliability"]:
                lines.append(
                    f"  {r['bin']}: {r['count']:>4}  {r['mean_predicted']:.2f} -> {r['observed_freq']:.2f}"
                )
    if cal.n_samples < 10:
        lines += ["", "NOTE: very few labels — coefficients are dominated by the "
                      "hand-weight prior and metrics are unreliable. Keep collecting."]
    return "\n".join(lines)


def main() -> None:
    """Console entry point (``aurora-calibrate``): fit from the DB and report."""
    from aurora.db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        cal = calibrate(db)
    finally:
        db.close()

    # Stamp the training time here (not inside calibrate, which stays pure/testable).
    cal.trained_at = dt.datetime.now(dt.timezone.utc).isoformat()
    print(_format_report(cal))
    path = cal.to_json()
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
