"""The compact alert sent on every channel: at most 160 characters, GSM-7 basic
and ASCII only (one SMS segment, one inReach message), Y/N prompt always kept.
"""

import pytest

from aurora import messages

D = {
    "lat": 41.7,
    "lon": -111.8,
    "visibility_score": 72.4,
    "calibrated": True,
    "ovation_probability": 40.0,
    "aurora_elevation_deg": 14.6,
    "kp_index": 6.33,
    "cloud_cover_pct": 30.0,
    "cloud_poleward_pct": 4.0,
    "aod_550nm": 0.12,
    "pwv_mm": 8.0,
    "moon_illumination": 0.42,
    "moon_altitude_deg": -5.0,
    "bortle": 4.0,
    "horizon_deg": 2.1,
    "forecast_time": "2026-11-06T08:40:00+00:00",
    "checked_at": "2026-11-06T08:10:00+00:00",
}


def _ok(text: str) -> None:
    assert len(text) <= messages.MAX_CHARS
    assert text.isascii()
    bad = {c for c in text if c not in messages.GSM7_BASIC}
    assert not bad, f"non-GSM-7 characters force UCS-2: {bad}"
    assert text.endswith(messages.REPLY_PROMPT)


def test_example_line():
    text = messages.format_alert("N Utah", D, "America/Denver")
    assert text == (
        "Aurora alert, N Utah: 72% chance. Kp 6.3, cloud 30%, moon 42% down. "
        "01:10-01:40 MST. Saw it? Reply Y or N"
    )
    _ok(text)


def test_uncalibrated_moon_up_utc_fallback():
    d = {**D, "calibrated": False, "moon_altitude_deg": 20.0}
    text = messages.format_alert("N Utah", d, None)
    assert text == (
        "Aurora alert, N Utah: score 72/100. Kp 6.3, cloud 30%, moon 42%. "
        "08:10-08:40Z. Saw it? Reply Y or N"
    )
    _ok(text)


def test_detailed_factors_not_sent():
    text = messages.format_alert("N Utah", D, "America/Denver")
    for absent in ("AOD", "PWV", "Bortle", "orizon", "toward"):
        assert absent not in text


def test_unknown_timezone_and_stale_forecast():
    d = {**D, "forecast_time": "2026-11-06T08:00:00+00:00"}
    assert " 08:10Z. " in messages.format_alert("x", d, "Not/AZone")


def test_long_unicode_place_is_cut_first_prompt_kept():
    place = "Parc national de la Jacques-Cartier, Québec — « réserve faunique » [north] " * 4
    text = messages.format_alert(place, D, "America/Toronto")
    _ok(text)
    assert text.startswith("Aurora alert, Parc national de la Jacques-Cartier, Quebec")
    # Every detail survives; only the place was shortened.
    for piece in ("72% chance", "Kp 6.3", "cloud 30%", "moon 42% down", "EST"):
        assert piece in text


@pytest.mark.parametrize("missing", ["kp_index", "cloud_cover_pct", "moon_illumination", "checked_at"])
def test_missing_fields_are_skipped(missing):
    d = {k: v for k, v in D.items() if k != missing}
    _ok(messages.format_alert("x", d, "America/Denver"))


@pytest.mark.parametrize("n", [0, 50, 159, 160, 161, 1000])
def test_hard_cap(n):
    assert len(messages.to_compact_text("é" * n)) <= 160


def test_to_compact_text():
    out = messages.to_compact_text("Kp 6 — at 15°\n“bright” 🌌 Montréal… {x} [y] ~|^`\\")
    assert out == 'Kp 6 - at 15deg "bright" Montreal... x y'
    assert all(c in messages.GSM7_BASIC for c in out)


def test_main_format_alert_is_the_compact_formatter():
    import aurora.main as main

    assert main._format_alert("x", D, "America/Denver") == messages.format_alert("x", D, "America/Denver")
