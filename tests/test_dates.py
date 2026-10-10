"""Time zones, camera and sidecar date agreement, and dates in file names."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from takeout_organizer.dates import (
    camera_offset,
    date_from_filename,
    has_location_lookup,
    offset_text,
    parse_timezone,
    to_local,
    zone_for_location,
)

TOKYO_NOON_UTC = 1686830400  # 2023-06-15 12:00:00 UTC


@pytest.mark.parametrize("text, expected", [
    ("+09:00", "+09:00"), ("-0530", "-05:30"), ("+5", "+05:00"), ("UTC", "+00:00"),
    ("UTC+05:45", "+05:45"),
])
def test_parse_timezone_offsets(text, expected):
    assert offset_text(to_local(TOKYO_NOON_UTC, parse_timezone(text))) == expected


def test_parse_timezone_names_and_errors():
    if os.name == "nt":
        pytest.importorskip("tzdata", reason="Windows has no zone database without tzdata")
    assert offset_text(to_local(TOKYO_NOON_UTC, parse_timezone("Asia/Tokyo"))) == "+09:00"
    with pytest.raises(ValueError, match="Unknown time zone"):
        parse_timezone("Mars/Olympus")
    with pytest.raises(ValueError, match="out of range"):
        parse_timezone("+15:00")


def test_dates_before_1970_convert_on_every_platform():
    # datetime.fromtimestamp raises for negative values on Windows.
    local = to_local(-157766400, parse_timezone("+00:00"))  # 1965-01-01
    assert local.year == 1965
    assert to_local(-157766400).year in (1964, 1965)  # this computer's zone


def test_camera_date_agreement():
    utc = 1686785445  # 2023-06-14 23:30:45 UTC
    tokyo = datetime(2023, 6, 15, 8, 30, 45)
    nine = timedelta(hours=9)
    assert camera_offset(tokyo, nine, utc) == nine            # offset tag agrees
    assert camera_offset(tokyo, None, utc) == nine            # offset inferred
    assert camera_offset(tokyo, timedelta(hours=-4), utc) is None
    assert camera_offset(tokyo.replace(second=50), None, utc) is None  # 5 s off any zone
    assert camera_offset(tokyo + timedelta(days=3), None, utc) is None  # edited date
    india = datetime(2023, 6, 15, 5, 0, 45)
    assert camera_offset(india, None, utc) == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize("name, expected, is_utc", [
    ("IMG_20230615_123456.jpg", datetime(2023, 6, 15, 12, 34, 56), False),
    ("VID_20230615_123456.mp4", datetime(2023, 6, 15, 12, 34, 56), False),
    ("PXL_20230615_123456789.jpg", datetime(2023, 6, 15, 12, 34, 56), True),
    ("Screenshot_2023-06-15-12-34-56.png", datetime(2023, 6, 15, 12, 34, 56), False),
    ("Screenshot_20230615-123456_Chrome.jpg", datetime(2023, 6, 15, 12, 34, 56), False),
    ("IMG-20230615-WA0001.jpg", datetime(2023, 6, 15), False),
])
def test_dates_in_file_names(name, expected, is_utc):
    assert date_from_filename(name) == (expected, is_utc)


@pytest.mark.parametrize("name", [
    "IMG_1234.jpg", "IMG_20231345_123456.jpg", "photo_99990101_000000.jpg", "DSC00042.JPG",
])
def test_no_date_in_file_name(name):
    assert date_from_filename(name) is None


def test_zone_for_gps_position():
    if not has_location_lookup():
        pytest.skip("GPS time zones are the optional [timezones] extra")
    for (lat, lon), expected in {
        (35.6762, 139.6503): "+09:00",   # Tokyo
        (28.6139, 77.2090): "+05:30",    # New Delhi
        (39.2904, -76.6122): "-04:00",   # Baltimore, daylight saving time
        (48.8566, 2.3522): "+02:00",     # Paris, summer time
    }.items():
        zone = zone_for_location(lat, lon)
        assert offset_text(to_local(TOKYO_NOON_UTC, zone)) == expected
