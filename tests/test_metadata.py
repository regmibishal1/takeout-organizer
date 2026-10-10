"""Unit tests for metadata extraction and the ExifTool command it builds.

No ExifTool binary needed - the subprocess call is captured so we can assert the
exact arguments (dates in local time, GPS hemispheres, overwrite flag).
"""

from __future__ import annotations

import json
import os
import time

import pytest

import takeout_organizer.metadata_handler as mh
from takeout_organizer.dates import has_location_lookup, offset_text, parse_timezone, to_local
from takeout_organizer.metadata_handler import MetadataHandler, _local_datestr


def _capture_exiftool_args(monkeypatch, stderr: str = "") -> dict:
    """Patch subprocess.run and return a dict that gets ['args'] on the next call.

    ExifTool receives its arguments through a UTF-8 argument file (`-@ FILE`),
    so the captured list is the lines of that file.
    """
    box: dict = {}

    class _Result:
        returncode = 0
        stdout = ""

    _Result.stderr = stderr

    def fake_run(args, **kwargs):
        argfile = os.path.join(kwargs.get("cwd") or "", args[args.index("-@") + 1])
        with open(argfile, encoding="utf-8") as f:
            lines = f.read().splitlines()
        if lines[:1] == ["-json"]:
            return _Result()  # the read-back after a write (tested on its own)
        box["args"] = lines
        box["command"] = args
        return _Result()

    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    monkeypatch.setattr(mh, "missing_fields", lambda args, found: [])
    return box


def test_extract_parses_google_takeout_json(tmp_path):
    path = tmp_path / "IMG.jpg.json"
    path.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1686839445"},
        "geoData": {"latitude": 39.2904, "longitude": -76.6122, "altitude": 10.0},
        "description": "Beach day",
        "favorited": True,
        "people": [{"name": "Alex"}],
    }))
    meta = MetadataHandler(require=False).extract_metadata_from_json(str(path))
    assert meta["timestamp"] == 1686839445
    assert meta["latitude"] == 39.2904 and meta["longitude"] == -76.6122
    assert meta["description"] == "Beach day"
    assert meta["rating"] == 5 and meta["people"] == ["Alex"]


def test_no_gps_when_coordinates_are_zero(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"geoData": {"latitude": 0.0, "longitude": 0.0}}))
    meta = MetadataHandler(require=False).extract_metadata_from_json(str(path))
    assert "latitude" not in meta and "longitude" not in meta


def test_datetime_without_gps_written_in_computer_zone_with_offset(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/photos/IMG.jpg", {"timestamp": 1686839445}
    )
    local = to_local(1686839445)  # this computer's zone - deterministic on any host
    args = box["args"]
    assert f"-MWG:DateTimeOriginal={_local_datestr(1686839445)}{offset_text(local)}" in args
    assert f"-OffsetTimeOriginal={offset_text(local)}" in args
    assert "-use" in args and "MWG" in args
    # ModifyDate means "last edited"; it is no longer set to the capture time.
    assert not any(a.startswith(("-AllDates", "-ModifyDate")) for a in args)


def test_timezone_option_sets_local_time_and_offset(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False,
                    timezone=parse_timezone("+05:30")).apply_metadata_to_image(
        "/photos/IMG.jpg", {"timestamp": 1686839445}  # 14:30:45 UTC
    )
    args = box["args"]
    assert "-MWG:DateTimeOriginal=2023:06:15 20:00:45+05:30" in args
    assert "-OffsetTimeOriginal=+05:30" in args
    assert "-FileModifyDate=2023:06:15 20:00:45+05:30" in args


def test_gps_hemispheres_and_overwrite_flag(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/photos/IMG.jpg", {"latitude": 39.29, "longitude": -76.61, "altitude": 10.0}
    )
    args = box["args"]
    assert "-overwrite_original" in args
    assert "-GPSLatitude=39.29" in args and "-GPSLatitudeRef=N" in args
    assert "-GPSLongitude=76.61" in args and "-GPSLongitudeRef=W" in args
    assert args[-1] == os.path.abspath("/photos/IMG.jpg")


def _extract(tmp_path, payload: dict) -> dict:
    path = tmp_path / "x.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return MetadataHandler(require=False).extract_metadata_from_json(str(path))


def test_impossible_sidecar_dates_are_rejected(tmp_path):
    future = int(time.time()) + 3 * 24 * 3600
    cases = {"0": "zero", "-5000000000": "too_old", str(future): "future", "soon": "invalid"}
    for raw, reason in cases.items():
        meta = _extract(tmp_path, {"photoTakenTime": {"timestamp": raw}})
        assert "timestamp" not in meta, raw
        assert meta["date_rejected"] == reason, raw


def test_scanned_photo_from_before_1970_is_kept(tmp_path):
    meta = _extract(tmp_path, {"photoTakenTime": {"timestamp": "-157766400"}})  # 1965
    assert meta["timestamp"] == -157766400


def test_upload_date_used_only_when_capture_date_missing(tmp_path):
    meta = _extract(tmp_path, {"creationTime": {"timestamp": "1686839445"}})
    assert meta == {"upload_timestamp": 1686839445}

    meta = _extract(tmp_path, {
        "photoTakenTime": {"timestamp": "0"},
        "creationTime": {"timestamp": "1686839445"},
    })
    assert meta["upload_timestamp"] == 1686839445 and "timestamp" not in meta

    meta = _extract(tmp_path, {
        "photoTakenTime": {"timestamp": "1500000000"},
        "creationTime": {"timestamp": "1686839445"},
    })
    assert meta["timestamp"] == 1500000000 and "upload_timestamp" not in meta


def test_upload_date_sets_file_times_but_not_capture_date(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/photos/IMG.jpg", {"upload_timestamp": 1686839445}
    )
    args = box["args"]
    local = to_local(1686839445)
    assert f"-FileModifyDate={_local_datestr(1686839445)}{offset_text(local)}" in args
    assert not any(a.startswith(("-DateTimeOriginal", "-AllDates")) for a in args)


def test_geodataexif_used_when_geodata_is_empty(tmp_path):
    meta = _extract(tmp_path, {
        "geoData": {"latitude": 0.0, "longitude": 0.0},
        "geoDataExif": {"latitude": 48.8566, "longitude": 2.3522, "altitude": 35.0},
    })
    assert (meta["latitude"], meta["longitude"], meta["altitude"]) == (48.8566, 2.3522, 35.0)


def test_video_gps_is_comma_separated_with_altitude(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_video(
        "/videos/VID.mp4", {"latitude": 35.6762, "longitude": 139.6503, "altitude": 40.0}
    )
    assert "-Keys:GPSCoordinates=35.6762, 139.6503, 40.0" in box["args"]


def test_exiftool_warning_that_drops_a_tag_is_a_failure(monkeypatch):
    _capture_exiftool_args(
        monkeypatch,
        stderr="Warning: Error converting value for Keys:GPSCoordinates (PrintConvInv)\n",
    )
    handler = MetadataHandler(exiftool_path="exiftool", require=False)
    with pytest.raises(RuntimeError, match="Error converting value"):
        handler.apply_metadata_to_video("/videos/VID.mp4", {"latitude": 1.0, "longitude": 2.0})


def test_file_create_date_only_where_it_exists(monkeypatch):
    for supported in (True, False):
        monkeypatch.setattr(mh, "_writes_file_create_date", lambda s=supported: s)
        box = _capture_exiftool_args(monkeypatch)
        MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
            "/photos/IMG.bmp", {"timestamp": 1686839445}
        )
        assert any(a.startswith("-FileCreateDate=") for a in box["args"]) is supported


def test_text_is_passed_as_utf8_with_iptc_charset(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/photos/IMG.jpg", {"description": "caf\u00e9 \u65e5\u672c\nsecond line"}
    )
    args = box["args"]
    assert "-charset" in box["command"] and "filename=utf8" in box["command"]
    # A file without IPTC gets none: the caption goes to EXIF and XMP.
    assert "iptc=UTF8" in args and "-IPTC:CodedCharacterSet=UTF8" not in args
    # Multi-line text goes in a C-string line, with the newline escaped.
    assert "#[CSTR]-MWG:Description=caf\u00e9 \u65e5\u672c\\nsecond line" in args


def test_a_file_that_has_iptc_gets_its_text_marked_utf8(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/photos/IMG.jpg", {"description": "caf\u00e9"},
        existing={"CurrentIPTCDigest": "d41d8cd98f00b204e9800998ecf8427e"})
    assert "-IPTC:CodedCharacterSet=UTF8" in box["args"]


def test_nothing_to_write_skips_exiftool(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    assert MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/photos/IMG.jpg", {"title": "IMG.jpg", "date_rejected": "zero"}
    )
    assert "args" not in box


def _handler_with_existing(monkeypatch, existing: dict, **options) -> MetadataHandler:
    handler = MetadataHandler(exiftool_path="exiftool", require=False, **options)
    monkeypatch.setattr(handler, "read_existing", lambda path: dict(existing))
    return handler


TOKYO_CAMERA = {"DateTimeOriginal": "2023:06:15 08:30:45", "OffsetTimeOriginal": "+09:00",
                "SubSecTimeOriginal": 123}
SAME_MOMENT = 1686785445  # 2023-06-14 23:30:45 UTC = 08:30:45 in Tokyo


def test_camera_date_of_the_same_moment_is_kept(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, TOKYO_CAMERA).apply_metadata_to_image(
        "/photos/IMG.jpg", {"timestamp": SAME_MOMENT}
    )
    args = box["args"]
    assert not any(a.startswith(("-DateTimeOriginal", "-CreateDate", "-SubSec")) for a in args)
    assert "-FileModifyDate=2023:06:15 08:30:45+09:00" in args


def test_camera_date_without_offset_gets_the_inferred_offset(monkeypatch):
    camera = {"DateTimeOriginal": "2023:06:15 08:30:45"}
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, camera).apply_metadata_to_image(
        "/photos/IMG.jpg", {"timestamp": SAME_MOMENT}
    )
    assert "-OffsetTimeOriginal=+09:00" in box["args"]
    assert not any(a.startswith("-DateTimeOriginal") for a in box["args"])


def test_date_corrected_in_google_photos_wins_by_default(monkeypatch):
    corrected = SAME_MOMENT + 3 * 24 * 3600
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, TOKYO_CAMERA).apply_metadata_to_image(
        "/photos/IMG.jpg", {"timestamp": corrected}
    )
    args = box["args"]
    # Written in the camera's zone, and the stale sub-seconds are cleared.
    assert "-MWG:DateTimeOriginal=2023:06:18 08:30:45+09:00" in args
    assert "-OffsetTimeOriginal=+09:00" in args
    assert "-SubSecTimeOriginal=" in args

    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, TOKYO_CAMERA, date_policy="camera-first") \
        .apply_metadata_to_image("/photos/IMG.jpg", {"timestamp": corrected})
    assert not any(a.startswith("-DateTimeOriginal") for a in box["args"])


def test_camera_gps_is_kept_unless_asked(monkeypatch):
    camera = {"GPSLatitude": 35.0, "GPSLongitude": 139.0}
    google = {"latitude": 48.8566, "longitude": 2.3522}
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, camera).apply_metadata_to_image("/p/IMG.jpg", google)
    assert "args" not in box  # nothing left to write, so ExifTool is not even run

    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, camera, overwrite_gps=True) \
        .apply_metadata_to_image("/p/IMG.jpg", google)
    assert "-GPSLatitude=48.8566" in box["args"]


def test_people_go_to_person_in_image(monkeypatch):
    meta = {"people": ["Alice", "Bob"], "keywords": ["beach"]}
    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False).apply_metadata_to_image(
        "/p/IMG.jpg", meta)
    args = box["args"]
    assert "-XMP-iptcExt:PersonInImage=Alice" in args and "-XMP-iptcExt:PersonInImage=Bob" in args
    assert "-MWG:Keywords=beach" in args and "-MWG:Keywords=Alice" not in args

    box = _capture_exiftool_args(monkeypatch)
    MetadataHandler(exiftool_path="exiftool", require=False,
                    people_as_keywords=True).apply_metadata_to_image("/p/IMG.jpg", meta)
    assert "-MWG:Keywords=Alice" in box["args"]


def test_video_keeps_matching_quicktime_date_and_adds_zone(monkeypatch):
    stored = {"CreateDate": "2023:06:14 23:30:45"}  # QuickTime stores UTC
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, stored, timezone=parse_timezone("+09:00")) \
        .apply_metadata_to_video("/v/VID.mp4", {"timestamp": SAME_MOMENT})
    args = box["args"]
    assert not any(a.startswith(("-QuickTime:CreateDate", "-TrackCreateDate")) for a in args)
    assert "-Keys:CreationDate=2023:06:15 08:30:45+09:00" in args


def test_video_dates_are_zoned_not_machine_local(monkeypatch):
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, {}, timezone=parse_timezone("+09:00")) \
        .apply_metadata_to_video("/v/VID.mp4", {"timestamp": SAME_MOMENT})
    args = box["args"]
    assert "-QuickTime:CreateDate=2023:06:15 08:30:45+09:00" in args
    assert not any(a.startswith(("-ModifyDate", "-TrackModifyDate", "-AllDates")) for a in args)


def test_camera_gps_of_zero_zero_means_no_location(monkeypatch):
    camera = {"GPSLatitude": 0.0, "GPSLongitude": 0.0}
    paris = {"timestamp": 1686839445, "latitude": 48.8566, "longitude": 2.3522}
    box = _capture_exiftool_args(monkeypatch)
    _handler_with_existing(monkeypatch, camera).apply_metadata_to_image("/p/IMG.jpg", paris)
    args = box["args"]
    assert "-GPSLatitude=48.8566" in args
    if has_location_lookup():  # zone from Paris, not from 0,0 (UTC+0)
        assert "-OffsetTimeOriginal=+02:00" in args
