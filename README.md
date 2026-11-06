# Aurora Alert Server

A Python server that alerts you when there's a good chance of seeing the aurora
borealis at one or more locations.  Conditions are evaluated on a configurable
schedule using a physics-motivated composite visibility model that goes well
beyond simple geomagnetic indices.

Alerts go out by **SMS** (Twilio, the everyday channel) or to a **Garmin
inReach** satellite messenger (for trips out of cell range).  Every alert is one
short line that fits a single SMS segment and a single inReach message:

```
Aurora alert, N Utah: 72% chance. Kp 6.3, cloud 30%, moon 42% down. 01:10-01:40 MST. Saw it? Reply Y or N
```

Y/N replies on either channel are stored as ground truth to calibrate the model.

## How the scoring model works

The visibility score (0–100) is a **weighted product** of nine independent
atmospheric and geophysical factors.  Each factor is converted to a
transmittance value in [0, 1], then raised to a configurable weight exponent:

```
score = f_dark
      × f_ovation^w  × f_kp^w
      × f_cloud^w    × f_aod^w   × f_pwv^w
      × f_elev^w     × f_horiz^w
      × f_moon^w     × f_lp^w
      × 100
```

| Factor | Source | Physical meaning |
|---|---|---|
| `f_dark` | `astral` (local) | Hard gate – 0 in daylight, linear through twilight, 1 at night |
| `f_ovation` | NOAA SWPC OVATION Prime | Interpolated aurora probability at site (0–100 %) |
| `f_kp` | NOAA SWPC Kp 1-min | Geomagnetic activity; higher Kp → brighter, more equatorward aurora |
| `f_cloud` | Open-Meteo Forecast | Cloud transmittance; Beer-Lambert on total cover |
| `f_aod` | CAMS via Open-Meteo Air Quality | Aerosol extinction at 550 nm; airmass ≈ 2 at 30° look angle |
| `f_pwv` | Open-Meteo Forecast | Precipitable water vapour; near-IR extinction |
| `f_elev` | Open-Meteo Elevation API | Elevation benefit; higher sites are above more of the aerosol/cloud deck |
| `f_horiz` | Open-Meteo Elevation API | Topographic horizon obstruction (8-point sampling at 20 km) |
| `f_moon` | `astral` (local) | Sky background brightness from lunar illumination |
| `f_lp` | NASA Black Marble (VIIRS, bundled) | Light pollution; Bortle class 1–9 |

Static factors (elevation, horizon, Bortle) are fetched once per subscription
and cached in the database.  Dynamic factors are re-fetched every check cycle.

## Setup

### 1. Prerequisites

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A [Twilio](https://console.twilio.com) account with a phone number, to send SMS
  alerts (optional — without it the server runs collect-only)
- For inReach alerts: an email mailbox for aurora, ideally with IMAP access
  (optional; see [Pairing an inReach](#pairing-an-inreach))

### 2. Install

```bash
git clone https://github.com/jmineau/aurora
cd aurora
uv sync --extra dev        # installs all deps + pytest
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env – fill in the three Twilio variables to send SMS, or leave all
# three blank to run collect-only (logs a snapshot every cycle, no texts).
# Likewise INREACH_REPLY_ADDRESS (+ IMAP_*) for inReach; every channel left
# unconfigured just logs its alerts.
```

### 4. Build the Bortle light-pollution raster (one-time)

```bash
uv run python data/download_bortle.py
```

This downloads a ~4 MB nighttime-lights PNG from NASA NEO and writes
`data/bortle.npy` (~25 MB).  The server will start without this file but
will log a warning and use a default Bortle 4 (rural/suburban) value.

### 5. Run the server

```bash
uv run aurora-server
# or:
uv run uvicorn aurora.main:app --reload
```

The server starts on `http://localhost:8000` (set `HOST` / `PORT` in `.env` to
change it).  Interactive API docs are at `http://localhost:8000/docs`.

To run it continuously (e.g. on a Raspberry Pi over Tailscale), see
[docs/deploy-raspberry-pi.md](docs/deploy-raspberry-pi.md).

### 6. Run tests

```bash
uv run pytest -v
```

## API

### Subscribe

```http
POST /subscribe
Content-Type: application/json

{
  "phone": "+12125551234",
  "locations": ["Fairbanks, AK", "Tromsø, Norway"],
  "threshold": 35
}
```

That is the SMS shorthand.  The general form names a `channel` (`"sms"`, the
default, or `"inreach"`) and its `recipient`:

```http
POST /subscribe
Content-Type: application/json

{
  "channel": "inreach",
  "recipient": "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9",
  "locations": ["Algonquin Provincial Park, Ontario"],
  "threshold": 35
}
```

SMS recipients must be E.164 phone numbers; inReach recipients are the device's
32-hex-digit GUID (see [Pairing an inReach](#pairing-an-inreach)).

`threshold` is the minimum visibility score (0–100) required to trigger an
alert.  Sensible starting points: 25 for high-latitude sites, 40 for
mid-latitudes.  Once a calibration has been fitted (see below) the score becomes
a calibrated **percent chance of seeing the aurora**, so the threshold reads
directly as "don't text me unless I'm at least this % likely to see it".

### Unsubscribe

```http
DELETE /unsubscribe/+12125551234
DELETE /unsubscribe/0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9
```

SMS subscribers can also text **STOP** (or STOPALL, UNSUBSCRIBE, CANCEL, END,
QUIT) to pause all their subscriptions, **START** to resume, and **HELP** for a
one-line description.  Subscriptions are deactivated, never deleted.

### List subscriptions

```http
GET /subscriptions/+12125551234
```

### Pending inReach pairings

```http
GET /pairings
```

inReach devices that have messaged the aurora mailbox but have no subscription
yet, with their GUIDs.

### Ad-hoc condition check

```http
GET /check?lat=64.2&lon=-21.9
```

Returns the current score and all factor values for the coordinate.

### Health

```http
GET /health
```

Reports the scheduler, calibration, and which channels really send:
`"channels": {"sms": true, "inreach": false}` (false means collect-only: alerts
for that channel are logged, not sent), plus `mailbox_enabled` and
`ntfy_enabled`.

## Pairing an inReach

Garmin gives an inReach no phone number or email address, but every email it
sends carries a reply link with the device's GUID, and aurora replies through
that link.  Setup, once:

1. Create a mailbox for aurora (any IMAP account; a dedicated one is best) and
   set `INREACH_REPLY_ADDRESS` to its address and the `IMAP_*` variables to its
   login.  Restart the server.
2. From the inReach, send any message (e.g. "pair") to that address.
3. Within `IMAP_POLL_SECONDS` aurora logs
   `inReach pairing request: GUID 0a1b2c3d-...` and lists it at `GET /pairings`.
   (Without IMAP, open the email yourself: the GUID is the `extId=` value in the
   reply link.)
4. Subscribe with `{"channel": "inreach", "recipient": "<that GUID>", ...}`.

Each message an inReach receives counts against its Garmin plan, so aurora sends
at most one alert per subscription per `ALERT_COOLDOWN_HOURS` and never sends
confirmations back.  Reply **Y** or **N** from the device after an alert to log
whether you saw it.  The Garmin reply form is undocumented and may change: a
failed send is logged with Garmin's response, and if `NTFY_URL` is set a
one-line notice is posted there.

## .env configuration reference

| Variable | Default | Description |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | _(blank)_ | From the Twilio Console. Set all three Twilio vars or none |
| `TWILIO_AUTH_TOKEN` | _(blank)_ | From the Twilio Console |
| `TWILIO_FROM_NUMBER` | _(blank)_ | Your Twilio number in E.164 format |
| `TWILIO_VALIDATE_SIGNATURE` | `false` | Check `X-Twilio-Signature` on `/sms/inbound`; set `true` in production |
| `INREACH_REPLY_ADDRESS` | _(blank)_ | The aurora mailbox inReach devices message. Blank = inReach alerts are logged, not sent |
| `INREACH_BASE_URL` | `https://inreach.garmin.com` | Garmin reply-form host (point at a test server in development) |
| `INREACH_TIMEOUT_SECONDS` | `20` | HTTP timeout for the Garmin form POST |
| `IMAP_HOST` | _(blank)_ | Aurora mailbox server. Set host, user and password together, or none |
| `IMAP_PORT` | `993` | IMAP over TLS port |
| `IMAP_USER` | _(blank)_ | Mailbox login |
| `IMAP_PASSWORD` | _(blank)_ | Mailbox password (an app password for Gmail/Fastmail) |
| `IMAP_FOLDER` | `INBOX` | Folder to read Garmin mail from |
| `IMAP_POLL_SECONDS` | `120` | How often the mailbox is checked |
| `NTFY_URL` | _(blank)_ | ntfy topic URL; a failed alert posts a one-line notice here |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `CHECK_INTERVAL_MINUTES` | `30` | How often the scheduler runs |
| `ALERT_COOLDOWN_HOURS` | `6` | Minimum gap between alerts per subscription |
| `DATABASE_URL` | `sqlite:///aurora.db` | SQLAlchemy database URL |
| `OPENTOPOGRAPHY_API_KEY` | _(optional)_ | Reserved for future SRTM horizon refinement |
| `WEIGHT_OVATION` | `1.0` | Factor weight exponent – OVATION probability |
| `WEIGHT_KP` | `0.5` | Factor weight exponent – Kp index |
| `WEIGHT_CLOUD` | `1.5` | Factor weight exponent – cloud cover |
| `WEIGHT_AOD` | `1.0` | Factor weight exponent – aerosol optical depth |
| `WEIGHT_ELEV` | `0.3` | Factor weight exponent – site elevation |
| `WEIGHT_MOON` | `0.5` | Factor weight exponent – lunar illumination |
| `WEIGHT_LP` | `0.5` | Factor weight exponent – light pollution |
| `WEIGHT_PWV` | `0.3` | Factor weight exponent – precipitable water |
| `WEIGHT_HORIZ` | `0.5` | Factor weight exponent – horizon elevation |

## Getting credentials

**Twilio**
1. Sign up at https://twilio.com
2. Get a phone number (trial accounts can text verified numbers for free)
3. Copy Account SID, Auth Token, and your Twilio number into `.env`

**Garmin inReach**: no account or key; see [Pairing an inReach](#pairing-an-inreach).

**OpenTopography** _(optional – reserved for a future higher-resolution horizon calculation)_
1. Register at https://portal.opentopography.org/requestApiKey
2. Add `OPENTOPOGRAPHY_API_KEY=<your key>` to `.env`
