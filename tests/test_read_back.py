"""After a write, the metadata is read back from the file, so a photo counted
as written really holds its date, location and text."""

from __future__ import annotations

import json
import os
import shutil
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import exiftool
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.manifest import Manifest
from takeout_organizer.metadata_handler import MetadataHandler, missing_fields

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")

TOKYO = chr(0x6771) + chr(0x4EAC)
FULL = {"timestamp": 1686839445, "latitude": -33.8568, "longitude": 151.2153,
        "altitude": 12.5, "description": f"Opera House {TOKYO}", "keywords": ["trip"],
        "people": ["Aiko"], "rating": 5}

PHOTO_ARGS = ["-DateTimeOriginal=2023:06:15 23:30:45", "-OffsetTimeOriginal=+09:00",
              "-GPSLatitude=33.8568", "-GPSLatitudeRef=S", "-GPSLongitude=151.2153",
              "-Description=Opera", "-ImageDescription=Opera", "-Caption-Abstract=Opera",
              "-Keywords=trip", "-Subject=trip", "-XMP-iptcExt:PersonInImage=Aiko",
              "-Rating=5", "-SubSecTimeOriginal=", "/photos/IMG.jpg"]


# --- comparing what was written with what is in the file --------------------

def test_a_jpeg_holding_everything_passes():
    found = {"ExifIFD:DateTimeOriginal": "2023:06:15 23:30:45", "GPS:GPSLatitude": 33.8568,
             "GPS:GPSLongitude": 151.2153, "XMP-dc:Description": "Opera",
             "IFD0:ImageDescription": "Opera", "IPTC:Caption-Abstract": "Opera",
             "IPTC:Keywords": "trip", "XMP-dc:Subject": "trip",
             "XMP-iptcExt:PersonInImage": "Aiko", "XMP-xmp:Rating": 5}
    assert missing_fields(PHOTO_ARGS, found) == []


def test_a_format_that_keeps_everything_in_xmp_passes():
    # A GIF: no EXIF or IPTC, so every value lands in XMP, the latitude signed.
    found = {"XMP-exif:DateTimeOriginal": "2023:06:15 23:30:45",
             "XMP-exif:GPSLatitude": -33.8568, "XMP-exif:GPSLongitude": 151.2153,
             "XMP-dc:Description": "Opera", "XMP-pdf:Keywords": "trip",
             "XMP-dc:Subject": ["trip"], "XMP-iptcExt:PersonInImage": "Aiko",
             "XMP-xmp:Rating": 5}
    assert missing_fields(PHOTO_ARGS, found) == []


def test_what_is_not_in_the_file_is_named():
    found = {"ExifIFD:DateTimeOriginal": "2023:06:15 23:30:45", "GPS:GPSLatitude": 33.8568,
             "GPS:GPSLongitude": 151.2153, "IPTC:Caption-Abstract": "?????",
             "XMP-xmp:Rating": 5}
    assert missing_fields(PHOTO_ARGS, found) == ["description", "keywords", "people"]
    assert missing_fields(PHOTO_ARGS, {}) == [
        "date", "location", "description", "keywords", "people", "rating"]


def test_text_cut_to_the_iptc_limit_counts():
    caption = TOKYO * 400                       # 2400 bytes; IPTC keeps 2000
    args = [f"-Caption-Abstract={caption}", "/photos/IMG.webp"]
    # 666 characters fit, then half a character, which reads back as dots.
    assert missing_fields(args, {"IPTC:Caption-Abstract": caption[:666] + ".."}) == []
    assert missing_fields(args, {"IPTC:Caption-Abstract": "..."}) == ["description"]


def test_video_dates_compare_as_stored_in_utc():
    args = ["-QuickTime:CreateDate=2023:06:15 23:30:45+09:00",
            "-Keys:CreationDate=2023:06:15 23:30:45+09:00",
            "-Keys:GPSCoordinates=35.6586, 139.7454, 12.5", "/videos/VID.mp4"]
    found = {"QuickTime:CreateDate": "2023:06:15 14:30:45",
             "Keys:CreationDate": "2023:06:15 23:30:45+09:00",
             "Keys:GPSCoordinates": "35.6586 139.7454 12.5"}
    assert missing_fields(args, found) == []
    assert missing_fields(args, dict(found, **{"QuickTime:CreateDate": "2023:06:15 23:30:45"})
                          ) == ["date"]


# --- with ExifTool ------------------------------------------------------------

def _mp4_bytes() -> bytes:
    """The smallest QuickTime file ExifTool will write to: ftyp plus moov/mvhd."""
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">IIIH", 1000, 0, 0x10000, 0x100)
               + bytes(10) + matrix + bytes(24) + struct.pack(">I", 2))
    return box(b"ftyp", b"isom\0\0\x02\0isommp41") + box(b"moov", mvhd)


@pytest.fixture
def handler():
    handler = MetadataHandler(keep_open=True)
    yield handler
    handler.close()


@needs_exiftool
@pytest.mark.parametrize("ext", ["jpg", "png", "gif", "webp", "tif"])
def test_every_photo_format_holds_what_was_written(handler, tmp_path, ext):
    path = tmp_path / f"IMG.{ext}"
    Image.new("RGB", (16, 16), (90, 20, 20)).save(path)
    assert handler.apply_metadata_to_image(str(path), FULL)  # raises if anything is missing


@needs_exiftool
def test_long_text_cut_by_iptc_is_not_a_failure(handler, tmp_path):
    path = tmp_path / "IMG.jpg"
    Image.new("RGB", (16, 16)).save(path)
    assert handler.apply_metadata_to_image(
        str(path), {"description": TOKYO * 400, "keywords": [TOKYO * 30]})


@needs_exiftool
def test_a_video_holds_what_was_written(handler, tmp_path):
    path = tmp_path / "VID.mp4"
    path.write_bytes(_mp4_bytes())
    assert handler.apply_metadata_to_video(str(path), FULL)


@needs_exiftool
def test_a_value_missing_after_the_write_is_an_error(handler, tmp_path, monkeypatch):
    path = tmp_path / "IMG.jpg"
    Image.new("RGB", (16, 16)).save(path)
    real = handler.read_back

    def without_description(media_path, names):
        return {k: v for k, v in real(media_path, names).items()
                if not k.endswith(("Description", "Caption-Abstract"))}
    monkeypatch.setattr(handler, "read_back", without_description)
    with pytest.raises(RuntimeError, match="does not hold what was written for: description"):
        handler.apply_metadata_to_image(str(path), FULL)


# --- in a run ---------------------------------------------------------------------

def _export(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for name, color in (("A.jpg", (200, 0, 0)), ("B.jpg", (0, 0, 200))):
            image = tmp_path / name
            Image.new("RGB", (8, 8), color).save(image)
            zf.write(image, f"Takeout/Google Photos/Photos from 2023/{name}")
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}.json", json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": "1686839445"},
                 "description": "a day out"}))
    return src


@needs_exiftool
def test_a_photo_whose_tags_did_not_stick_is_copied_and_reported(tmp_path, monkeypatch):
    real = MetadataHandler.read_back

    def lost_on_b(self, media_path, names):
        return {} if os.path.basename(media_path) == "B.jpg" else real(self, media_path, names)
    monkeypatch.setattr(MetadataHandler, "read_back", lost_on_b)

    started = []
    real_start = exiftool.ExifToolProcess._start
    monkeypatch.setattr(exiftool.ExifToolProcess, "_start",
                        lambda self: (started.append(1), real_start(self))[1])

    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest))
    assert processor.run()  # the run completes; the photo is counted, not dropped
    assert processor.summary["written"] == 2 and processor.summary["without_metadata"] == 1
    assert sorted(p.name for p in dest.rglob("*.jpg")
                  if ".takeout-organizer" not in p.parts) == ["A.jpg", "B.jpg"]
    manifest = Manifest(processor.manifest_path)
    try:
        errors = {Path(r["rel_path"]).name: r["error"] for r in manifest.files(processor.run_id)}
    finally:
        manifest.close()
    assert errors["A.jpg"] is None
    assert "does not hold what was written for: date, description" in errors["B.jpg"]
    assert len(started) == 1  # reading back uses the run's one ExifTool process
