# Collecting aurora observations (for calibration)

The model learns from **contrast** — nights the aurora was visible vs. nights it
wasn't. A log of only sightings can't calibrate anything; the single most useful
thing you can do is **record the blank nights too**. A "clear sky, looked north,
saw nothing" entry is as valuable as a photo.

Fill in [`observations_template.csv`](observations_template.csv) — one row per
time you (or Mom) actually looked at the sky. Delete the three EXAMPLE rows first.

## Columns

| Column | Required | What to put |
|---|---|---|
| `observed_at_local` | yes | Local date + clock time you looked, `YYYY-MM-DD HH:MM` (24-hour). The importer derives the time zone from the location, so just use local wall-clock time. |
| `lat`, `lon` | preferred | Decimal degrees of where you were (e.g. `64.8378,-147.7164`). Leave blank if you don't know them and fill `place` instead. |
| `place` | if no lat/lon | A place name the importer can geocode, e.g. `"Fairbanks, AK"`. Quote it if it contains a comma. |
| `saw` | yes | `y` if the aurora was visible to the eye, `n` if not. |
| `intensity` | yes | `0` nothing · `1` faint / camera-only · `2` clear glow or arc · `3` bright / structured / moving. Use `0` whenever `saw=n`. |
| `notes` | optional | Anything useful: cloud, moon, direction you looked, how long, whether it was a photo. Quote if it contains a comma. |

## What counts as a good entry

- **Log every night you look**, sighting or not. Twenty "nothing" nights and five
  sightings is a real dataset; five sightings alone is not.
- **One row per session.** If you watched for an hour and it came and went, log the
  best moment (highest intensity) — or a couple rows if conditions changed a lot.
- **Camera-only counts as `saw=n, intensity=1`** if your eyes couldn't see it — the
  model predicts *human* visibility. Note "camera only" so we can revisit later.
- **Back-fill from photos:** each aurora photo you already have is a `saw=y` row —
  its timestamp and GPS (EXIF) give you `observed_at_local`, `lat`, `lon`. These are
  positives; they're most useful alongside the negative nights.

## What happens next

An importer (coming in a follow-up) reads this CSV and, for each row, reconstructs
the sky/space-weather conditions at that time and place from reanalysis data, then
stores it as a labelled `Observation` the calibration fit can train on. You don't
need the alert server running to collect — a spreadsheet is enough.
