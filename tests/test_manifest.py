"""The manifest: stable IDs, one row per input file, runs recorded."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.manifest import Manifest, input_id

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG")
    return buf.getvalue()


def _sidecar(title: str) -> str:
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": "1686839445"}})


def _zip_source(tmp_path, files: dict) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for name, data in files.items():
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}", data)
    return src


def _rows(processor) -> dict:
    manifest = Manifest(processor.manifest_path)
    try:
        return {row["rel_path"]: row for row in manifest.files(processor.run_id)}, \
            manifest.runs()
    finally:
        manifest.close()


def test_input_ids_are_stable_and_distinct():
    a = input_id("takeout-001.zip", "Takeout/Google Photos/Album/IMG.jpg")
    assert a == hashlib.sha1(b"takeout-001.zip/Takeout/Google Photos/Album/IMG.jpg").hexdigest()
    assert a != input_id("takeout-002.zip", "Takeout/Google Photos/Album/IMG.jpg")
    assert input_id(None, "Takeout/Google Photos/Album/IMG.jpg") == \
        "Takeout/Google Photos/Album/IMG.jpg"


def test_manifest_tables_round_trip(tmp_path):
    manifest = Manifest(str(tmp_path / "m" / "manifest.db"))
    manifest.start_run("r1", "src", "dest", False, {"layout": "year-month"})
    manifest.record_files("r1", [{"input_id": "x", "rel_path": "a.jpg", "outcome": "written"}])
    manifest.finish_run("r1", "completed")
    runs, files = manifest.runs(), manifest.files("r1")
    manifest.close()
    assert runs[0]["status"] == "completed" and json.loads(runs[0]["settings"]) == \
        {"layout": "year-month"}
    assert files[0]["rel_path"] == "a.jpg" and files[0]["outcome"] == "written"


def test_every_input_file_has_a_row_with_its_decisions(tmp_path):
    photo = _jpeg()
    src = _zip_source(tmp_path, {
        "IMG.jpg": photo, "IMG.jpg.json": _sidecar("IMG.jpg"),
        "IMG(1).jpg": photo,                       # exact duplicate
        "notes.txt": b"not a photo",
    })
    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
    assert processor.run()
    assert Path(processor.manifest_path).parent == Path(processor.output_dir)
    assert not (tmp_path / "out").exists()

    rows, runs = _rows(processor)
    base = "Takeout/Google Photos/Photos from 2023/"
    assert set(rows) == {base + "IMG.jpg", base + "IMG(1).jpg", base + "notes.txt"}
    kept, dup, note = rows[base + "IMG.jpg"], rows[base + "IMG(1).jpg"], rows[base + "notes.txt"]
    assert kept["input_id"] == input_id("takeout-001.zip", base + "IMG.jpg")
    assert kept["zip"] == "takeout-001.zip" and kept["kind"] == "photo"
    assert kept["match_rule"] == "exact" and kept["sidecar"].endswith("IMG.jpg.json")
    assert kept["date_source"] == "sidecar" and kept["date_chosen"].startswith("2023-06-15")
    assert kept["dedup_role"] == "keep" and kept["outcome"] == "written"
    assert kept["sha256"] == hashlib.sha256(photo).hexdigest()
    assert dup["dedup_role"] == "drop" and dup["kept_input_id"] == kept["input_id"]
    assert dup["outcome"] == "duplicate"
    assert note["kind"] == "other" and note["outcome"] == "other"
    assert runs[0]["run_id"] == processor.run_id and runs[0]["status"] == "completed"
    assert runs[0]["dry_run"] == 1


def test_ids_do_not_change_between_runs(tmp_path):
    src = _zip_source(tmp_path, {"IMG.jpg": _jpeg(), "IMG.jpg.json": _sidecar("IMG.jpg")})
    ids = []
    for _ in range(2):
        processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
        assert processor.run()
        rows, _ = _rows(processor)
        ids.append({row["input_id"] for row in rows.values()})
    assert ids[0] == ids[1]


def test_folder_source_ids_are_relative_paths(takeout_dir, tmp_path):
    processor = MainProcessor(str(takeout_dir.parent.parent), str(tmp_path / "out"),
                              dry_run=True, dedup_mode="off")
    assert processor.run()
    rows, _ = _rows(processor)
    row = rows["Google Photos/Album/photo.jpg"]
    assert row["input_id"] == row["rel_path"] and row["zip"] is None


def test_a_stopped_run_is_recorded_as_stopped(tmp_path):
    src = _zip_source(tmp_path, {"IMG.jpg": _jpeg()})
    stop = threading.Event()
    stop.set()
    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True, cancel_event=stop)
    assert processor.run() is False
    _, runs = _rows(processor)
    assert runs[0]["status"] == "stopped"


@needs_exiftool
def test_real_runs_share_one_manifest_in_the_destination(tmp_path):
    src = _zip_source(tmp_path, {"IMG_1.HEIC": _jpeg(), "IMG_1.HEIC.json": _sidecar("IMG_1.HEIC")})
    dest = tmp_path / "out"
    first = MainProcessor(str(src), str(dest), fix_extensions=True)
    assert first.run()
    assert Path(first.manifest_path) == dest / ".takeout-organizer" / "manifest.db"

    rows, _ = _rows(first)
    # The working copy was renamed to .jpg; the row keeps the name in the ZIP.
    row = rows["Takeout/Google Photos/Photos from 2023/IMG_1.HEIC"]
    assert row["target_path"] == "2023/06/IMG_1.jpg" and (dest / row["target_path"]).exists()

    second = MainProcessor(str(src), str(dest), fix_extensions=True)
    assert second.run()
    rows, runs = _rows(second)
    assert [r["status"] for r in runs] == ["completed", "completed"]
    assert rows["Takeout/Google Photos/Photos from 2023/IMG_1.HEIC"]["outcome"] == "already"
