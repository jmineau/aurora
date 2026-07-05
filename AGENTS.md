# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## Keep this file current

This document and [docs/roadmap.md](docs/roadmap.md) are the project's memory.
**As you work, update them** — it is part of finishing a task, not optional:

- Landed something on the roadmap? Check it off in
  [docs/roadmap.md](docs/roadmap.md) and note the commit/PR.
- Changed the architecture, a convention, a command, or the model's structure?
  Update the relevant section here so the next agent isn't working from a stale map.
- Discovered a new issue or limitation? Add it to the roadmap before you lose it.

Prefer a small doc edit in the same change over a "docs are out of date" comment.

## What this project is

An **aurora alert server**. It texts subscribers (via Twilio SMS) when the
chance of *seeing* the aurora at their location is favourable. The differentiator
versus existing tools (which mostly surface raw Kp or OVATION probability) is a
**composite visibility model**: it combines the NOAA/NASA OVATION Prime aurora
forecast with the atmospheric and geographic factors that determine whether a
human on the ground can actually see the aurora — clouds, aerosols, moon, light
pollution, terrain, and elevation.

The long-term goal is to be *better calibrated than the alternatives* by folding
in real ground-truth observations (see **Calibration**, below). Treat that as the
north star when making design decisions: prefer changes that make the model
measurable, calibratable, and honest about uncertainty over changes that just add
more hand-tuned factors.

## Domain context

The maintainer is an atmospheric scientist comfortable with geospatial/NWP data
(herbie, SynopticPy, xarray, etc.). Scientific correctness matters as much as code
quality. When touching the model, state your physical reasoning and cite the
assumption you are making. Don't silently "fix" a factor's math without explaining
the physics.

Key domain facts that the code must respect:

- **Aurora is emitted at ~100–400 km altitude.** That is why it is visible from
  hundreds of km away, low on the poleward horizon. At mid-latitudes you look
  *north* (N hemisphere) toward the oval, not overhead.
- **OVATION "aurora probability"** is the probability that auroral flux exceeds a
  threshold in a grid cell — i.e. *presence of aurora*, not *probability a person
  sees it*. It is naturally the P(aurora present) term; the other factors are
  P(visible | present).
- **Kp and OVATION are correlated** — OVATION is driven by the same solar-wind
  input. Multiplying both double-counts the geomagnetic driver. Acceptable as a
  heuristic; must be handled honestly once we calibrate.
- The system is currently a **nowcast**, not a forecast: it scores conditions
  *now* and only alerts when it is *currently* dark at the site. Lead time is
  therefore short. Any move toward "aurora forecast for tonight" is a real feature
  change, not a bug fix.

## Repository layout

```
src/aurora/
  main.py          FastAPI app: endpoints + APScheduler check loop
  aurora.py        AuroraChecker – orchestrates all factor fetches, returns CheckResult
  score.py         compute_score() – the weighted-product visibility model (the core)
  config.py        pydantic-settings Settings (env-driven; factor weights live here)
  db.py            SQLAlchemy models: Subscription, AlertLog, Observation
  feedback.py      Record/link ground-truth Observations; parse SMS Y/N replies
  geocoding.py     Nominatim geocoder with on-disk pickle cache
  sms.py           Twilio wrapper + inbound-webhook signature validation
  factors/         one module per factor, each exposing fetch_*() -> *Result dataclass
    ovation.py       NOAA SWPC OVATION Prime grid (cached, interpolated)
    kp.py            NOAA SWPC 1-min planetary Kp
    weather.py       Open-Meteo cloud cover (low/mid/high) + PWV
    aod.py           CAMS aerosol optical depth via Open-Meteo Air Quality
    terrain.py       Open-Meteo elevation + 8-point horizon estimate
    moon.py          astral lunar illumination (local, no I/O)
    light_pollution.py  Bortle class from a bundled VIIRS raster (data/bortle.npy)
data/
  download_bortle.py  one-time script to build data/bortle.npy from NASA NEO
tests/
  test_score.py    scoring-model properties (boundaries, monotonicity, weights)
  test_factors.py  pure/local factor math (moon, terrain geometry, darkness)
  conftest.py      stubs Twilio env vars before import
```

## The scoring model (`score.py`)

The score is a **weighted product** of per-factor transmittances in `[0, 1]`,
each raised to a configurable exponent weight, times a hard darkness gate, times
100:

```
score = f_dark · Π_i (f_i ^ w_i) · 100
```

`f_dark` is a hard gate (0 in daylight → 0 score). Weights `w_i` come from
`Settings` / `.env` (`WEIGHT_*`). See the `score.py` docstring for each factor's
derivation.

**Important property for calibration:** taking the log makes this a *linear
model* in the log-transmittances:

```
log(score/100) = log(f_dark) + Σ_i w_i · log(f_i)
```

So the weights are the coefficients of a log-linear model. That is the hook for
turning the hand-tuned weights into fitted coefficients (logistic regression on
`x_i = log(f_i)`) once ground-truth labels exist. Preserve this structure.

## Conventions

- **Python ≥ 3.11**, `uv` for env/deps. Package layout is `src/`-based.
- Each factor module exposes a `fetch_*()` returning a small frozen-ish
  `@dataclass` `*Result`. Keep network I/O inside `fetch_*`; keep pure math
  (transmittance conversion) in `score.py` so it stays unit-testable without HTTP.
- Async: all network `fetch_*` are `async` and gathered concurrently in
  `AuroraChecker.check`. Local calcs (moon, light pollution, score) are sync.
- Module-level TTL caches guard the shared NOAA endpoints (OVATION 15 min, Kp
  5 min) so one check cycle across many subscriptions makes one HTTP call.
- Static per-site factors (elevation, horizon, Bortle) are fetched once and cached
  on the `Subscription` row; dynamic factors are re-fetched every cycle.
- Settings are validated at import (Twilio creds required) — tests set stub env
  vars in `conftest.py`.

## Commands

```bash
uv sync --extra dev                 # install deps + pytest
uv run pytest -q                    # run tests (no network; all pure/local)
uv run aurora-server                # start the server (needs a real .env)
uv run python data/download_bortle.py   # one-time: build the Bortle raster
```

Tests must stay **offline** — do not add tests that hit the live NOAA/Open-Meteo
APIs. Mock or test the pure conversion functions instead.

## Known limitations / roadmap

These are understood gaps, roughly in priority order. Read before "fixing"
something that is a known trade-off. The **tracked, checkbox version** lives in
[docs/roadmap.md](docs/roadmap.md) — update that as items land.

1. **Viewing geometry is not modelled.** OVATION is sampled at the observer's
   coordinate (overhead), but mid-latitude viewers see the oval on the poleward
   horizon. The biggest accuracy win is to sample OVATION *poleward* of the
   observer and test whether the emission layer (~110 km) is geometrically above
   the local horizon. The `f_horiz` factor should feed this, not act as a generic
   penalty.
2. **No calibration / ground truth.** See below. This is the headline feature.
3. **Moon factor ignores lunar altitude.** A full moon below the horizon does not
   brighten the sky; `moon.py` uses only illumination fraction.
4. **Cloud is treated as overhead + linear.** `f_cloud = 1 − cover` (not the
   Beer-Lambert the docstring claims), and it uses total cover rather than the
   cloud along the line of sight toward the oval. Low/mid/high are fetched but
   unused in scoring.
5. **OVATION interpolator is rebuilt per call** (grid fill + `RegularGridInterpolator`
   construction happen after the JSON cache). Cache the interpolator, not just the
   JSON.
6. **Naive/aware datetime mix.** `main.py`/`db.py` use deprecated
   `datetime.utcnow()`; `aurora.py` uses aware UTC. Standardise on aware UTC.
7. **Nowcast, not forecast** (see Domain context). Alerts only fire during current
   darkness → little lead time.

## Calibration (the headline feature — design in progress)

Goal: use real observations — true positives, false positives, false negatives,
true negatives — to replace hand-tuned weights with fitted, *calibrated
probabilities*.

Data we already have: `AlertLog` logs every factor value + score + whether an
alert was sent, for **every** check cycle (not just alerts). That is the feature
store. What is missing is **labels** and a **fit step**:

- **Labels** *(built)*: users report whether aurora was actually visible via an
  `Observation` record `(lat, lon, observed_at, saw_aurora, intensity?, source)`,
  linked in `feedback.py` to the nearest `AlertLog` snapshot so each label carries
  its factor vector. Capture paths: SMS **Y/N** reply (`POST /sms/inbound` Twilio
  webhook) and `POST /report` for unsolicited sightings. The alert SMS prompts for
  the reply. Confusion-matrix class (TP/FP/FN/TN) is derived at fit time from
  (`alert_log.alerted`, `saw_aurora`), not stored.
- **Model**: fit **logistic regression on the log-transmittances**
  `x_i = log(f_i)` → `P(visible)`. This reuses the existing model structure (the
  weights become fitted coefficients) and yields a real probability.
- **Calibration + metrics**: report precision/recall, ROC-AUC, Brier score, and a
  reliability diagram; apply Platt/isotonic calibration. Expect heavy class
  imbalance (visible-aurora nights are rare at mid-latitudes) — use
  regularisation / a Bayesian prior and seed with the current hand weights.
- **Decision threshold**: the user's `threshold` becomes a choice on *calibrated
  probability* ("don't text me unless ≥70% likely I'll see it"), trading false
  alarms (precision) against misses (recall).

When implementing, keep the hand-tuned weighted-product model as the default/prior
so the system works with zero labels and improves as labels accumulate.
