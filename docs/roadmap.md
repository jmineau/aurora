# Roadmap & known issues

Working list of things we want to handle, so they don't get lost. Ordered by
impact within each section. Check items off as they land and add a one-line note
(PR/commit) next to completed ones. New findings go here first.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Headline features

- [ ] **Calibration loop (ground truth → fitted probabilities).** Replace
  hand-tuned weights with a fitted, calibrated `P(visible)`. See the full design
  in [AGENTS.md](../AGENTS.md#calibration-the-headline-feature--design-in-progress).
  Sub-tasks:
  - [x] `Observation` table: `(lat, lon, observed_at, saw_aurora, intensity?, source)`,
    joined to the nearest `AlertLog` snapshot so each label carries its factor
    vector. — `db.Observation` + `feedback.py` linking (commit on `dev`).
  - [x] Label capture: SMS **Y/N** reply (Twilio inbound webhook `POST /sms/inbound`)
    + `POST /report` endpoint for unsolicited sightings (incl. false negatives).
    Alert SMS now prompts for a Y/N reply.
  - [x] Offline fit script: MAP logistic regression on `x_i = log(f_i)` with a
    Gaussian prior centred on the current hand weights — `calibration.py` +
    `aurora-calibrate` CLI; writes `data/calibration.json`.
  - [x] Report precision/recall, ROC-AUC (rank-based), Brier score, reliability
    table + k-fold CV metrics. (Reliability is a text table, not a plot — a
    matplotlib `viz` extra could add the diagram later.)
  - [ ] Turn the user `threshold` into a calibrated-probability decision knob
    ("≥70% likely I'll actually see it"). **← next**
  - [ ] Wire `predict_proba` into live scoring as an opt-in (fall back to the
    hand-tuned weighted-product when no `calibration.json` exists).
  - [x] Keep the hand-tuned weighted-product as the zero-label default/prior —
    fit returns the prior exactly at zero labels; scoring still uses it.

  _Open follow-ups from this chunk:_ enable `TWILIO_VALIDATE_SIGNATURE=true` in
  production (public webhook writes to the DB); a Y/N reply currently attributes
  to the most recent *alerted* snapshot for the phone — ambiguous when a phone has
  several locations alerted in the same window (refine later).

- [~] **Viewing geometry — biggest accuracy win.** OVATION was sampled overhead;
  now projected from the *poleward* oval onto the observer's sky (`geometry.py`).
  - [x] Sample OVATION *poleward* of the observer (`ovation.sample_poleward_profile`).
  - [x] Elevation angle of the ~110 km emission layer above the horizon; gate on it
    (`geometry.elevation_angle` / `visible_aurora`); `f_ovation` uses the visible
    probability. Emission height is configurable (`AURORA_EMISSION_KM`).
  - [x] `terrain.horizon_deg` is now the *poleward* horizon and gates the geometry.
  - [ ] Refinements: use a **geomagnetic** poleward bearing (currently geographic —
    off by tens of degrees in azimuth at some longitudes); attenuate very
    low-elevation aurora (distant faint arcs); reconcile the `f_horiz` factor with
    the geometry gate (currently both penalise the poleward horizon — mild
    double-count).

---

## Model / physics corrections

- [ ] **`f_ovation` is a different kind of factor.** It is `P(aurora present)`;
  the rest are `P(visible | present)`. Make the conditional structure explicit:
  `P(see) = P(present) × P(visible | present)`.
- [ ] **Kp double-counts OVATION** (same solar-wind driver). Flag it; let the
  calibrated fit down-weight or drop it. Acceptable as a heuristic until then.
- [ ] **Moon factor ignores lunar altitude.** A full moon below the horizon
  doesn't brighten the sky — gate `f_moon` by moon elevation (astral provides it).
- [ ] **Cloud is overhead + linear, not as documented.** `f_cloud = 1 − cover` is
  linear (docstring claims Beer-Lambert), uses *total* cover (low/mid/high are
  fetched but unused), and is overhead rather than along the poleward line of
  sight. High cirrus vs low stratus matter very differently.
- [ ] **PWV is a constant.** The forecast endpoint doesn't return integrated water
  vapour, so `f_pwv` currently uses a fixed 20 mm fallback (`weather._extract_pwv`).
  Source real PWV from a reanalysis/forecast, or drop the factor until then.
- [ ] **Nowcast, not forecast.** Alerts only fire during *current* darkness → little
  lead time. Decide whether "will it be good tonight?" is a target product.

---

## Extensibility (deferred — do when the need arrives, not before)

Design principle and the clean seams are documented in
[AGENTS.md](../AGENTS.md#extensibility--design-for-future-upgrades). Keep the code
robust to these without building them yet:

- [ ] **Channel-agnostic notifications.** Introduce a `Notifier` interface
  (`send(recipient, subject, body)`) with per-channel implementations (SMS today;
  email/push/app later). Give `Subscription` a `channel` + `destination` instead of
  assuming `phone`. Trigger: when we add a second channel.
- [ ] **Front-end contract.** The FastAPI JSON API is already the GUI/app seam; keep
  endpoints Pydantic-typed. Trigger: when a web GUI or mobile app is started.

## Engineering / cleanup

- [x] **OVATION interpolator rebuilt per call.** Now the fitted interpolator is
  cached (not just the JSON) and the grid fill is vectorized (`ovation.py`).
- [ ] **Datetime hygiene.** `main.py`/`db.py` use deprecated `datetime.utcnow()`
  (naive); `aurora.py` uses aware UTC. Standardize on aware UTC everywhere.

---

## Done

- **SWPC Kp feed parsing bug.** `kp.py` assumed list-of-lists (`entry[1]`); the
  feed returns dicts (`estimated_kp`). This crashed every live check. Fixed +
  regression-tested (`_parse_latest_kp`).
- **Weather PWV IndexError.** Missing PWV series was indexed out of range, also
  crashing live checks. Guarded (`_extract_pwv`) + tested.
