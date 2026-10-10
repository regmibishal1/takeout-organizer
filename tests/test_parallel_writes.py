"""Several ExifTool processes write metadata at once in big runs; the result
must be the same as with one, file for file, and stopping must end them all."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli, main_processor
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.metadata_handler import MetadataHandler

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
G = "Takeout/Google Photos/Photos from 2023"
COUNT = 14


def _export(tmp_path) -> Path:
    """COUNT photos with sidecars, a day apart; IMG_0007 is a JPEG named .heic
    (ExifTool cannot write it, so it is copied without new metadata)."""
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i in range(COUNT):
            name = f"IMG_{i:04d}" + (".heic" if i == 7 else ".jpg")
            image = tmp_path / f"{i}.jpg"
            noise = random.Random(i).randbytes(16 * 16 * 3)
            Image.frombytes("RGB", (16, 16), noise).save(image, "JPEG")
            zf.write(image, f"{G}/{name}")
            zf.writestr(f"{G}/{name}.json", json.dumps({
                "title": name, "description": f"photo {i}",
                "photoTakenTime": {"timestamp": str(1686839445 + i * 86400)},
                "geoData": {"latitude": 35.6586, "longitude": 139.7454, "altitude": 0.0}}))
    return src


@pytest.fixture
def parallel(monkeypatch):
    """Any run with at least one file writes with `processes` ExifTool
    processes; returns the handlers it started."""
    started = []
    real_clone = MetadataHandler.clone

    def clone(self):
        handler = real_clone(self)
        handler.closed = False
        real_close = handler.close

        def close():
            handler.closed = True
            real_close()
        handler.close = close
        started.append(handler)
        return handler

    def use(processes: int):
        monkeypatch.setattr(main_processor, "EXIFTOOL_PROCESSES", processes)
        monkeypatch.setattr(main_processor, "PARALLEL_FROM", 1)
        monkeypatch.setattr(MetadataHandler, "clone", clone)
        return started
    return use


def _tags(dest: Path) -> dict:
    files = sorted(p for p in dest.rglob("IMG_*") if ".takeout-organizer" not in p.parts)
    out = subprocess.run(["exiftool", "-json", "-n", "-DateTimeOriginal", "-OffsetTimeOriginal",
                          "-GPSLatitude", "-Description", *map(str, files)],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return {Path(row["SourceFile"]).relative_to(dest).as_posix():
            {k: v for k, v in row.items() if k != "SourceFile"} for row in json.loads(out)}


@needs_exiftool
def test_several_processes_write_the_same_as_one(tmp_path, parallel):
    src = _export(tmp_path)
    one = MainProcessor(str(src), str(tmp_path / "one"), time_zone="+00:00")
    assert one.run() and one.exiftool_processes == 1
    started = parallel(3)
    many = MainProcessor(str(src), str(tmp_path / "many"), time_zone="+00:00")
    assert many.run() and many.exiftool_processes == 3
    assert len(started) == 3 and all(h.closed for h in started)
    assert many.summary == one.summary
    assert many.summary["written"] == COUNT and many.summary["without_metadata"] == 1
    assert _tags(tmp_path / "many") == _tags(tmp_path / "one")
    # The file ExifTool could not write is named in its own failure.
    assert list(many.metadata_errors) == [p for p in many.metadata_errors
                                          if p.endswith("IMG_0007.heic")]
    assert len(many.metadata_errors) == 1


@needs_exiftool
def test_stopping_ends_every_process_and_a_resume_finishes(tmp_path, parallel):
    src, dest = _export(tmp_path), tmp_path / "out"
    started = parallel(3)
    stop = threading.Event()

    def progress(current, total, phase, details):
        if phase == "Copying" and current == 5:
            stop.set()
    processor = MainProcessor(str(src), str(dest), time_zone="+00:00", cancel_event=stop,
                              progress_callback=progress)
    assert processor.run() is False
    assert started and all(h.closed for h in started)
    assert sum(1 for o in processor.outcomes.values() if o == "written") < COUNT

    again = MainProcessor(str(src), str(dest), time_zone="+00:00", resume=True)
    assert again.run()
    assert again.summary["written"] + again.summary["already"] == COUNT
    assert cli.main(["verify", "--dest", str(dest)]) == 0


@needs_exiftool
def test_a_small_run_writes_with_one_process(tmp_path, monkeypatch):
    created = []
    monkeypatch.setattr(MetadataHandler, "clone", lambda self: created.append(1))
    src = _export(tmp_path)   # COUNT files, under PARALLEL_FROM
    processor = MainProcessor(str(src), str(tmp_path / "out"), time_zone="+00:00")
    assert processor.run()
    assert created == [] and processor.exiftool_processes == 1
