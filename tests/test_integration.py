"""End-to-end integration test that runs the REAL ExifTool binary.

Unlike the unit tests (which prove matching + dry-run planning without ExifTool),
this drives the full pipeline for real - writes EXIF into a genuine JPEG and reads
it back - so it actually proves metadata restoration works. Skipped automatically
when ExifTool isn't installed; runs in CI where it is.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from takeout_organizer.dates import parse_timezone
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.metadata_handler import MetadataHandler

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="ExifTool not installed"
)

PIL = pytest.importorskip("PIL.Image", reason="Pillow needed to make a test JPEG")


def test_end_to_end_writes_and_reads_back_exif(tmp_path):
    # A realistic already-extracted Takeout layout with one photo + its sidecar.
    src = tmp_path / "src" / "Takeout" / "Google Photos" / "Photos from 2023"
    src.mkdir(parents=True)
    PIL.new("RGB", (8, 8), (120, 30, 30)).save(src / "IMG.jpg", "JPEG")
    (src / "IMG.jpg.json").write_text(json.dumps({
        "title": "IMG.jpg",
        "photoTakenTime": {"timestamp": "1686839445"},          # 2023-06-15 (UTC)
        "geoData": {"latitude": 39.2904, "longitude": -76.6122, "altitude": 10.0},
    }))
    dest = tmp_path / "out"

    ok = MainProcessor(
        source_folder=str(tmp_path / "src"),
        dest_folder=str(dest),
        dry_run=False,
    ).run()
    assert ok, "pipeline reported failure"

    outputs = list(dest.rglob("*.jpg"))
    assert outputs, "expected an organized JPEG in the destination"

    # Read the EXIF back with the real ExifTool - the actual proof it was written.
    result = subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude", str(outputs[0])],
        capture_output=True, text=True, check=True,
    )
    tags = json.loads(result.stdout)[0]
    assert str(tags.get("DateTimeOriginal", "")).startswith("2023:06:15"), tags
    assert "39" in str(tags.get("GPSLatitude", "")), tags
    assert "76" in str(tags.get("GPSLongitude", "")), tags


def test_already_extracted_source_is_never_modified(tmp_path):
    """SAFETY guarantee: pointing --source at an unzipped export must not touch
    the originals (ExifTool writes to a working copy, not the source)."""
    src = tmp_path / "src" / "Takeout" / "Google Photos" / "Photos from 2023"
    src.mkdir(parents=True)
    img = src / "IMG.jpg"
    PIL.new("RGB", (8, 8), (10, 90, 200)).save(img, "JPEG")
    (src / "IMG.jpg.json").write_text(json.dumps({
        "title": "IMG.jpg",
        "photoTakenTime": {"timestamp": "1686839445"},
    }))
    before = hashlib.sha256(img.read_bytes()).hexdigest()

    ok = MainProcessor(
        source_folder=str(tmp_path / "src"),
        dest_folder=str(tmp_path / "out"),
        dry_run=False,
    ).run()
    assert ok

    after = hashlib.sha256(img.read_bytes()).hexdigest()
    assert before == after, "source file was modified - safety guarantee violated"


def _read_tags(path, *tags) -> dict:
    """Read tags back with the real ExifTool (UTF-8 safe for any filename)."""
    result = MetadataHandler()._run_exiftool([*tags, "-json", str(path)])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)[0]


def _zip_export(tmp_path, files: dict) -> Path:
    """Build takeout-001.zip holding Takeout/Google Photos/Photos from 2023/<files>."""
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for name, data in files.items():
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}", data)
    return src


def _jpeg_bytes(color=(120, 30, 30)) -> bytes:
    buf = io.BytesIO()
    PIL.new("RGB", (8, 8), color).save(buf, "JPEG")
    return buf.getvalue()


def _sidecar(title: str, **extra) -> str:
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": "1686839445"}, **extra})


def _mp4_bytes() -> bytes:
    """The smallest QuickTime file ExifTool will write to: ftyp plus moov/mvhd."""
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">IIIH", 1000, 0, 0x10000, 0x100)
               + bytes(10) + matrix + bytes(24) + struct.pack(">I", 2))
    return box(b"ftyp", b"isom\0\0\x02\0isommp41") + box(b"moov", mvhd)


def test_dry_run_then_real_run_writes_metadata(tmp_path):
    """Following the README (preview first, then run) must not lose the dates."""
    src = _zip_export(tmp_path, {
        "IMG.jpg": _jpeg_bytes(),
        "IMG.jpg.json": _sidecar("IMG.jpg", geoData={"latitude": 39.2904, "longitude": -76.6122}),
    })
    dest = tmp_path / "out"

    assert MainProcessor(str(src), str(dest), dry_run=True).run()
    assert not dest.exists()
    assert MainProcessor(str(src), str(dest)).run()

    outputs = list(dest.rglob("*.jpg"))
    assert len(outputs) == 1 and outputs[0].relative_to(dest).parts[0] == "2023"
    tags = _read_tags(outputs[0], "-DateTimeOriginal", "-GPSLatitude")
    assert str(tags.get("DateTimeOriginal", "")).startswith("2023:06:1"), tags
    assert "39" in str(tags.get("GPSLatitude", "")), tags


def test_rerun_into_same_destination_copies_nothing_new(tmp_path):
    src = _zip_export(tmp_path, {"IMG.jpg": _jpeg_bytes(), "IMG.jpg.json": _sidecar("IMG.jpg")})
    dest = tmp_path / "out"

    assert MainProcessor(str(src), str(dest)).run()
    assert MainProcessor(str(src), str(dest)).run()

    assert [p.name for p in dest.rglob("*.jpg")] == ["IMG.jpg"]


def test_format_exiftool_cannot_write_gets_an_xmp_sidecar(tmp_path):
    # ExifTool cannot write BMP: the photo is copied as it is, and its date and
    # place go in SCAN.bmp.xmp next to it.
    buf = io.BytesIO()
    PIL.new("RGB", (8, 8), (1, 2, 3)).save(buf, "BMP")
    src = _zip_export(tmp_path, {
        "SCAN.bmp": buf.getvalue(),
        "SCAN.bmp.json": _sidecar("SCAN.bmp", geoData={"latitude": 48.8566, "longitude": 2.3522}),
    })
    dest = tmp_path / "out"

    processor = MainProcessor(str(src), str(dest), time_zone="+02:00")
    assert processor.run()
    outputs = list(dest.rglob("*.bmp"))
    assert [p.name for p in outputs] == ["SCAN.bmp"]
    assert outputs[0].relative_to(dest).parts[:2] == ("2023", "06")  # the sidecar date
    assert processor.session.state["failed_files"] == []

    xmp = outputs[0].with_name("SCAN.bmp.xmp")
    tags = _read_tags(xmp, "-XMP-exif:DateTimeOriginal", "-XMP-exif:GPSLatitude")
    assert tags["DateTimeOriginal"] == "2023:06:15 16:30:45+02:00", tags
    assert "48 deg" in str(tags.get("GPSLatitude")), tags


def test_temp_folder_with_non_ansi_name_works(tmp_path, monkeypatch):
    # ExifTool's argument file lives in the temp folder. Its full path must not
    # cross the Windows command line, which cannot carry these characters.
    odd_temp = tmp_path / "\u65e5\u672c_temp"
    odd_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(odd_temp))
    photo = odd_temp / "IMG.jpg"
    PIL.new("RGB", (8, 8), (5, 5, 5)).save(photo, "JPEG")

    MetadataHandler().apply_metadata_to_image(str(photo), {"timestamp": 1686839445})
    assert str(_read_tags(photo, "-DateTimeOriginal").get("DateTimeOriginal", "")).startswith("2023")


def test_video_location_with_altitude_is_written(tmp_path):
    video = tmp_path / "VID.mp4"
    video.write_bytes(_mp4_bytes())
    MetadataHandler().apply_metadata_to_video(
        str(video), {"latitude": 35.6762, "longitude": 139.6503, "altitude": 40.0}
    )
    coords = str(_read_tags(video, "-Keys:GPSCoordinates").get("GPSCoordinates", ""))
    assert coords.startswith("35 deg 40'") and "139 deg 39'" in coords, coords


def test_non_ascii_caption_and_filename_survive(tmp_path):
    caption = "Caf\u00e9 in \u6771\u4eac\nsecond line"
    name = "\u65e5\u672c_IMG.jpg"
    src = _zip_export(tmp_path, {name: _jpeg_bytes(), f"{name}.json": _sidecar(name, description=caption)})
    dest = tmp_path / "out"

    assert MainProcessor(str(src), str(dest)).run()
    outputs = list(dest.rglob("*.jpg"))
    assert [p.name for p in outputs] == [name]
    tags = _read_tags(outputs[0], "-XMP-dc:Description", "-EXIF:ImageDescription",
                      "-IPTC:Caption-Abstract", "-DateTimeOriginal")
    assert tags.get("Description") == caption, tags
    assert tags.get("ImageDescription") == caption, tags
    assert "Caption-Abstract" not in tags, tags    # no IPTC block is added (MWG)
    assert str(tags.get("DateTimeOriginal", "")).startswith("2023:06:1"), tags


def test_a_photo_with_iptc_keeps_it_in_step_and_readable(tmp_path):
    # A camera or editor already wrote IPTC: the caption goes there too, as UTF-8.
    caption = "Caf\u00e9 in \u6771\u4eac"
    plain = tmp_path / "plain.jpg"
    plain.write_bytes(_jpeg_bytes())
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-IPTC:ObjectName=Old title",
                    str(plain)], check=True)
    src = _zip_export(tmp_path, {"IMG.jpg": plain.read_bytes(),
                                 "IMG.jpg.json": _sidecar("IMG.jpg", description=caption)})
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    photo = next(dest.rglob("IMG.jpg"))
    tags = _read_tags(photo, "-IPTC:Caption-Abstract", "-IPTC:ObjectName",
                      "-XMP-dc:Description")
    assert tags.get("Caption-Abstract") == caption, tags
    assert tags.get("ObjectName") == "Old title" and tags.get("Description") == caption


def _camera_jpeg(exif_ifd: dict) -> bytes:
    """A JPEG whose camera recorded the given Exif IFD tags (by number)."""
    exif = PIL.Exif()
    exif.get_ifd(0x8769).update(exif_ifd)
    buf = io.BytesIO()
    PIL.new("RGB", (8, 8), (40, 80, 120)).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_camera_date_from_abroad_is_kept(tmp_path):
    """B8: a Tokyo photo processed on a computer in another zone stayed 13 hours off."""
    # DateTimeOriginal (36867) and OffsetTimeOriginal (36881), set by the camera.
    jpeg = _camera_jpeg({36867: "2023:06:15 08:30:45", 36881: "+09:00"})
    src = _zip_export(tmp_path, {
        "IMG.jpg": jpeg,
        "IMG.jpg.json": json.dumps({
            "title": "IMG.jpg",
            "photoTakenTime": {"timestamp": "1686785445"},  # same moment, as UTC
            "geoData": {"latitude": 35.6762, "longitude": 139.6503},
        }),
    })
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), time_zone="America/New_York",
                         layout="year-month-day").run()

    outputs = list(dest.rglob("*.jpg"))
    assert outputs[0].relative_to(dest).parts[:3] == ("2023", "06", "15")
    tags = _read_tags(outputs[0], "-DateTimeOriginal", "-OffsetTimeOriginal", "-GPSLatitude")
    assert tags["DateTimeOriginal"] == "2023:06:15 08:30:45", tags
    assert tags["OffsetTimeOriginal"] == "+09:00", tags
    assert "35" in str(tags.get("GPSLatitude", "")), tags


def test_photo_without_camera_date_gets_the_zone_of_its_location(tmp_path):
    pytest.importorskip("tzfpy", reason="GPS time zones are the optional [timezones] extra")
    src = _zip_export(tmp_path, {
        "IMG.jpg": _jpeg_bytes(),
        "IMG.jpg.json": _sidecar("IMG.jpg", geoData={"latitude": 35.6762,
                                                    "longitude": 139.6503}),
    })  # 1686839445 = 2023-06-15 14:30:45 UTC
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), time_zone="-08:00").run()

    output = next(dest.rglob("*.jpg"))
    tags = _read_tags(output, "-DateTimeOriginal", "-OffsetTimeOriginal")
    assert tags["DateTimeOriginal"] == "2023:06:15 23:30:45", tags
    assert tags["OffsetTimeOriginal"] == "+09:00", tags


def test_video_dates_do_not_depend_on_the_computer_zone(tmp_path):
    video = tmp_path / "VID.mp4"
    video.write_bytes(_mp4_bytes())
    MetadataHandler(timezone=parse_timezone("+09:00")).apply_metadata_to_video(
        str(video), {"timestamp": 1686839445}
    )
    # Read QuickTime:CreateDate as stored (UTC), without QuickTimeUTC.
    tags = _read_tags(video, "-QuickTime:CreateDate", "-Keys:CreationDate")
    assert tags["CreateDate"] == "2023:06:15 14:30:45", tags
    assert tags["CreationDate"] == "2023:06:15 23:30:45+09:00", tags


def test_people_names_land_in_person_in_image(tmp_path):
    photo = tmp_path / "IMG.jpg"
    photo.write_bytes(_jpeg_bytes())
    MetadataHandler().apply_metadata_to_image(str(photo), {"people": ["Alice", "Bob"]})
    tags = _read_tags(photo, "-XMP-iptcExt:PersonInImage", "-IPTC:Keywords", "-XMP-dc:Subject")
    assert tags.get("PersonInImage") == ["Alice", "Bob"], tags
    assert "Keywords" not in tags and "Subject" not in tags, tags


def test_sidecarless_file_is_filed_by_the_date_in_its_name(tmp_path):
    src = _zip_export(tmp_path, {"IMG_20190704_101500.jpg": _jpeg_bytes()})
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), layout="year-month-day").run()
    output = next(dest.rglob("*.jpg"))
    assert output.relative_to(dest).parts[:3] == ("2019", "07", "04")


@pytest.mark.parametrize("fmt, ext, has_offset", [
    ("GIF", "gif", False),   # no EXIF block: the date goes to XMP, without an offset
    ("PNG", "png", True), ("WEBP", "webp", True), ("TIFF", "tif", True),
])
def test_dates_written_to_formats_besides_jpeg(tmp_path, fmt, ext, has_offset):
    image = tmp_path / f"IMG.{ext}"
    PIL.new("RGB", (8, 8), (9, 9, 9)).save(image, fmt)
    MetadataHandler(timezone=parse_timezone("+09:00")).apply_metadata_to_image(
        str(image), {"timestamp": 1686839445}
    )
    tags = _read_tags(image, "-DateTimeOriginal", "-OffsetTimeOriginal")
    assert str(tags.get("DateTimeOriginal")).startswith("2023:06:15 23:30:45"), tags
    assert (tags.get("OffsetTimeOriginal") == "+09:00") is has_offset, tags


def test_shared_album_zip_photos_are_kept(tmp_path):
    # An album's "Download all" ZIP has the photos at the top, no Takeout folders.
    src = _zip_export(tmp_path, {"MINE.jpg": _jpeg_bytes((200, 0, 0)),
                                 "MINE.jpg.json": _sidecar("MINE.jpg")})
    with zipfile.ZipFile(src / "Trip to Tokyo.zip", "w") as zf:
        zf.writestr("FRIEND_1.jpg", _jpeg_bytes((0, 200, 0)))
        zf.writestr("FRIEND_2.jpg", _jpeg_bytes((0, 0, 200)))
    with zipfile.ZipFile(src / "takeout-drive.zip", "w") as zf:
        zf.writestr("Takeout/Drive/report.pdf", b"%PDF-1.4")  # not Google Photos: skipped

    dest = tmp_path / "out"
    processor = MainProcessor(str(src), str(dest))
    assert processor.run()
    assert sorted(p.name for p in dest.rglob("*.jpg")) == ["FRIEND_1.jpg", "FRIEND_2.jpg",
                                                         "MINE.jpg"]
    assert not list(dest.rglob("*.pdf"))
    assert processor.summary["balanced"] and processor.summary["input"] == 3


def test_every_input_file_is_accounted_for(tmp_path):
    photo = _jpeg_bytes((10, 120, 30))
    src = _zip_export(tmp_path, {
        "IMG.jpg": photo, "IMG.jpg.json": _sidecar("IMG.jpg"),
        "IMG(1).jpg": photo,                                  # exact duplicate
        "SCAN.jfif": _jpeg_bytes((90, 90, 200)),              # JPEG the name lists miss
        "SCAN.jfif.json": _sidecar("SCAN.jfif"),
        "notes.txt": b"not a photo",                          # goes to _other/
        "metadata.json": json.dumps({"title": "Album"}),      # Google's, not counted
    })
    dest = tmp_path / "out"
    processor = MainProcessor(str(src), str(dest))
    assert processor.run()

    assert (dest / "_other" / "Photos from 2023" / "notes.txt").read_bytes() == b"not a photo"
    jfif = next(dest.rglob("SCAN.jfif"))
    assert jfif.relative_to(dest).parts[:2] == ("2023", "06")
    summary = processor.summary
    assert summary["balanced"]
    assert (summary["input"], summary["written"], summary["duplicates"], summary["other"],
            summary["failed"]) == (4, 2, 1, 1, 0)
    assert summary["date_sources"] == {"sidecar": 2}


def test_wrong_extension_is_reported_or_fixed(tmp_path):
    files = {"IMG_1.HEIC": _jpeg_bytes(), "IMG_1.HEIC.json": _sidecar("IMG_1.HEIC")}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    plain = MainProcessor(str(_zip_export(tmp_path / "a", files)), str(tmp_path / "out_a"))
    assert plain.run()
    kept = next((tmp_path / "out_a").rglob("IMG_1.HEIC"))
    assert kept.relative_to(tmp_path / "out_a").parts[:2] == ("2023", "06")
    assert plain.summary["without_metadata"] == 1
    assert "wrong extension" in plain.session.state["failed_files"][0]["error"]

    fixed = MainProcessor(str(_zip_export(tmp_path / "b", files)), str(tmp_path / "out_b"),
                          fix_extensions=True)
    assert fixed.run()
    output = next((tmp_path / "out_b").rglob("IMG_1.jpg"))
    assert str(_read_tags(output, "-DateTimeOriginal").get("DateTimeOriginal", "")) \
        .startswith("2023:06:15")
    assert fixed.summary["without_metadata"] == 0


def test_keep_json_puts_the_sidecar_next_to_the_photo(tmp_path):
    src = _zip_export(tmp_path, {"IMG.jpg": _jpeg_bytes(), "IMG.jpg.json": _sidecar("IMG.jpg")})
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), keep_json=True).run()
    photo = next(dest.rglob("IMG.jpg"))
    assert json.loads((photo.parent / "IMG.jpg.json").read_text())["title"] == "IMG.jpg"


def test_cli_exits_1_when_a_file_could_not_be_copied(tmp_path, monkeypatch):
    from takeout_organizer import file_processor
    from takeout_organizer.cli import main

    src = _zip_export(tmp_path, {"A.jpg": _jpeg_bytes((1, 1, 1)),
                                 "B.jpg": _jpeg_bytes((250, 250, 250))})
    # B.jpg cannot be put in place, whether it is moved or copied there.
    for name in ("copy_file_safe", "move_file_safe"):
        original = getattr(file_processor.FileProcessor, name)

        def but_fail_b(self, source, target, *rest, original=original):
            return False if source.endswith("B.jpg") else original(self, source, target, *rest)

        monkeypatch.setattr(file_processor.FileProcessor, name, but_fail_b)
    assert main(["run", "--source", str(src), "--dest", str(tmp_path / "out")]) == 1


def test_guessed_dates_go_to_undated(tmp_path):
    # No sidecar, no EXIF date, no date in the name: only the file time is left.
    src = _zip_export(tmp_path, {"DSC00042.jpg": _jpeg_bytes()})
    dest = tmp_path / "out"
    processor = MainProcessor(str(src), str(dest))
    assert processor.run()
    assert (dest / "_undated" / "DSC00042.jpg").exists()
    assert processor.summary["date_sources"] == {"file time": 1}
    assert any("in _undated/ (date is a guess)" in line and line.rstrip().endswith("1")
               for line in processor.summary_lines)


@pytest.mark.parametrize("layout, parts", [
    ("year-month-day", ("2023", "06", "15", "IMG.jpg")),
    ("year-month", ("2023", "06", "IMG.jpg")),
    ("year", ("2023", "IMG.jpg")),
    ("flat", ("IMG.jpg",)),
])
def test_layouts(tmp_path, layout, parts):
    src = _zip_export(tmp_path, {"IMG.jpg": _jpeg_bytes(), "IMG.jpg.json": _sidecar("IMG.jpg")})
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), layout=layout, time_zone="+00:00").run()
    assert next(dest.rglob("IMG.jpg")).relative_to(dest).parts == parts


def test_albums_become_playlists(tmp_path):
    photo = _jpeg_bytes((30, 60, 90))
    src = _zip_export(tmp_path, {"IMG.jpg": photo, "IMG.jpg.json": _sidecar("IMG.jpg")})
    with zipfile.ZipFile(src / "takeout-002.zip", "w") as zf:
        # Takeout's album folder holds another copy of the same photo.
        zf.writestr("Takeout/Google Photos/Paris trip/IMG.jpg", photo)
        zf.writestr("Takeout/Google Photos/Paris trip/IMG.jpg.json", _sidecar("IMG.jpg"))
        zf.writestr("Takeout/Google Photos/Paris trip/metadata.json",
                    json.dumps({"title": "Paris: day one"}))
    with zipfile.ZipFile(src / "Friends.zip", "w") as zf:
        zf.writestr("FRIEND.jpg", _jpeg_bytes((200, 10, 10)))

    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), time_zone="+00:00").run()

    albums = dest / "Albums"
    paris = (albums / "Paris_ day one.m3u8").read_text(encoding="utf-8").splitlines()
    assert paris == ["#EXTM3U", "../2023/06/IMG.jpg"]  # the kept copy, relative
    friends = (albums / "Friends.m3u8").read_text(encoding="utf-8").splitlines()
    assert len(friends) == 2 and friends[1].endswith("FRIEND.jpg")
    index = json.loads((albums / "albums.json").read_text(encoding="utf-8"))
    assert index["Paris: day one"] == ["2023/06/IMG.jpg"]
    assert len(list(dest.rglob("IMG.jpg"))) == 1  # the album copy was a duplicate

    other = tmp_path / "out_no_albums"
    assert MainProcessor(str(src), str(other), albums="off").run()
    assert not (other / "Albums").exists()
