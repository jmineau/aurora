"""Tests for the observation backfill importer (aurora.backfill).

Offline: CSV parsing and timezone->UTC resolution (timezonefinder is local data,
no network). The reconstruction fetchers hit the network and are covered by a
manual smoke run, not here.
"""

import datetime as dt

from aurora.backfill import ObsRow, parse_csv, resolve_utc


def _write(tmp_path, text):
    p = tmp_path / "obs.csv"
    p.write_text(text, encoding="utf-8")
    return p


class TestParseCsv:
    def test_parses_rows_and_types(self, tmp_path):
        csv = (
            "observed_at_local,lat,lon,place,saw,intensity,notes\n"
            "2026-07-03 23:00,41.68,-112.71,\"N Utah\",y,3,strong\n"
            "2026-01-13 22:00,,,\"Fairbanks, AK\",n,0,nothing\n"
        )
        rows = parse_csv(_write(tmp_path, csv))
        assert len(rows) == 2
        assert rows[0] == ObsRow("2026-07-03 23:00", 41.68, -112.71, "N Utah", True, 3, "strong")
        # Blank lat/lon -> None, place kept for geocoding.
        assert rows[1].lat is None and rows[1].place == "Fairbanks, AK"
        assert rows[1].saw is False

    def test_annotated_headers_and_comment_lines(self, tmp_path):
        # A legend comment above the header and inline legends in the header cells
        # must both parse (headers normalise to their leading word).
        csv = (
            "# intensity: 0=none 1=faint 2=glow 3=bright; log the nothing nights too\n"
            "observed_at_local (YYYY-MM-DD HH:MM),lat,lon,place,saw (y/n),intensity (0-3),notes\n"
            "2026-07-03 23:00,41.68,-112.71,,y,3,strong\n"
        )
        rows = parse_csv(_write(tmp_path, csv))
        assert len(rows) == 1
        assert rows[0].saw is True
        assert rows[0].intensity == 3
        assert rows[0].lat == 41.68

    def test_skips_example_and_blank_rows(self, tmp_path):
        csv = (
            "observed_at_local,lat,lon,place,saw,intensity,notes\n"
            "2026-07-03 23:00,41.68,-112.71,,y,3,EXAMPLE row - delete me\n"
            ",,,,,,\n"
            "2026-07-04 22:00,41.68,-112.71,,maybe,,\n"     # unparseable saw -> skip
            "2026-07-05 22:00,41.68,-112.71,,y,2,real\n"
        )
        rows = parse_csv(_write(tmp_path, csv))
        assert len(rows) == 1
        assert rows[0].observed_at_local == "2026-07-05 22:00"


class TestResolveUtc:
    def test_mountain_daylight_time(self):
        # 41.68,-112.71 is America/Denver; July -> MDT (UTC-6).
        # 23:00 local on Jul 3 -> 05:00 UTC on Jul 4.
        when = resolve_utc("2026-07-03 23:00", 41.680567, -112.707793)
        assert when == dt.datetime(2026, 7, 4, 5, 0)
        assert when.tzinfo is None  # naive UTC for the DB

    def test_winter_standard_time(self):
        # Same place in January -> MST (UTC-7).
        when = resolve_utc("2026-01-13 22:00", 41.680567, -112.707793)
        assert when == dt.datetime(2026, 1, 14, 5, 0)
