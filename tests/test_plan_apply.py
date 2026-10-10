"""Plan, then apply: a preview shows exactly what a run does, and a run
applies its plan row by row."""

from __future__ import annotations

import builtins
import io
import json
import shutil
import sqlite3
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer.cli import main
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.manifest import Manifest
from takeout_organizer.metadata_handler import MetadataHandler

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")


def _jpeg(color, exif_ifd=None) -> bytes:
    exif = Image.Exif()
    if exif_ifd:
        exif.get_ifd(0x8769).update(exif_ifd)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _sidecar(title: str, timestamp: str = "1686839445") -> str:
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": timestamp}})


def _export(tmp_path) -> Path:
    """Covers each way a folder is chosen, and a name used twice."""
    src = tmp_path / "src"
    src.mkdir()
    base = "Takeout/Google Photos"
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        # Camera in Tokyo, sidecar the same moment: the camera date is kept, so
        # the folder is 06/15 even though the zone setting would say 06/14.
        zf.writestr(f"{base}/Photos from 2023/TOKYO.jpg",
                    _jpeg((200, 0, 0), {36867: "2023:06:15 08:30:45", 36881: "+09:00"}))
        zf.writestr(f"{base}/Photos from 2023/TOKYO.jpg.json", _sidecar("TOKYO.jpg", "1686785445"))
        # The same name in two albums, different photos.
        zf.writestr(f"{base}/Trip A/IMG.jpg", _jpeg((0, 200, 0)))
        zf.writestr(f"{base}/Trip A/IMG.jpg.json", _sidecar("IMG.jpg"))
        zf.writestr(f"{base}/Trip B/IMG.jpg", _jpeg((0, 0, 200)))
        zf.writestr(f"{base}/Trip B/IMG.jpg.json", _sidecar("IMG.jpg"))
        # No sidecar: dated by its name, or not at all.
        zf.writestr(f"{base}/Photos from 2019/IMG_20190704_101500.jpg", _jpeg((90, 90, 0)))
        zf.writestr(f"{base}/Photos from 2019/DSC00042.jpg", _jpeg((0, 90, 90)))
    return src


def _targets(processor) -> dict:
    manifest = Manifest(processor.manifest_path)
    try:
        return {row["rel_path"]: row["target_path"] for row in manifest.files(processor.run_id)}
    finally:
        manifest.close()


def _statuses(processor) -> tuple:
    manifest = Manifest(processor.manifest_path)
    try:
        rows = manifest.files(processor.run_id)
        run = [r for r in manifest.runs() if r["run_id"] == processor.run_id][0]
        return sorted(r["status"] for r in rows), run["status"]
    finally:
        manifest.close()


@needs_exiftool
def test_preview_equals_result(tmp_path):
    src = _export(tmp_path)
    options = dict(time_zone="America/New_York", layout="year-month-day")
    preview = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True, **options)
    assert preview.run()
    planned = _targets(preview)

    real = MainProcessor(str(src), str(tmp_path / "out"), **options)
    assert real.run()
    assert _targets(real) == planned

    out = tmp_path / "out"
    for target in planned.values():
        assert (out / target).exists(), target
    by_name = {Path(rel).name: target for rel, target in planned.items()}
    assert by_name["TOKYO.jpg"] == "2023/06/15/TOKYO.jpg"
    assert by_name["IMG_20190704_101500.jpg"] == "2019/07/04/IMG_20190704_101500.jpg"
    assert by_name["DSC00042.jpg"] == "_undated/DSC00042.jpg"
    imgs = sorted(t for rel, t in planned.items() if Path(rel).name == "IMG.jpg")
    assert imgs == ["2023/06/15/IMG.jpg", "2023/06/15/IMG_1.jpg"]  # 10:30 in New York


@needs_exiftool
def test_a_stopped_apply_leaves_the_rest_planned(tmp_path):
    stop = threading.Event()

    def progress(current, total, phase, details):
        if phase == "Copying" and current == 1:
            stop.set()  # stops before the second file

    processor = MainProcessor(str(_export(tmp_path)), str(tmp_path / "out"),
                              progress_callback=progress, cancel_event=stop)
    assert processor.run() is False
    statuses, run_status = _statuses(processor)
    assert statuses.count("done") == 1 and statuses.count("planned") == len(statuses) - 1
    assert run_status == "stopped"


@needs_exiftool
def test_declining_writes_no_photos(tmp_path):
    shown = []

    def decline(lines):
        shown.extend(lines)
        return False

    out = tmp_path / "out"
    processor = MainProcessor(str(_export(tmp_path)), str(out), confirm=decline)
    assert processor.run() is False and processor.declined
    assert any(line.startswith("Photos and videos to write") and line.rstrip().endswith("5")
               for line in shown)
    assert not list(out.rglob("*.jpg"))
    statuses, run_status = _statuses(processor)
    assert set(statuses) == {"planned"} and run_status == "declined"


@needs_exiftool
@pytest.mark.parametrize("answer, code, written", [("n", 3, 0), ("y", 0, 5)])
def test_run_asks_before_writing_on_a_terminal(tmp_path, monkeypatch, capsys, answer, code,
                                               written):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt: answer)
    out = tmp_path / "out"
    assert main(["run", "--source", str(_export(tmp_path)), "--dest", str(out)]) == code
    captured = capsys.readouterr()
    assert "Photos and videos to write" in captured.out
    assert len(list(out.rglob("*.jpg"))) == written
    if answer == "n":
        assert "Nothing was written." in captured.err


def test_no_question_without_a_terminal_or_with_yes(monkeypatch):
    from takeout_organizer import cli
    args = type("Args", (), {"yes": False})()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert not cli._should_ask(args, dry_run=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert cli._should_ask(args, dry_run=False)
    assert not cli._should_ask(args, dry_run=True)
    args.yes = True
    assert not cli._should_ask(args, dry_run=False)


def test_a_manifest_from_the_first_version_is_upgraded(tmp_path):
    path = tmp_path / "manifest.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT,
            source TEXT NOT NULL, dest TEXT NOT NULL, dry_run INTEGER NOT NULL,
            settings TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE files (run_id TEXT NOT NULL, input_id TEXT NOT NULL, zip TEXT,
            rel_path TEXT NOT NULL, size INTEGER, sha256 TEXT, kind TEXT, sidecar TEXT,
            match_rule TEXT, date_chosen TEXT, date_source TEXT, date_rejected TEXT,
            dedup_role TEXT, kept_input_id TEXT, target_path TEXT, outcome TEXT NOT NULL,
            error TEXT, PRIMARY KEY (run_id, input_id));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO runs VALUES ('old', 'x', 'y', 's', 'd', 0, '{}', 'completed');
        INSERT INTO files (run_id, input_id, rel_path, outcome) VALUES ('old', 'a', 'a.jpg',
            'written');
    """)
    old.commit()
    old.close()

    manifest = Manifest(str(path))
    manifest.update_file("old", "a", status="done")
    rows = manifest.files("old")
    manifest.close()
    assert rows[0]["status"] == "done" and rows[0]["outcome"] == "written"


def test_prediction_matches_the_write_decision():
    handler = MetadataHandler(exiftool_path="exiftool", require=False)
    tokyo = {"DateTimeOriginal": "2023:06:15 08:30:45", "OffsetTimeOriginal": "+09:00"}
    # Same moment: the camera date stays.
    assert handler.predicted_capture({"timestamp": 1686785445}, tokyo) == \
        (datetime(2023, 6, 15, 8, 30, 45), "exif")
    # A date corrected in Google Photos: Google's date is written.
    date, source = handler.predicted_capture({"timestamp": 1686785445 + 86400 * 3}, tokyo)
    assert source == "sidecar" and date.day == 18
    # No sidecar date: whatever the camera wrote.
    assert handler.predicted_capture({}, tokyo)[1] == "exif"
    assert handler.predicted_capture({}, {}) == (None, None)
