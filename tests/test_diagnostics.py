"""Bug-report diagnostics and the per-run summary.json: useful numbers, and
none of the user's folder paths."""

from __future__ import annotations

import json
import shutil
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli
from takeout_organizer.diagnostics import last_run, report, shorten_paths
from takeout_organizer.main_processor import MainProcessor

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
G = "Takeout/Google Photos/Photos from 2023"


def _export(tmp_path, names=("A.jpg",), orphan_sidecar=False) -> Path:
    src = tmp_path / "My Photos" / "Takeout export"   # spaces, as real folders have
    src.mkdir(parents=True)
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i, name in enumerate(names):
            image = tmp_path / name
            Image.new("RGB", (8, 8), (60 * i, 9, 9)).save(image)
            zf.write(image, f"{G}/{name}")
            zf.writestr(f"{G}/{name}.json", json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": "1686839445"}}))
        if orphan_sidecar:
            zf.writestr(f"{G}/IMG_9999.jpg.json", json.dumps({"title": "IMG_9999.jpg"}))
    return src


def test_folder_paths_are_left_out():
    text = (r"Error processing C:\Users\Jane Doe\Pictures\Out\2023\06\IMG_1.jpg: bad" "\n"
            "copied /home/jane/Photos/Kyoto trip/IMG_2.jpg\n"
            r"cannot read D:\Takeout\takeout-001.zip" "\n"
            "see /var/lib/xyz/run.log")
    out = shorten_paths(text, {"<dest>": r"C:\Users\Jane Doe\Pictures\Out",
                               "<source>": "/home/jane/Photos"})
    assert out.splitlines() == [
        r"Error processing <dest>\2023\06\IMG_1.jpg: bad",
        "copied <source>/Kyoto trip/IMG_2.jpg",
        "cannot read takeout-001.zip",
        "see run.log",
    ]
    assert "Jane" not in out and "jane" not in out


def test_a_preview_writes_its_numbers_to_summary_json(tmp_path):
    src = _export(tmp_path, names=("A.jpg", "B.jpg"))
    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True)
    assert processor.run()
    data = json.loads(Path(processor.log_file).with_suffix(".json").read_text(encoding="utf-8"))
    assert data["status"] == "completed" and data["dry_run"] is True
    assert data["summary"]["input"] == 2 and data["summary"]["balanced"] is True
    assert data["match_rate"] == 1.0
    assert data["seconds"] >= 0 and data["seconds_per_1000_files"] is not None
    assert data["tool_version"]


def test_a_stopped_run_still_writes_its_summary(tmp_path):
    src = _export(tmp_path)
    stop = threading.Event()
    stop.set()
    processor = MainProcessor(str(src), str(tmp_path / "out"), dry_run=True, cancel_event=stop)
    assert processor.run() is False
    data = json.loads(Path(processor.log_file).with_suffix(".json").read_text(encoding="utf-8"))
    assert data["status"] == "stopped" and data["summary"] == {}


@needs_exiftool
def test_diagnose_describes_the_last_run_without_folder_paths(tmp_path, capsys):
    src, dest = _export(tmp_path, orphan_sidecar=True), tmp_path / "My Library"
    assert MainProcessor(str(src), str(dest)).run()

    assert cli.main(["diagnose", "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Takeout Organizer diagnostics")
    assert "ExifTool" in out and "Python" in out and "Operating system" in out
    assert "Last run: " in out and "input: 1" in out and "photos_missing: 1" in out
    assert "names a photo that is not in this export (for example IMG_9999.jpg)" in out
    for private in (str(tmp_path), str(tmp_path).replace("\\", "/"), "My Library",
                    "Takeout export"):
        assert private not in out
    assert last_run(str(dest))["status"] == "completed"


def test_diagnose_without_a_run_says_so(tmp_path):
    out = report(str(tmp_path))
    assert "No run found in the destination folder." in out
    assert str(tmp_path) not in out
