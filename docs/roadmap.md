# Roadmap & known issues

Working list of things we want to handle, so they don't get lost. Ordered by
impact within each section. Check items off as they land and add a one-line note
(PR/commit) next to completed ones. New findings go here first.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Headline features

- [~] **Calibration loop (ground truth → fitted probabilities).** Replace
  hand-tuned weights with a fitted, calibrated `P(visible)`. See the full design
  in [AGENTS.md](../AGENTS.md#calibration-the-headline-feature--design-in-progress).
  The machinery is complete end-to-end (capture → fit → load → calibrated score);
  it now just needs **real labels** to improve on the prior. Sub-tasks:
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
  - [x] Wire `predict_proba` into live scoring as an opt-in — `AuroraChecker`
    loads `data/calibration.json` if present and overlays a darkness-gated
    calibrated score (`calibration.apply_calibration`); falls back to the
    weighted product otherwise. `reload_calibration()` picks up a re-fit.
  - [x] Turn the user `threshold` into a calibrated-probability decision knob:
    when calibrated, `visibility_score` becomes `100·P(saw)`, so the existing
    0–100 threshold reads directly as a percent chance (no schema change). SMS
    says "Chance you see it: X%"; `/health` reports calibration status.
  - [x] Keep the hand-tuned weighted-product as the zero-label default/prior —
    fit returns the prior exactly at zero labels; scoring still uses it.

  - [x] **Backfill importer** (`aurora-import` / `backfill.py`): CSV of sightings →
    reconstruct each row's atmospheric/static/moon factors from reanalysis
    (Open-Meteo ERA5 + CAMS archive, astral, rasters) → labelled `Observation` +
    `backfilled` `AlertLog`. Turns photos + logs into training data with no server
    running.
  - [x] **Phase 2 — historical space weather** (`space_weather.py`): OvationPyme is
    GitHub-only and its `aacgmv2` C-extension doesn't install on Python 3.14, so
    reconstruct the space-weather factor from **archived Kp** (GFZ) + a
    **geomagnetic-latitude auroral-oval model**, projected through the same
    `geometry.visible_aurora` pipeline as live. Backfilled rows now carry real
    `ovation_prob` + `kp_index` (no NULLs), so negatives are conditioned. Validated
    on the July 3 storm: Kp 7.3 → 99% modelled visibility at Utah vs 5% quiet.
    **Caveats:** a proxy (no local-time/substructure detail; approximate boundary
    constants), and different in *source* from the live SWPC OVATION feature, so a
    live+backfill mix has some model skew. Removing skew entirely means one model
    for both live and historical (OvationPyme) — a larger commitment; the oval
    could also move from centred dipole to AACGM.

  _Open follow-ups from this chunk:_ enable `TWILIO_VALIDATE_SIGNATURE=true` in
  production (public webhook writes to the DB); a Y/N reply currently attributes
  to the most recent *alerted* snapshot for the recipient (phone or inReach GUID)
  — ambiguous when it has several locations alerted in the same window (refine
  later).

- [~] **Viewing geometry — biggest accuracy win.** OVATION was sampled overhead;
  now projected from the *poleward* oval onto the observer's sky (`geometry.py`).
  - [x] Sample OVATION *poleward* of the observer (`ovation.sample_poleward_profile`).
  - [x] Elevation angle of the ~110 km emission layer above the horizon; gate on it
    (`geometry.elevation_angle` / `visible_aurora`); `f_ovation` uses the visible
    probability. Emission height is configurable (`AURORA_EMISSION_KM`).
  - [x] `terrain.horizon_deg` is now the *poleward* horizon and gates the geometry.
  - [x] **Geomagnetic poleward bearing** — sample OVATION (and the terrain horizon)
    toward the geomagnetic pole, not geographic north (`geometry.geomagnetic_pole_bearing`,
    centred-dipole). ~9° E of N from Utah, ~12° from Seattle, W of N from Europe.
    Could upgrade the centred dipole to AACGM later.
  - [ ] Refinements: attenuate very low-elevation aurora (distant faint arcs);
    reconcile the `f_horiz` factor with the geometry gate (both penalise the
    poleward horizon — mild double-count).

---

## Model / physics corrections

- [ ] **`f_ovation` is a different kind of factor.** It is `P(aurora present)`;
  the rest are `P(visible | present)`. Make the conditional structure explicit:
  `P(see) = P(present) × P(visible | present)`.
- [ ] **Kp double-counts OVATION** (same solar-wind driver). Flag it; let the
  calibrated fit down-weight or drop it. Acceptable as a heuristic until then.
- [x] **Moon factor ignores lunar altitude.** Fixed: `fetch_moon(when, lat, lon)`
  computes the moon's altitude (astral) and the scoring input is now
  `illumination × max(0, sin(altitude))` — 0 when the moon is below the horizon.
  Caught from the July 3 Utah sighting (bright moon, but set until ~midnight).
- [~] **Cloud modelling.**
  - [x] *Directional:* cloud is now sampled overhead **and** toward the poleward
    horizon and blended by the aurora's elevation (`geometry.line_of_sight_cloud`,
    `weather` two-point fetch) — so a clear north with cloudy overhead (or vice
    versa) scores correctly. Verified (e.g. Bergen: 48% overhead vs 100% poleward).
  - [ ] *Remaining:* `f_cloud = 1 − cover` is linear, not Beer-Lambert on optical
    depth; low/mid/high layers are fetched but unused (high cirrus vs low stratus
    obscure very differently).
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

- [x] **Channel-agnostic notifications.** `notify.py`: `Notifier` interface
  (`send(recipient, subject, body)`) + registry, `TwilioSmsNotifier` and
  `InReachNotifier`; `Subscription` has `channel` + `recipient` (migrated from
  `phone`). Branch `claude/inreach-notifier-94d8dc`.
- [x] **Garmin inReach channel** (for canoe trips out of cell range, no cost
  beyond the Garmin plan). Delivery through the reply form behind Garmin's
  email links; pairing and Y/N replies through an IMAP-polled aurora mailbox
  (`inbox.py`, `GET /pairings`); failure notices to `NTFY_URL`. Same branch.
- [ ] **Plain email channel** (SMTP out, replies via the existing IMAP poller):
  one more `Notifier` in `_REGISTRY`. Not built yet.
- [ ] **Per-recipient send cap.** The cooldown is per *subscription*: an inReach
  subscribed at several locations can get one alert per location in the same
  night, each billed. Add a per-recipient minimum gap if that happens.
- [ ] **Front-end contract.** The FastAPI JSON API is already the GUI/app seam; keep
  endpoints Pydantic-typed. Trigger: when a web GUI or mobile app is started.

## Risks

- **Garmin's inReach reply form is undocumented.** `POST /TextMessage/TxtMsg` with
  `Guid`/`ReplyAddress`/`ReplyMessage` has worked for community scripts since
  ~2018 with no CAPTCHA as of 2023, but Garmin can change or block it at any
  time (new fields, a CAPTCHA, user-agent filtering, a moved host). Mitigations:
  anything but HTTP 200 + `"Success": true` is a failure, logged with the first
  500 chars of the response; `NTFY_URL` gets a notice; the cooldown stops repeat
  attempts. Test the channel with a real device before each trip. The GUID in a
  reply link is a capability (anyone holding it can message the device), so it is
  masked in ntfy notices.

## Engineering / cleanup

- [x] **OVATION interpolator rebuilt per call.** Now the fitted interpolator is
  cached (not just the JSON) and the grid fill is vectorized (`ovation.py`).
- [x] **Datetime hygiene.** All DB timestamps standardized on a naive-UTC helper
  (`db.utcnow`); deprecated `datetime.utcnow()` removed (matters on Python 3.14).
- [x] **Configurable bind address.** `HOST` / `PORT` settings (default
  `0.0.0.0:8000`) instead of a hardcoded port — 8000 collides with Pi-hole's web UI.

## Deployment / running it

- [x] **Raspberry Pi + Tailscale deploy guide** — systemd service, tailnet access,
  optional Tailscale Funnel for the public webhook. See
  [deploy-raspberry-pi.md](deploy-raspberry-pi.md).
- [ ] **Run it for real (this season).** Deploy to the Pi, subscribe the N-Utah
  site, start accumulating snapshots + labels. Mid-latitude + solar max = events
  can happen any night now, so this is live, not off-season.
- [x] **Optional: run collect-only without Twilio.** Twilio creds are now
  all-or-none; with none, snapshots log every cycle and alerts are logged, not
  texted (`settings.sms_enabled`, `/health` reports it). Partial config fails at
  startup. Free alternative to SMS (ntfy/Pushover via a `Notifier`) still open;
  the `Notifier` seam now exists, so it is one class.
- [x] **SMS cost and opt-out.** The alert is one GSM-7 line (<=160 chars, one
  segment; a degree sign or em-dash used to force UCS-2 and double the cost), and
  `/sms/inbound` honours STOP/START/HELP keywords (deactivate, never delete).

---

## Done

- **SWPC Kp feed parsing bug.** `kp.py` assumed list-of-lists (`entry[1]`); the
  feed returns dicts (`estimated_kp`). This crashed every live check. Fixed +
  regression-tested (`_parse_latest_kp`).
- **Weather PWV IndexError.** Missing PWV series was indexed out of range, also
  crashing live checks. Guarded (`_extract_pwv`) + tested.
