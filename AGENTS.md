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
  *north* (N hemisphere) toward the oval, not overhead. **This is now modelled**
  (`geometry.py`): OVATION is sampled along a poleward profile and `f_ovation` uses
  the probability that clears the observer's (poleward) horizon, not the overhead
  value. Approximations remain (geographic — not geomagnetic — bearing; no
  low-elevation attenuation) — see docs/roadmap.md.
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
  score.py         compute_score() + per-factor f_* conversions and transmittances()
  geometry.py      viewing geometry: poleward projection, elevation angle, visible_aurora()
  calibration.py   fit P(saw|conditions) from Observations; metrics; aurora-calibrate CLI
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

## Extensibility — design for future upgrades

A standing goal: adding a **new notification channel** (email, push, in-app) or a
**new front end** (web GUI, mobile app) should be a small, localized change, not a
rewrite. We are not building those now, but keep the seams clean so they stay cheap.
Keep the layers separated as they are today:

- **Core (no I/O, no delivery).** `score.py` and the `factors/*` conversion math are
  pure and independently testable. Never import FastAPI, Twilio, or the DB into the
  scoring core. A CLI, a web app, and a cron job should all be able to call
  `AuroraChecker.check()` / `compute_score()` the same way.

- **Delivery / notifications — the SMS seam.** Outbound messaging goes through one
  place, `sms.py`, from a single call site in `check_all_subscriptions`
  ([main.py](src/aurora/main.py)). To add email/push/app later, introduce a
  `Notifier` interface (`send(recipient, subject, body)`) with one implementation per
  channel, and give `Subscription` a `channel` + `destination` instead of assuming
  `phone`. Until then, **don't scatter `send_sms`/Twilio/`phone` assumptions** beyond
  `sms.py`, the `Subscription` model, and that one call site. Alert *content* is
  already separate from *delivery* (`CheckResult.to_dict()` → `_format_alert`) — keep
  body formatting channel-specific and the payload neutral.

- **Presentation / API — the GUI seam.** The FastAPI app *is* the contract a web GUI
  or mobile app would consume; there is no HTML/templating in the core. A front end
  should talk to the same JSON endpoints (`/check`, `/subscribe`, `/report`, …). Keep
  request/response shapes as Pydantic models so the schema stays the single source of
  truth and OpenAPI stays usable by client generators.

- **Data sources / factors — the pattern to copy.** Each factor is a self-contained
  module exposing `async fetch_*(client, lat, lon) -> *Result`, with its [0,1]
  conversion in `score.py`. A new input (a different aurora model, a satellite cloud
  product) is one new module in the same shape — no change to the orchestrator's
  structure.

Rule of thumb: **a new channel or front end should touch delivery/presentation code
only.** If a proposed change reaches into `score.py`, `AuroraChecker`, or the factor
fetchers to support a channel/UI, reconsider the boundary.

## Commands

```bash
uv sync --extra dev                 # install deps + pytest
uv run pytest -q                    # run tests (no network; all pure/local)
uv run aurora-server                # start the server (needs a real .env)
uv run aurora-calibrate             # fit the model from logged observations, print a report
uv run python data/download_bortle.py   # one-time: build the Bortle raster
```

Tests must stay **offline** — do not add tests that hit the live NOAA/Open-Meteo
APIs. Mock or test the pure conversion functions instead.

## Known limitations / roadmap

The **single source of truth is [docs/roadmap.md](docs/roadmap.md)** — a tracked,
checkboxed list of headline features, physics corrections, engineering cleanup, and
done items. Read it before "fixing" something that may be a known trade-off, and
update it (check items off, add new findings) as part of any change.

The two headline efforts, at a glance:
- **Calibration loop** (labels → fitted probabilities): fully wired end-to-end
  (capture → fit → load → calibrated score); needs real labels to beat the prior.
  See below.
- **Viewing geometry** (poleward projection): built in `geometry.py`; refinements
  (geomagnetic bearing, low-elevation attenuation) are tracked in the roadmap.

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
- **Model** *(built)*: `calibration.py` fits **MAP logistic regression on the
  log-transmittances** `x_i = log(f_i)` → `P(saw)`, with a Gaussian prior centred
  on the current hand weights (`fit()` uses scipy L-BFGS with an analytic
  gradient; the `f_*` conversions come from `score.py` so features match scoring
  exactly). At zero labels the fit returns the hand weights; each fitted `β_i`
  stays directly comparable to the hand weight `w_i`. No sklearn dependency.
- **Metrics** *(built)*: `evaluate()` reports Brier, rank-based ROC-AUC,
  precision/recall/confusion at a threshold, and a reliability table;
  `cross_val_metrics()` adds k-fold out-of-sample Brier/AUC when n≥10. Expect
  heavy class imbalance (visible nights are rare mid-latitude) — the prior does
  the regularising. `aurora-calibrate` prints the report and writes
  `data/calibration.json`.
- **Decision threshold** *(built)*: `AuroraChecker` loads `data/calibration.json`
  if present and `calibration.apply_calibration()` overlays a **darkness-gated
  calibrated score** — `visibility_score` becomes `100·P(saw)`, so the existing
  0–100 subscription `threshold` reads directly as a percent chance (no schema
  change), and the weighted product is retained as `heuristic_score`. Falls back
  to the weighted product when there is no calibration; `reload_calibration()`
  picks up a re-fit; `/health` reports calibration status. Features come from the
  same geometry-aware transmittances the scorer uses, so they match what was logged.

The hand-tuned weighted-product remains the zero-label default and prior. The full
loop (capture → fit → load → calibrated score) is wired; it now just needs **real
labels** to beat the prior.
