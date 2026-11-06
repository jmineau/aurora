"""Alert text, built from the channel-neutral ``CheckResult.to_dict()`` payload.

Pure formatting (no I/O, no delivery).  Every channel gets the same compact
alert: one line of at most ``MAX_CHARS`` characters drawn from the
intersection of ASCII and the GSM-7 basic set, so it is a single SMS segment
(a degree sign or em-dash would force UCS-2 and halve the segment length) and
a single inReach satellite message (each one counts against the plan).

    Aurora alert, N Utah: 72% chance. Kp 6.3, cloud 10%, moon 20% down. 01:10-01:40 MST. Saw it? Reply Y or N

When too long, the place name is shortened first, then lower-priority details
are dropped; the Y/N prompt is always kept because the reply is a calibration
label.  The detailed factors (AOD, PWV, Bortle, horizon, poleward cloud) are not
sent; they are logged in every ``AlertLog`` snapshot.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from zoneinfo import ZoneInfo

MAX_CHARS = 160
REPLY_PROMPT = "Saw it? Reply Y or N"
ALERT_SUBJECT = "Aurora alert"

# GSM 03.38 basic character set (escape excluded).
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Safe for both SMS (one GSM-7 segment) and inReach (ASCII only).
_COMPACT_CHARS = frozenset(c for c in GSM7_BASIC if c.isascii() and c.isprintable())

# Characters NFKD can't reduce to ASCII but that have an obvious spelling.
_ASCII_SUBS = {
    "°": "deg",
    "—": "-",
    "–": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    "×": "x",
}


def to_compact_text(text: str, limit: int = MAX_CHARS) -> str:
    """Single line of ASCII ∩ GSM-7 basic characters, hard-capped at *limit*."""
    for src, dst in _ASCII_SUBS.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text)
    text = "".join(c for c in text if c in _COMPACT_CHARS).strip()
    return text[:limit].rstrip()


def _when(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    when = dt.datetime.fromisoformat(raw)
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _time_window(d: dict, tz_name: str | None) -> str:
    """'01:10-01:40 MST': from the check to the OVATION forecast's valid time.

    This is a nowcast, so the window is short; with no later forecast time it is
    just the check time.  Falls back to UTC ('08:10Z') if the zone is unknown.
    """
    start, end = _when(d.get("checked_at")), _when(d.get("forecast_time"))
    if start is None:
        return ""
    zone: dt.tzinfo = dt.timezone.utc
    if tz_name:
        try:
            zone = ZoneInfo(tz_name)
        except (KeyError, ValueError):
            tz_name = None
    start = start.astimezone(zone)
    label = start.strftime("%H:%M")
    if end is not None and end > start:
        label += "-" + end.astimezone(zone).strftime("%H:%M")
    return label + (f" {start.tzname()}" if tz_name else "Z")


def format_alert(address: str, d: dict, tz_name: str | None = None) -> str:
    """The compact alert (<= 160 chars, ASCII and GSM-7 only) for every channel.

    Priority when trimming: place name, chance (score/100 when uncalibrated),
    Kp, overhead cloud, moon, time window; the Y/N prompt always stays.
    """
    chance = (
        f"{d['visibility_score']:.0f}% chance"
        if d.get("calibrated")
        else f"score {d['visibility_score']:.0f}/100"
    )
    # Optional details, highest priority first.
    details: list[str] = []
    if d.get("kp_index") is not None:
        details.append(f"Kp {d['kp_index']:.1f}")
    if d.get("cloud_cover_pct") is not None:
        details.append(f"cloud {d['cloud_cover_pct']:.0f}%")
    if d.get("moon_illumination") is not None:
        alt = d.get("moon_altitude_deg")
        down = " down" if alt is not None and alt <= 0 else ""
        details.append(f"moon {d['moon_illumination'] * 100:.0f}%{down}")
    window = _time_window(d, tz_name)

    place = to_compact_text(address)
    while True:
        tail = f": {chance}."
        if details:
            tail += " " + ", ".join(details) + "."
        if window:
            tail += f" {window}."
        tail += f" {REPLY_PROMPT}"
        head = "Aurora alert"
        room = MAX_CHARS - len(head) - len(tail) - 2  # ", " before the place
        if room >= 4 or not (details or window):
            break
        # Even an empty place doesn't fit: drop the lowest-priority detail.
        if window:
            window = ""
        else:
            details.pop()
    if room < 4:
        place = ""
    elif len(place) > room:
        place = place[:room].rstrip(" ,.")
    text = f"{head}, {place}{tail}" if place else f"{head}{tail}"
    return to_compact_text(text)


_tf = None


def site_timezone(lat: float, lon: float) -> str | None:
    """IANA zone name at a site (timezonefinder: local data, no network)."""
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    return _tf.timezone_at(lat=lat, lng=lon)
