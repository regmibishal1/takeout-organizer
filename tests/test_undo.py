"""Undo: remove exactly what a run wrote, never anything else."""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer.cli import main
from takeout_organizer.main_processor import MainProcessor
from takeout_organizer.manifest import Manifest

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None,
                                reason="ExifTool not installed")


def _image(color, fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, fmt)
    return buf.getvalue()


def _sidecar(title):
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": "1686839445"}})


def _export(src: Path, names=("A.jpg", "B.jpg")) -> Path:
    src.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i, name in enumerate(names):
            zf.writestr(f"Takeout/Google Photos/Trip/{name}", _image((40 * i, 80, 120)))
            zf.writestr(f"Takeout/Google Photos/Trip/{name}.json", _sidecar(name))
        zf.writestr("Takeout/Google Photos/Trip/SCAN.bmp", _image((5, 5, 5), "BMP"))
        zf.writestr("Takeout/Google Photos/Trip/SCAN.bmp.json", _sidecar("SCAN.bmp"))
        zf.writestr("Takeout/Google Photos/Trip/notes.txt", b"not a photo")
    return src


def _files(dest: Path) -> list:
    """What is in the destination, apart from the tool's own records."""
    return sorted(str(p.relative_to(dest)).replace("\\", "/") for p in dest.rglob("*")
                  if p.is_file() and ".takeout-organizer" not in p.parts
                  and p.name != "takeout-organizer-report.html")


def _run_status(dest: Path, run_id: str) -> str:
    manifest = Manifest(str(dest / ".takeout-organizer" / "manifest.db"))
    try:
        return {r["run_id"]: r["status"] for r in manifest.runs()}[run_id]
    finally:
        manifest.close()


def test_undo_removes_exactly_what_the_run_wrote(tmp_path, capsys):
    src, dest = _export(tmp_path / "src"), tmp_path / "out"
    zip_hash = hashlib.sha256((src / "takeout-001.zip").read_bytes()).hexdigest()
    run = MainProcessor(str(src), str(dest))
    assert run.run()
    written = _files(dest)
    assert "2023/06/SCAN.bmp.xmp" in written and "_other/Trip/notes.txt" in written
    assert "Albums/Trip.m3u8" in written

    assert main(["undo", "--dest", str(dest), "--yes"]) == 0
    assert "Deleted 5 files" in capsys.readouterr().out  # A, B, SCAN.bmp + .xmp, notes.txt
    assert _files(dest) == []
    assert not (dest / "2023").exists() and not (dest / "_other").exists()
    assert not (dest / "Albums").exists()
    assert _run_status(dest, run.run_id) == "undone"
    assert hashlib.sha256((src / "takeout-001.zip").read_bytes()).hexdigest() == zip_hash


def test_a_file_edited_since_the_run_is_kept(tmp_path, capsys):
    src, dest = _export(tmp_path / "src"), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    edited = dest / "2023" / "06" / "A.jpg"
    edited.write_bytes(edited.read_bytes() + b"edited later")

    assert main(["undo", "--dest", str(dest), "--yes"]) == 0
    captured = capsys.readouterr()
    assert "Kept, edited since the run" in captured.err and "A.jpg" in captured.err
    assert _files(dest) == ["2023/06/A.jpg", "Albums/Trip.m3u8", "Albums/albums.json"]
    playlist = (dest / "Albums" / "Trip.m3u8").read_text(encoding="utf-8").splitlines()
    assert playlist == ["#EXTM3U", "../2023/06/A.jpg"]


def test_each_undo_removes_only_its_own_run(tmp_path):
    dest = tmp_path / "out"
    first = MainProcessor(str(_export(tmp_path / "src1")), str(dest))
    assert first.run()
    second = MainProcessor(str(_export(tmp_path / "src2", ("A.jpg", "B.jpg", "C.jpg"))),
                           str(dest))
    assert second.run()  # A and B are already there: only C is new

    assert main(["undo", "--dest", str(dest), "--yes"]) == 0
    remaining = _files(dest)
    assert "2023/06/C.jpg" not in remaining and "2023/06/A.jpg" in remaining
    assert _run_status(dest, second.run_id) == "undone"

    assert main(["undo", "--dest", str(dest), "--yes"]) == 0
    assert _files(dest) == []
    assert _run_status(dest, first.run_id) == "undone"


def test_undo_asks_or_needs_yes(tmp_path, monkeypatch, capsys):
    src, dest = _export(tmp_path / "src"), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    before = _files(dest)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["undo", "--dest", str(dest)]) == 2
    assert "Run with --yes" in capsys.readouterr().err

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt: "n")
    assert main(["undo", "--dest", str(dest)]) == 3
    err = capsys.readouterr().err
    assert "Nothing was deleted." in err
    assert _files(dest) == before


def test_undo_with_nothing_recorded(tmp_path, capsys):
    assert main(["undo", "--dest", str(tmp_path), "--yes"]) == 2
    assert "No runs are recorded" in capsys.readouterr().err

    src, dest = _export(tmp_path / "src"), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    assert main(["undo", "--dest", str(dest), "--run", "no-such-run", "--yes"]) == 2
    assert "was not found" in capsys.readouterr().err
