# Aurora Alert Server

A Python server that texts you when there's a good chance of seeing the aurora
borealis at one or more locations.  Conditions are evaluated on a configurable
schedule using a physics-motivated composite visibility model that goes well
beyond simple geomagnetic indices.

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
- A [Twilio](https://console.twilio.com) account with a phone number

### 2. Install

```bash
git clone https://github.com/jmineau/aurora
cd aurora
uv sync --extra dev        # installs all deps + pytest
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env – at minimum fill in the three Twilio variables
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

The server starts on `http://localhost:8000`.  Interactive API docs are at
`http://localhost:8000/docs`.

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

`threshold` is the minimum visibility score (0–100) required to trigger an
alert.  Sensible starting points: 25 for high-latitude sites, 40 for
mid-latitudes.  Once a calibration has been fitted (see below) the score becomes
a calibrated **percent chance of seeing the aurora**, so the threshold reads
directly as "don't text me unless I'm at least this % likely to see it".

### Unsubscribe

```http
DELETE /unsubscribe/+12125551234
```

### List subscriptions

```http
GET /subscriptions/+12125551234
```

### Ad-hoc condition check

```http
GET /check?lat=64.2&lon=-21.9
```

Returns the current score and all factor values for the coordinate.

### Health

```http
GET /health
```

## .env configuration reference

| Variable | Default | Description |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | required | From the Twilio Console |
| `TWILIO_AUTH_TOKEN` | required | From the Twilio Console |
| `TWILIO_FROM_NUMBER` | required | Your Twilio number in E.164 format |
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

**OpenTopography** _(optional – reserved for a future higher-resolution horizon calculation)_
1. Register at https://portal.opentopography.org/requestApiKey
2. Add `OPENTOPOGRAPHY_API_KEY=<your key>` to `.env`
