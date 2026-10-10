"""A run works next to the photos and moves each finished file into place:
no second copy, and nothing extra needed on the system drive."""

from __future__ import annotations

import errno
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import file_processor, preflight
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.metadata_handler import MetadataHandler

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")


def _export(tmp_path, names=("A.jpg", "B.jpg")) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i, name in enumerate(names):
            image = tmp_path / name
            Image.new("RGB", (8, 8), (60 * i, 20, 20)).save(image)
            zf.write(image, f"Takeout/Google Photos/Photos from 2023/{name}")
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}.json", json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": "1686839445"}}))
    return src


def _photos(dest: Path) -> list:
    return sorted(p.name for p in dest.rglob("*.jpg") if ".takeout-organizer" not in p.parts)


@pytest.fixture
def copies(monkeypatch):
    """The files copied (not moved) into place during the test."""
    copied = []
    real = file_processor.FileProcessor.copy_file_safe

    def spy(self, src, dst, overwrite=False):
        copied.append(os.path.basename(dst))
        return real(self, src, dst, overwrite)
    monkeypatch.setattr(file_processor.FileProcessor, "copy_file_safe", spy)
    return copied


@needs_exiftool
def test_a_run_works_next_to_the_photos_and_moves_them(tmp_path, copies):
    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest))
    assert processor.work_root == str(dest / ".takeout-organizer" / "work")
    assert processor.run()

    assert _photos(dest) == ["A.jpg", "B.jpg"] and copies == []
    assert not os.path.exists(processor.temp_dir)
    assert not (dest / ".takeout-organizer" / "work").exists()
    # The metadata written into the working copy moved with it.
    handler = MetadataHandler(keep_open=True)
    try:
        found = handler.read_back(str(next(dest.rglob("A.jpg"))), ["DateTimeOriginal"])
    finally:
        handler.close()
    assert found.get("ExifIFD:DateTimeOriginal")


@needs_exiftool
def test_moving_falls_back_to_copying_across_drives(tmp_path, copies, monkeypatch):
    def cross_drive(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(file_processor.os, "rename", cross_drive)

    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    assert _photos(dest) == ["A.jpg", "B.jpg"] and sorted(copies) == ["A.jpg", "B.jpg"]


@needs_exiftool
def test_a_file_already_at_the_name_is_never_replaced(tmp_path):
    src, dest = _export(tmp_path, names=("A.jpg",)), tmp_path / "out"
    folder = dest / "2023" / "06"
    folder.mkdir(parents=True)
    (folder / "A.jpg").write_bytes(b"someone else's photo")

    assert MainProcessor(str(src), str(dest)).run()
    assert (folder / "A.jpg").read_bytes() == b"someone else's photo"
    assert sorted(p.name for p in folder.iterdir()) == ["A.jpg", "A_1.jpg"]


@needs_exiftool
def test_a_working_copy_left_by_a_killed_run_is_removed(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    stale = dest / ".takeout-organizer" / "work" / "google_photos_killed"
    stale.mkdir(parents=True)
    (stale / "IMG_0001.jpg").write_bytes(b"x" * 1000)

    assert MainProcessor(str(src), str(dest)).run()
    assert not stale.exists()


@needs_exiftool
def test_a_work_folder_given_by_the_user_is_never_emptied(tmp_path):
    src, dest, work = _export(tmp_path), tmp_path / "out", tmp_path / "work"
    keep = work / "google_photos_mine"
    keep.mkdir(parents=True)
    (keep / "notes.txt").write_text("mine")

    assert MainProcessor(str(src), str(dest), work_dir=str(work)).run()
    assert (keep / "notes.txt").read_text() == "mine"
    assert _photos(dest) == ["A.jpg", "B.jpg"]


def test_a_preview_puts_nothing_in_the_destination(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest), dry_run=True)
    assert not processor.work_root.startswith(str(dest))
    assert processor.run()
    assert not dest.exists()


def test_on_one_drive_the_run_needs_room_for_one_copy(tmp_path, monkeypatch):
    src, dest = _export(tmp_path), tmp_path / "out"
    size = preflight.check(str(src), str(dest), str(tmp_path), dry_run=False).work_needed
    # Room for one copy and a half: enough when files move, not for two copies.
    monkeypatch.setattr(preflight, "_free", lambda path: int(size * 1.5))
    work = preflight.default_work_root(str(dest), dry_run=False)

    monkeypatch.setattr(preflight, "_same_drive", lambda a, b: True)
    assert preflight.check(str(src), str(dest), work, dry_run=False).problems == []

    monkeypatch.setattr(preflight, "_same_drive", lambda a, b: False)
    assert preflight.check(str(src), str(dest), work, dry_run=False).problems == []
    monkeypatch.setattr(preflight, "_free", lambda path: int(size * 0.5))
    assert len(preflight.check(str(src), str(dest), work, dry_run=False).problems) == 2
