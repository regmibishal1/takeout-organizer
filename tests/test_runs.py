"""Long runs: preflight checks, one run per destination, atomic copies, Ctrl+C."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import zipfile

import pytest
from PIL import Image

from takeout_organizer import cli, preflight
from takeout_organizer.file_processor import PART_SUFFIX, FileProcessor
from takeout_organizer.main_processor import MainProcessor, SetupError
from takeout_organizer.runguard import KeepAwake, RunLock


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG")
    return buf.getvalue()


def _takeout_zip(path, files: dict, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in files.items():
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}", data)


# --- preflight -------------------------------------------------------------

@pytest.mark.parametrize("size, text", [
    (512, "512 bytes"), (2048, "2.0 KB"), (52 * 1024 ** 3, "52 GB"), (1536 * 1024 ** 4, "1536 TB"),
])
def test_sizes_read_naturally(size, text):
    assert preflight.human(size) == text


def test_missing_and_damaged_parts_are_reported(tmp_path):
    for part in (1, 3):
        _takeout_zip(tmp_path / f"takeout-20261001T120000Z-00{part}.zip", {"A.jpg": _jpeg()})
    (tmp_path / "takeout-20261001T120000Z-004.zip").write_bytes(b"cut off download")

    result = preflight.check(str(tmp_path), str(tmp_path.parent / "out"), str(tmp_path.parent),
                             dry_run=False)
    assert not result.problems
    assert any("Part 002 of 004 is missing" in w for w in result.warnings)
    assert any("takeout-20261001T120000Z-004.zip is damaged" in w for w in result.warnings)


def test_a_zip_that_expands_far_beyond_its_size_is_refused(tmp_path, monkeypatch):
    # 20 MB of zeros packs into a few KB: a ZIP bomb in miniature.
    monkeypatch.setattr(preflight, "EXPANSION_FLOOR", 1024 ** 2)
    src = tmp_path / "src"
    src.mkdir()
    _takeout_zip(src / "takeout-001.zip", {"zeros.bin": bytes(20 * 1024 ** 2)})
    with pytest.raises(SetupError, match=r"takeout-001\.zip would expand from .+ to 20 MB, "
                                         r"more than 20 times its size"):
        MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
    assert not (tmp_path / "out").exists()


def test_photos_and_small_zips_are_not_taken_for_a_bomb(tmp_path, monkeypatch):
    def problems(folder):
        return preflight.check(str(folder), str(tmp_path / "out"), str(tmp_path),
                               dry_run=True).problems

    # Text packs tightly, but below the floor the ratio does not count.
    text = tmp_path / "text"
    text.mkdir()
    _takeout_zip(text / "takeout-001.zip", {"notes.txt": bytes(5 * 1024 ** 2)})
    assert problems(text) == []

    # Photos barely compress, so they pass even with no floor at all.
    photos = tmp_path / "photos"
    photos.mkdir()
    _takeout_zip(photos / "takeout-001.zip", {"A.jpg": _jpeg(), "B.jpg": _jpeg((9, 9, 9))})
    monkeypatch.setattr(preflight, "EXPANSION_FLOOR", 0)
    assert problems(photos) == []
    assert problems(text) != []


def test_a_zip_with_too_many_entries_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "MAX_ENTRIES", 5)
    _takeout_zip(tmp_path / "takeout-001.zip", {f"IMG_{i}.jpg": _jpeg() for i in range(6)})
    result = preflight.check(str(tmp_path), str(tmp_path.parent / "out"), str(tmp_path.parent),
                             dry_run=True)
    assert result.problems == [
        "takeout-001.zip holds 6 files, more than a Takeout part ever does. It is probably "
        "damaged or not from Google Takeout. Move it out of the folder and run again."]


def test_not_enough_space_stops_before_anything_is_written(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _takeout_zip(src / "takeout-001.zip", {"A.jpg": _jpeg()})
    monkeypatch.setattr(preflight, "_free", lambda path: 10)
    monkeypatch.setattr(preflight, "_same_drive", lambda a, b: False)

    with pytest.raises(SetupError, match="Not enough space for the working copy"):
        MainProcessor(str(src), str(tmp_path / "out"), exiftool_path="exiftool")
    assert not (tmp_path / "out").exists()


def test_space_shortage_into_a_full_library_only_warns(tmp_path, monkeypatch):
    # A rerun skips files already there, so it may need much less than the estimate.
    src = tmp_path / "src"
    src.mkdir()
    _takeout_zip(src / "takeout-001.zip", {"A.jpg": _jpeg()})
    dest = tmp_path / "library"
    dest.mkdir()
    (dest / "old.jpg").write_bytes(_jpeg())
    monkeypatch.setattr(preflight, "_free", lambda path: 10 ** 12 if "work" in path else 10)
    monkeypatch.setattr(preflight, "_same_drive", lambda a, b: False)

    result = preflight.check(str(src), str(dest), str(tmp_path / "work"), dry_run=False)
    assert not result.problems and "already there" in result.warnings[0]


def test_work_dir_must_exist_and_be_outside_the_source(tmp_path):
    src = tmp_path / "src"
    (src / "inner").mkdir(parents=True)
    with pytest.raises(SetupError, match="work folder does not exist"):
        MainProcessor(str(src), str(tmp_path / "out"), dry_run=True,
                      work_dir=str(tmp_path / "missing"))
    with pytest.raises(SetupError, match="work folder is inside the Takeout folder"):
        MainProcessor(str(src), str(tmp_path / "out"), dry_run=True,
                      work_dir=str(src / "inner"))


def test_work_dir_holds_the_working_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _takeout_zip(src / "takeout-001.zip", {"A.jpg": _jpeg()})
    work = tmp_path / "work"
    work.mkdir()
    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True, work_dir=str(work))
    assert processor.run()
    assert os.path.dirname(processor.temp_dir) == str(work)
    assert not os.path.exists(processor.temp_dir)  # removed at the end


# --- one run per destination ----------------------------------------------

@pytest.fixture
def other_live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _write_lock(dest, pid):
    lock = dest / ".takeout-organizer" / "lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{pid}\n2026-10-10 14:02", encoding="utf-8")


def test_a_second_run_into_the_same_folder_is_refused(tmp_path, other_live_process):
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "out"
    _write_lock(dest, other_live_process)
    with pytest.raises(SetupError, match=f"Another run is using this folder "
                                         f"\\(started 2026-10-10 14:02, process "
                                         f"{other_live_process}\\)"):
        MainProcessor(str(src), str(dest), exiftool_path="exiftool")


def test_a_lock_left_by_a_finished_process_is_taken_over(tmp_path):
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True)
    dest = tmp_path / "out"
    _write_lock(dest, int(done.stdout))
    lock = RunLock(str(dest))
    assert lock.acquire() is None
    assert (dest / ".takeout-organizer" / "lock").read_text().startswith(str(os.getpid()))
    lock.release()
    assert not (dest / ".takeout-organizer" / "lock").exists()


# --- copies ----------------------------------------------------------------

def test_copies_appear_complete_or_not_at_all(tmp_path, monkeypatch):
    fp = FileProcessor()
    src = tmp_path / "a.jpg"
    src.write_bytes(_jpeg())
    assert fp.copy_file_safe(str(src), str(tmp_path / "out" / "a.jpg"))
    assert sorted(os.listdir(tmp_path / "out")) == ["a.jpg"]

    def broken_copy(source, target):
        with open(target, "wb") as f:
            f.write(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", broken_copy)
    assert not fp.copy_file_safe(str(src), str(tmp_path / "out" / "b.jpg"))
    assert sorted(os.listdir(tmp_path / "out")) == ["a.jpg"]


def test_only_leftover_partial_copies_are_removed(tmp_path):
    (tmp_path / "2023").mkdir()
    (tmp_path / "2023" / f"IMG.jpg{PART_SUFFIX}").write_bytes(b"half")
    (tmp_path / "2023" / "movie.part").write_bytes(b"the user's own file")
    assert FileProcessor().remove_partial_copies(str(tmp_path)) == 1
    assert os.listdir(tmp_path / "2023") == ["movie.part"]


def test_merging_moves_instead_of_copying(tmp_path):
    extracted = tmp_path / "takeout-001"
    album = extracted / "Takeout" / "Google Photos" / "Album"
    album.mkdir(parents=True)
    (album / "A.jpg").write_bytes(_jpeg())
    merged = FileProcessor().merge_takeout_folders([str(extracted)], str(tmp_path / "merged"))
    assert os.path.exists(os.path.join(merged, "Album", "A.jpg"))
    assert not (album / "A.jpg").exists()  # moved: the working copy needs half the space


def test_unreadable_zip_entries_count_as_failed_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    good, bad = _jpeg((200, 0, 0)), _jpeg((0, 200, 0))
    zip_path = src / "takeout-001.zip"
    _takeout_zip(zip_path, {"GOOD.jpg": good, "BAD.jpg": bad}, compression=zipfile.ZIP_STORED)
    data = bytearray(zip_path.read_bytes())
    at = data.find(bad[100:120])
    data[at] ^= 0xFF  # damage BAD.jpg inside the archive: its checksum fails
    zip_path.write_bytes(bytes(data))

    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
    assert processor.run()
    summary = processor.summary
    assert summary["balanced"] and summary["input"] == 2
    assert summary["written"] == 1 and summary["failed"] == 1
    assert any(entry.endswith("BAD.jpg") for entry in processor.inputs)


# --- stopping ----------------------------------------------------------------

def test_ctrl_c_stops_after_the_current_file_then_quits(monkeypatch):
    stop = threading.Event()
    previous = cli._stop_on_ctrl_c(stop)
    try:
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert stop.is_set()
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
    finally:
        signal.signal(signal.SIGINT, previous)


def test_stopped_run_exits_3(tmp_path, monkeypatch, capsys):
    src = tmp_path / "src"
    src.mkdir()

    def stopped_run(self):
        self.cancelled = True
        return False

    monkeypatch.setattr(MainProcessor, "run", stopped_run)
    code = cli.main(["run", "--source", str(src), "--dest", str(tmp_path / "out"), "--dry-run"])
    assert code == 3
    assert "Run the same command again to continue" in capsys.readouterr().err


def test_keep_awake_never_breaks_a_run():
    guard = KeepAwake()
    guard.start()  # best effort: may be unavailable, must not raise
    guard.stop()
    assert not guard.active


def test_real_run_releases_its_lock(tmp_path):
    if shutil.which("exiftool") is None:
        pytest.skip("ExifTool not installed")
    src = tmp_path / "src"
    src.mkdir()
    _takeout_zip(src / "takeout-001.zip", {
        "A.jpg": _jpeg(), "A.jpg.json": json.dumps({"title": "A.jpg"}),
    })
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    assert not (dest / ".takeout-organizer" / "lock").exists()
