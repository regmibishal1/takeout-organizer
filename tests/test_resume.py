"""Resuming a stopped run, and applying a saved plan."""

from __future__ import annotations

import io
import json
import shutil
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import main_processor
from takeout_organizer.cli import main
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.manifest import Manifest

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None,
                                reason="ExifTool not installed")


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG")
    return buf.getvalue()


def _export(tmp_path, extra: dict = None) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    files = {
        "A.jpg": _jpeg((200, 0, 0)), "B.jpg": _jpeg((0, 200, 0)), "C.jpg": _jpeg((0, 0, 200)),
        "A(1).jpg": _jpeg((200, 0, 0)),            # exact duplicate of A.jpg
        "notes.txt": b"not a photo",
    }
    files.update(extra or {})
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for name, data in files.items():
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}", data)
            if name.endswith(".jpg") and "(" not in name:
                zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}.json", json.dumps(
                    {"title": name, "photoTakenTime": {"timestamp": "1686839445"}}))
    return src


def _photos(dest: Path) -> list:
    """Photos in the destination, not the tool's own files (report thumbnails)."""
    return [p for p in dest.rglob("*.jpg") if ".takeout-organizer" not in p.parts]


def _rows(processor):
    manifest = Manifest(processor.manifest_path)
    try:
        return {Path(r["rel_path"]).name: r for r in manifest.files(processor.run_id)}, \
            {r["run_id"]: r["status"] for r in manifest.runs()}
    finally:
        manifest.close()


def _stopped_run(src, dest):
    stop = threading.Event()

    def progress(current, total, phase, details):
        if phase == "Copying" and current == 1:
            stop.set()

    first = MainProcessor(str(src), str(dest), progress_callback=progress, cancel_event=stop)
    assert first.run() is False and first.cancelled
    return first


def test_resume_finishes_a_stopped_run(tmp_path, monkeypatch):
    src, dest = _export(tmp_path), tmp_path / "out"
    first = _stopped_run(src, dest)
    first_rows, _ = _rows(first)
    finished = [name for name, row in first_rows.items()
                if row["status"] == "done" and row["outcome"] != "duplicate"]
    assert len(finished) == 1

    def refuse(items):
        raise AssertionError("duplicate detection ran again")
    monkeypatch.setattr(main_processor, "deduplicate", refuse)

    resumed = MainProcessor(str(src), str(dest), resume=True)
    assert resumed.run()
    assert resumed.previous_run == first.run_id
    rows, runs = _rows(resumed)
    assert rows[finished[0]]["outcome"] == "already"      # not done twice
    assert rows["A(1).jpg"]["outcome"] == "duplicate"    # decision reused
    assert {r["status"] for r in rows.values()} == {"done"}
    assert runs == {first.run_id: "stopped", resumed.run_id: "completed"}
    assert sorted(p.name for p in _photos(dest)) == ["A.jpg", "B.jpg", "C.jpg"]
    assert (dest / "_other" / "Photos from 2023" / "notes.txt").exists()


def test_resume_without_a_stopped_run_does_a_full_run(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest), resume=True)
    assert processor.run() and processor.previous_run is None
    assert len(_photos(dest)) == 3
    assert not (dest / "processing_session.json").exists()


def test_a_saved_plan_is_applied_as_it_is(tmp_path, monkeypatch):
    src, dest = _export(tmp_path), tmp_path / "out"
    preview = MainProcessor(str(src), str(dest), dry_run=True)
    assert preview.run()
    planned, _ = _rows(preview)

    def refuse(items):
        raise AssertionError("duplicate detection ran again")
    monkeypatch.setattr(main_processor, "deduplicate", refuse)
    real = MainProcessor(str(src), str(dest), plan_file=preview.manifest_path)
    assert real.run()
    applied, _ = _rows(real)
    assert {n: r["target_path"] for n, r in applied.items()} == \
        {n: r["target_path"] for n, r in planned.items()}
    assert real.previous_run == preview.run_id


def test_a_plan_for_another_export_is_refused(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    preview = MainProcessor(str(src), str(dest), dry_run=True)
    assert preview.run()
    _export(tmp_path, {"D.jpg": _jpeg((90, 90, 90))})  # the export changed

    real = MainProcessor(str(src), str(dest), plan_file=preview.manifest_path)
    assert real.run() is False
    assert "The export changed since the plan was made" in real.problem
    assert not _photos(dest)


def test_a_plan_made_with_other_settings_is_refused(tmp_path, capsys):
    src, dest = _export(tmp_path), tmp_path / "out"
    preview = MainProcessor(str(src), str(dest), dry_run=True, layout="year")
    assert preview.run()
    code = main(["run", "--source", str(src), "--dest", str(dest), "--plan",
                 preview.manifest_path])
    assert code == 1
    assert "other settings (layout)" in capsys.readouterr().err
    assert not _photos(dest)


def test_plan_command_says_how_to_apply_it(tmp_path, capsys):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert main(["plan", "--source", str(src), "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "To apply exactly this plan:" in out and "--plan" in out and "manifest.db" in out


def test_resume_and_plan_together_are_refused(tmp_path, capsys):
    src = _export(tmp_path)
    with pytest.raises(SystemExit):
        main(["run", "--source", str(src), "--dest", str(tmp_path / "out"), "--resume",
              "--plan", str(tmp_path / "x.db")])
    assert "not allowed with argument" in capsys.readouterr().err


def test_a_rerun_skips_files_already_in_the_library(tmp_path, monkeypatch):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()

    preview = MainProcessor(str(src), str(dest), dry_run=True)
    assert preview.run()
    assert preview.summary["already"] == 4 and preview.summary["written"] == 0

    from takeout_organizer.metadata_handler import MetadataHandler

    def no_writes(*args, **kwargs):
        raise AssertionError("metadata written again")
    monkeypatch.setattr(MetadataHandler, "apply_metadata_to_image", no_writes)
    again = MainProcessor(str(src), str(dest))
    assert again.run()
    assert again.summary["already"] == 4 and again.summary["written"] == 0
    assert sorted(p.name for p in _photos(dest)) == ["A.jpg", "B.jpg", "C.jpg"]


def test_a_file_changed_in_the_library_is_not_taken_as_there(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    edited = next(p for p in _photos(dest) if p.name == "A.jpg")
    edited.write_bytes(edited.read_bytes() + b"edited")

    preview = MainProcessor(str(src), str(dest), dry_run=True)
    assert preview.run()
    assert preview.summary["already"] == 3 and preview.summary["written"] == 1


def _newer_export(tmp_path, extra: dict = None) -> Path:
    """The same photos in a newer Takeout: its ZIPs have other names, so the
    input IDs differ and only the content tells the files apart."""
    newer = tmp_path / "newer"
    newer.mkdir()
    built = _export(tmp_path / "build", extra) if extra else None
    source = built or (tmp_path / "src")
    shutil.copy(source / "takeout-001.zip", newer / "takeout-20261009T120000Z-001.zip")
    return newer


def test_a_newer_export_finds_what_is_already_there_by_content(tmp_path, monkeypatch):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    newer = _newer_export(tmp_path)

    preview = MainProcessor(str(newer), str(dest), dry_run=True)
    assert preview.run()
    assert preview.summary["already"] == 4 and preview.summary["written"] == 0

    from takeout_organizer.metadata_handler import MetadataHandler

    def no_writes(*args, **kwargs):
        raise AssertionError("metadata written again")
    monkeypatch.setattr(MetadataHandler, "apply_metadata_to_image", no_writes)
    again = MainProcessor(str(newer), str(dest))
    assert again.run()
    assert again.summary["already"] == 4 and again.summary["written"] == 0
    assert sorted(p.name for p in _photos(dest)) == ["A.jpg", "B.jpg", "C.jpg"]


def test_a_newer_export_adds_only_what_is_new(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    (tmp_path / "build").mkdir()
    newer = _newer_export(tmp_path, {"D.jpg": _jpeg((90, 90, 0))})

    again = MainProcessor(str(newer), str(dest))
    assert again.run()
    assert again.summary["already"] == 4 and again.summary["written"] == 1
    assert sorted(p.name for p in _photos(dest)) == ["A.jpg", "B.jpg", "C.jpg", "D.jpg"]


def test_content_is_recorded_without_the_duplicate_check(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    first = MainProcessor(str(src), str(dest), dedup_mode="off")
    assert first.run()
    rows, _ = _rows(first)
    assert all(row["sha256"] for row in rows.values())

    again = MainProcessor(str(_newer_export(tmp_path)), str(dest), dedup_mode="off")
    assert again.run()
    assert again.summary["written"] == 0


def test_a_file_changed_in_the_library_is_not_found_by_content(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    edited = next(p for p in _photos(dest) if p.name == "A.jpg")
    edited.write_bytes(edited.read_bytes() + b"edited")

    preview = MainProcessor(str(_newer_export(tmp_path)), str(dest), dry_run=True)
    assert preview.run()
    assert preview.summary["already"] == 3 and preview.summary["written"] == 1
