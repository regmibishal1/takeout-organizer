"""What a run leaves in the library, dates found outside a sidecar, and the
notes that help when something is off."""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli
from takeout_organizer.main_processor import WORK_DIR_PREFIX, MainProcessor
from takeout_organizer.metadata_handler import MetadataHandler
from takeout_organizer.preflight import counted

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
G = "Takeout/Google Photos"


def _jpeg(tmp_path, color) -> bytes:
    path = tmp_path / "tmp.jpg"
    Image.new("RGB", (8, 8), color).save(path)
    return path.read_bytes()


def _mp4() -> bytes:
    def box(kind, payload):
        return struct.pack(">I", 8 + len(payload)) + kind + payload
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">IIIH", 1000, 0, 0x10000, 0x100)
               + bytes(10) + matrix + bytes(24) + struct.pack(">I", 2))
    return box(b"ftyp", b"isom\0\0\x02\0isommp41") + box(b"moov", mvhd)


def _export(tmp_path, files: dict) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for name, data in files.items():
            zf.writestr(f"{G}/{name}", data)
    return src


def _sidecar(title, ts="1686839445"):
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": ts}})


def _read(path, *tags):
    handler = MetadataHandler(keep_open=True)
    try:
        return handler.read_back(str(path), list(tags))
    finally:
        handler.close()


# --- dates found outside a sidecar -------------------------------------------

@needs_exiftool
def test_a_date_in_the_file_name_is_written_into_the_photo(tmp_path):
    src = _export(tmp_path, {"Photos from 2019/IMG_20190704_101500.jpg": _jpeg(tmp_path, "red")})
    dest = tmp_path / "out"
    processor = MainProcessor(str(src), str(dest))
    assert processor.run()
    photo = dest / "2019" / "07" / "IMG_20190704_101500.jpg"
    found = _read(photo, "DateTimeOriginal", "OffsetTimeOriginal")
    assert found["ExifIFD:DateTimeOriginal"] == "2019:07:04 10:15:00"
    assert "ExifIFD:OffsetTimeOriginal" not in found   # a name gives no zone
    assert processor.summary["date_sources"] == {"filename": 1}


@needs_exiftool
def test_a_date_in_the_file_name_is_written_into_a_video(tmp_path):
    src = _export(tmp_path, {"Photos from 2019/VID_20190704_101500.mp4": _mp4()})
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest), time_zone="+09:00").run()
    video = dest / "2019" / "07" / "VID_20190704_101500.mp4"
    found = _read(video, "QuickTime:CreateDate", "Keys:CreationDate")
    assert found["QuickTime:CreateDate"] == "2019:07:04 01:15:00"     # stored in UTC
    assert found["Keys:CreationDate"] == "2019:07:04 10:15:00+09:00"


@needs_exiftool
def test_a_video_without_a_sidecar_is_filed_by_its_own_date(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(_mp4())
    subprocess.run(["exiftool", "-q", "-overwrite_original",
                    "-QuickTime:CreateDate=2021:03:04 05:06:07", str(video)], check=True)
    src = _export(tmp_path, {"Shared/clip.mp4": video.read_bytes()})
    dest = tmp_path / "out"
    processor = MainProcessor(str(src), str(dest), time_zone="+00:00")
    assert processor.run()
    assert (dest / "2021" / "03" / "clip.mp4").exists()
    assert processor.summary["date_sources"] == {"exif": 1}


# --- what a run leaves in the library ----------------------------------------

@needs_exiftool
def test_the_library_holds_photos_and_one_report_and_logs_are_tucked_away(tmp_path):
    src = _export(tmp_path, {"Photos from 2023/A.jpg": _jpeg(tmp_path, "red"),
                             "Photos from 2023/A.jpg.json": _sidecar("A.jpg")})
    dest = tmp_path / "out"
    for _ in range(2):  # a second run, often in the same second, must not add files here
        processor = MainProcessor(str(src), str(dest))
        assert processor.run()
    top = sorted(p.name for p in dest.iterdir())
    assert top == [".takeout-organizer", "2023", "takeout-organizer-report.html"]
    logs = dest / ".takeout-organizer" / "logs"
    assert Path(processor.log_file).parent == logs
    logs_written = sorted(p.stem for p in logs.glob("run-*.log"))
    assert len(logs_written) == 2
    assert sorted(p.stem for p in logs.glob("run-*.md")) == logs_written  # same names


def test_a_preview_counts_the_xmp_sidecars_a_run_will_write(tmp_path, monkeypatch):
    monkeypatch.setattr(MetadataHandler, "can_write",
                        lambda self, path: not path.lower().endswith(".bmp"))
    bmp = tmp_path / "scan.bmp"
    Image.new("RGB", (8, 8), "blue").save(bmp)
    src = _export(tmp_path, {"Photos from 2022/scan.bmp": bmp.read_bytes(),
                             "Photos from 2022/scan.bmp.json": _sidecar("scan.bmp")})
    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
    assert processor.run()
    assert any("metadata in an .xmp sidecar" in line and line.rstrip().endswith(" 1")
               for line in processor.summary_lines)


# --- notes that help when something is off -----------------------------------

def test_sidecars_whose_photo_is_not_in_the_export_are_reported(tmp_path, caplog):
    long_name = "Screenshot_20230615-123456_A_Very_Long_Application_Name.png"
    src = _export(tmp_path, {
        "Photos from 2023/A.jpg": _jpeg(tmp_path, "red"),
        "Photos from 2023/A.jpg.json": _sidecar("A.jpg"),
        # An album repeats the sidecar of a photo kept in its year folder.
        "Trip/A.jpg.json": _sidecar("A.jpg"),
        # A long name: the file name is cut short, the title is not.
        "Photos from 2023/Screenshot_20230615-123456_A_Very_Long_Applica.png":
            _jpeg(tmp_path, "white"),
        "Trip/Screenshot_20230615-123456_A_Very_Long_Applica.json": _sidecar(long_name),
        # The photo itself was in a part that is missing.
        "Photos from 2023/IMG_9999.jpg.json": _sidecar("IMG_9999.jpg"),
    })
    with caplog.at_level(logging.WARNING, logger="takeout_organizer"):
        processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
        assert processor.run()
    assert processor.photos_missing == ["IMG_9999.jpg"]
    assert any("1 sidecar names a photo that is not in this export (for example "
               "IMG_9999.jpg)" in r.getMessage() for r in caplog.records)
    assert any(line.startswith("Photos missing from the export: 1")
               for line in processor.summary_lines)


def test_leftover_work_folders_are_reported_in_one_line(tmp_path, caplog, monkeypatch):
    work = tmp_path / "work"
    for i in range(5):
        old = work / f"{WORK_DIR_PREFIX}{i}"
        old.mkdir(parents=True)
        os.utime(old, (0, 0))
    src = _export(tmp_path, {"Photos from 2023/A.jpg": _jpeg(tmp_path, "red")})
    with caplog.at_level(logging.WARNING, logger="takeout_organizer"):
        assert MainProcessor(str(src), str(tmp_path / "out"), dry_run=True,
                             work_dir=str(work)).run()
    messages = [r.getMessage() for r in caplog.records if "leftover" in r.getMessage()]
    assert messages == [f"Found 5 leftover work folders from earlier runs in {work} "
                        f"(named {WORK_DIR_PREFIX}...). They are safe to delete."]


def test_counts_read_naturally():
    assert counted(1, "file") == "1 file"
    assert counted(8135, "file") == "8,135 files"
    assert counted(0, "guessed date") == "0 guessed dates"


@needs_exiftool
def test_undo_asks_about_one_file_in_the_singular(tmp_path, capsys, monkeypatch):
    src = _export(tmp_path, {"Photos from 2023/A.jpg": _jpeg(tmp_path, "red"),
                             "Photos from 2023/A.jpg.json": _sidecar("A.jpg")})
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["undo", "--dest", str(dest)]) == 2
    assert "Delete 1 file (" in capsys.readouterr().err
    assert cli.main(["undo", "--dest", str(dest), "--yes"]) == 0
    assert "Deleted 1 file written by run" in capsys.readouterr().out


