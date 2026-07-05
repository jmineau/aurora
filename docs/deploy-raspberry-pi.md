# Deploying on a Raspberry Pi (with Tailscale)

A Pi is a good home for this: it runs the scheduler + API continuously, keeps a
local SQLite database, and costs nothing to leave on. This guide gets it running
as a `systemd` service that survives reboots, reachable over your tailnet, with an
optional public URL for the Twilio reply webhook.

Assumes a 64-bit Raspberry Pi OS (Bookworm or later) on a Pi 4/5, with Tailscale
already installed and logged in. Replace `pi` / `/home/pi` below if your user or
path differs.

## 1. Install

```bash
# uv (installs to ~/.local/bin)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/jmineau/aurora
cd aurora
uv sync                      # numpy/scipy/pillow have ARM wheels — no compiling
```

## 2. Configure

```bash
cp .env.example .env
nano .env
```

Fill in at least the three Twilio variables (the app won't start without them).
Recommended settings for a long-running Pi service:

```ini
DATABASE_URL=sqlite:////home/pi/aurora/aurora.db   # absolute path (note 4 slashes)
CHECK_INTERVAL_MINUTES=15                            # OVATION updates ~every 30 min
TWILIO_VALIDATE_SIGNATURE=true                       # if you expose the webhook (step 6)
```

## 3. Build the light-pollution raster (one-time)

```bash
uv run python data/download_bortle.py
```

~25 MB written to `data/bortle.npy`. The server runs without it (defaults to
Bortle 4) but light pollution is then a constant, so build it.

## 4. Run as a systemd service

Create `/etc/systemd/system/aurora.service`:

```ini
[Unit]
Description=Aurora alert server
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/aurora
ExecStart=/home/pi/.local/bin/uv run aurora-server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` matters: the `.env`, the SQLite DB, and `geocode_cache.pkl` are
all resolved relative to it. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aurora
journalctl -u aurora -f          # watch it start and run check cycles
```

You should see "Aurora alert server started" and, each cycle, one log line per
active subscription with its score.

## 5. Access it over Tailscale

The server listens on `0.0.0.0:8000`, so it's reachable at your Pi's tailnet name
from any device on your tailnet — no ports opened to the internet:

```bash
curl http://<pi-name>.<tailnet>.ts.net:8000/health
```

Subscribe your dark-sky spot (the July 3 site):

```bash
curl -X POST http://<pi-name>.<tailnet>.ts.net:8000/subscribe \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+1XXXXXXXXXX","locations":["41.680567,-112.707793"],"threshold":30}'
```

From now on it logs a conditions snapshot for that location every cycle — the
feature store the calibration trains on — and texts you when the score clears your
threshold.

## 6. (Optional) Public webhook for one-tap Y/N replies

Twilio's servers are on the public internet and can't reach a tailnet address, so
the inbound-SMS webhook (`/sms/inbound`) needs a public URL. **Tailscale Funnel**
exposes just that one service publicly over HTTPS, with TLS handled for you:

```bash
sudo tailscale funnel 8000        # serve port 8000 on https://<pi>.<tailnet>.ts.net
tailscale funnel status
```

Then in the Twilio console, set the phone number's **A MESSAGE COMES IN** webhook
to `https://<pi>.<tailnet>.ts.net/sms/inbound` (POST). Keep
`TWILIO_VALIDATE_SIGNATURE=true` so only genuine Twilio requests are accepted.

Don't want any public exposure yet? Skip this. You can still log sightings by
POSTing to `/report` over the tailnet, or by adding rows to
`data/observations.csv` (see [collecting-observations.md](collecting-observations.md)).

## 7. Operating it

```bash
sudo systemctl restart aurora     # after editing .env
sudo systemctl status aurora
journalctl -u aurora --since "1 hour ago"

# update to latest code
cd /home/pi/aurora && git pull && uv sync && sudo systemctl restart aurora

# after fitting a calibration, the running server picks it up on restart
uv run aurora-calibrate && sudo systemctl restart aurora
```

The SQLite DB, `geocode_cache.pkl`, and `data/observations.csv` are your data —
back them up (e.g. periodic copy to another tailnet machine). They're gitignored,
so `git pull` never touches them.
