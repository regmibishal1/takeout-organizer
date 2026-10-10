"""`verify`: a library checked later against what the runs recorded."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli, verify
from takeout_organizer.main_processor import MainProcessor

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None,
                                reason="ExifTool not installed")
G = "Takeout/Google Photos/Photos from 2023"


@pytest.fixture
def library(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i, name in enumerate(("A.jpg", "B.jpg", "C.jpg")):
            image = tmp_path / name
            Image.new("RGB", (8, 8), (70 * i, 9, 9)).save(image)
            zf.write(image, f"{G}/{name}")
            zf.writestr(f"{G}/{name}.json", json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": "1686839445"}}))
        zf.writestr("Takeout/Google Photos/notes.txt", "not a photo")
    dest = tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()
    return dest


def _state(dest: Path) -> dict:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in dest.rglob("*") if p.is_file()}


def test_a_library_as_the_runs_left_it_passes(library, capsys):
    before = _state(library)
    assert cli.main(["verify", "--dest", str(library)]) == 0
    out = capsys.readouterr().out
    assert "Checked 4 files the runs put in" in out
    assert "All files are as the runs left them." in out
    assert _state(library) == before   # verify only reads


def test_missing_and_changed_files_are_listed(library, capsys):
    (library / "2023" / "06" / "A.jpg").unlink()
    with open(library / "2023" / "06" / "B.jpg", "ab") as f:
        f.write(b"edited")
    assert cli.main(["verify", "--dest", str(library)]) == 1
    out = capsys.readouterr().out
    assert "Missing: 1 (no longer in the library)\n  2023/06/A.jpg" in out
    assert "Changed since written: 1" in out and "  2023/06/B.jpg" in out
    assert "2 files are fine." in out


def test_a_photo_that_lost_its_date_is_listed(library, monkeypatch):
    def without_c(handler, files):
        found = real(handler, files)
        return {path: tags for path, tags in found.items() if not path.lower().endswith("c.jpg")}
    real = verify._read_dates
    monkeypatch.setattr(verify, "_read_dates", without_c)
    result = verify.check(str(library))
    assert result.dates_checked and result.without_date == ["2023/06/C.jpg"]
    assert not result.ok and result.fine == 3


def test_json_lists_every_file_for_scripts(library, capsys):
    (library / "_other" / "notes.txt").unlink()
    assert cli.main(["verify", "--dest", str(library), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["checked"] == 4 and data["fine"] == 3
    assert data["missing"] == ["_other/notes.txt"]


def test_a_folder_without_a_manifest_is_not_a_library(tmp_path, capsys):
    assert cli.main(["verify", "--dest", str(tmp_path)]) == 2
    assert "No library made by this tool" in capsys.readouterr().err


def test_without_exiftool_the_dates_are_not_checked(library, capsys, monkeypatch):
    from takeout_organizer.metadata_handler import MetadataHandler
    monkeypatch.setattr(MetadataHandler, "_find_exiftool", lambda self: None)
    monkeypatch.setenv("PATH", os.devnull)
    assert cli.main(["verify", "--dest", str(library)]) == 0
    assert "the dates inside the files were not checked" in capsys.readouterr().out
