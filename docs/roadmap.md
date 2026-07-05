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
  - [ ] Offline fit script: regularized logistic regression on `x_i = log(f_i)`,
    seeded with current hand weights as a prior. **← next**
  - [ ] Report precision/recall, ROC-AUC, Brier score, reliability diagram.
  - [ ] Turn the user `threshold` into a calibrated-probability decision knob
    ("≥70% likely I'll actually see it").
  - [ ] Keep the hand-tuned weighted-product as the zero-label default/prior.

  _Open follow-ups from this chunk:_ enable `TWILIO_VALIDATE_SIGNATURE=true` in
  production (public webhook writes to the DB); a Y/N reply currently attributes
  to the most recent *alerted* snapshot for the phone — ambiguous when a phone has
  several locations alerted in the same window (refine later).

- [ ] **Viewing geometry — biggest accuracy win.** OVATION is sampled at the
  observer's coordinate (overhead), but mid-latitude viewers see the oval low on
  the *poleward* horizon because it emits at ~100–400 km altitude.
  - [ ] Sample OVATION *poleward* of the observer, not at the point.
  - [ ] Compute the elevation angle of the ~110 km emission layer above the
    observer's horizon; check it clears local terrain.
  - [ ] Wire `f_horiz` into this geometry instead of using it as a generic penalty.

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

- [ ] **OVATION interpolator rebuilt per call.** Grid fill + `RegularGridInterpolator`
  construction happen on every location lookup after the JSON cache. Cache the
  interpolator, not just the JSON. The Python loop filling the 360×181 grid is
  also slow — vectorize it.
- [ ] **Datetime hygiene.** `main.py`/`db.py` use deprecated `datetime.utcnow()`
  (naive); `aurora.py` uses aware UTC. Standardize on aware UTC everywhere.

---

## Done

_(nothing yet)_
