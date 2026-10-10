"""--heic-copies jpg: a JPG beside each HEIC photo, recorded on the HEIC's
row, so undo removes it, verify checks it, and a rerun does not make it twice."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli, deduplicator, preflight
from takeout_organizer.main_processor import MainProcessor, SetupError
from takeout_organizer.metadata_handler import MetadataHandler

pytestmark = [
    pytest.mark.skipif(shutil.which("exiftool") is None, reason="ExifTool not installed"),
    pytest.mark.skipif(deduplicator.HEIC_SUPPORT is None, reason="no HEIC decoder"),
]
G = "Takeout/Google Photos/Photos from 2023"


def _heic(tmp_path) -> bytes:
    # Writing a test HEIC needs an encoder: pillow-heif, a development dependency.
    pillow_heif = pytest.importorskip("pillow_heif", reason="needed to write a test HEIC")
    pillow_heif.register_heif_opener()
    path = tmp_path / "make.heic"
    Image.new("RGB", (64, 48), (180, 60, 60)).save(path, "HEIF", quality=80)
    return path.read_bytes()


def _export(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        zf.writestr(f"{G}/IMG_1.HEIC", _heic(tmp_path))
        zf.writestr(f"{G}/IMG_1.HEIC.json", json.dumps({
            "title": "IMG_1.HEIC", "photoTakenTime": {"timestamp": "1686839445"},
            "geoData": {"latitude": 35.6586, "longitude": 139.7454, "altitude": 40.0}}))
    return src


def _tags(path: Path) -> dict:
    handler = MetadataHandler(keep_open=True)
    try:
        return handler.read_back(str(path), ["DateTimeOriginal", "GPSLatitude", "Orientation"])
    finally:
        handler.close()


def test_a_heic_gets_a_jpg_copy_with_its_metadata(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest), heic_copies="jpg",
                              rename="{date:%Y%m%d}_{name}", time_zone="+09:00")
    assert processor.run()
    folder = dest / "2023" / "06"
    assert sorted(p.name for p in folder.iterdir()) == ["20230615_IMG_1.HEIC",
                                                        "20230615_IMG_1.jpg"]
    heic, jpg = _tags(folder / "20230615_IMG_1.HEIC"), _tags(folder / "20230615_IMG_1.jpg")
    assert jpg["ExifIFD:DateTimeOriginal"] == heic["ExifIFD:DateTimeOriginal"]
    assert jpg["GPS:GPSLatitude"] == heic["GPS:GPSLatitude"]
    assert "IFD0:Orientation" not in jpg          # the pixels are upright already
    with Image.open(folder / "20230615_IMG_1.jpg") as image:
        assert image.format == "JPEG" and image.size == (64, 48)
    assert processor.summary["heic_copies"] == 1 and processor.summary["balanced"]


def test_undo_and_verify_know_the_copy(tmp_path, capsys):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), heic_copies="jpg").run()
    copy = dest / "2023" / "06" / "IMG_1.jpg"
    copy.unlink()
    assert cli.main(["verify", "--dest", str(dest)]) == 1
    assert "2023/06/IMG_1.jpg" in capsys.readouterr().out

    assert MainProcessor(str(src), str(dest), heic_copies="jpg").run()   # made again
    assert copy.exists()
    assert cli.main(["undo", "--dest", str(dest), "--yes"]) == 0      # that run made the copy
    assert not copy.exists() and (dest / "2023" / "06" / "IMG_1.HEIC").exists()


def test_copies_can_be_added_to_a_library_later_and_are_made_once(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest)).run()                 # no copies yet
    later = MainProcessor(str(src), str(dest), heic_copies="jpg")
    assert later.run()
    assert later.summary["already"] == 1 and later.summary["heic_copies"] == 1
    again = MainProcessor(str(src), str(dest), heic_copies="jpg")
    assert again.run() and again.summary["heic_copies"] == 0
    assert sorted(p.name for p in (dest / "2023" / "06").iterdir()) == ["IMG_1.HEIC",
                                                                         "IMG_1.jpg"]


def test_without_a_decoder_copies_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(deduplicator, "HEIC_SUPPORT", None)
    (tmp_path / "src").mkdir()
    with pytest.raises(SetupError, match=r"takeout-organizer\[heic\]"):
        MainProcessor(str(tmp_path / "src"), str(tmp_path / "out"), heic_copies="jpg")


def test_the_space_check_counts_the_copies(tmp_path):
    src = _export(tmp_path)
    plain = preflight.check(str(src), str(tmp_path / "out"), str(tmp_path), dry_run=False)
    with_copies = preflight.check(str(src), str(tmp_path / "out"), str(tmp_path),
                                  dry_run=False, heic_copies=True)
    assert with_copies.dest_needed > plain.dest_needed


def _copy_rows(path) -> dict:
    from takeout_organizer.manifest import Manifest
    manifest = Manifest(str(path))
    try:
        run = manifest.runs()[-1]["run_id"]
        return {Path(r["rel_path"]).name: r["derived_path"] for r in manifest.files(run)}
    finally:
        manifest.close()


def test_a_jpg_already_named_like_the_copy_is_kept_and_the_preview_agrees(tmp_path):
    # iPhone Takeouts often hold IMG_1.HEIC and IMG_1.jpg both.
    src, dest = _export(tmp_path), tmp_path / "out"
    jpg = tmp_path / "IMG_1.jpg"
    Image.new("RGB", (8, 8), (9, 9, 200)).save(jpg)
    with zipfile.ZipFile(src / "takeout-001.zip", "a") as zf:
        zf.write(jpg, f"{G}/IMG_1.jpg")
        zf.writestr(f"{G}/IMG_1.jpg.json", json.dumps(
            {"title": "IMG_1.jpg", "photoTakenTime": {"timestamp": "1686839445"}}))
    assert MainProcessor(str(src), str(dest)).run()             # IMG_1.jpg is in the library
    original = (dest / "2023" / "06" / "IMG_1.jpg").read_bytes()

    preview = MainProcessor(str(src), str(dest), dry_run=True, heic_copies="jpg")
    assert preview.run()
    assert _copy_rows(preview.manifest_path)["IMG_1.HEIC"] == "2023/06/IMG_1_1.jpg"
    real = MainProcessor(str(src), str(dest), heic_copies="jpg")
    assert real.run()
    assert (dest / "2023" / "06" / "IMG_1_1.jpg").exists()
    assert (dest / "2023" / "06" / "IMG_1.jpg").read_bytes() == original   # never replaced


def test_a_saved_plan_makes_the_copies_it_showed(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    options = {"heic_copies": "jpg", "rename": "{date:%Y%m%d}_{name}", "time_zone": "+09:00"}
    preview = MainProcessor(str(src), str(dest), dry_run=True, **options)
    assert preview.run()
    planned = _copy_rows(preview.manifest_path)["IMG_1.HEIC"]
    assert planned == "2023/06/20230615_IMG_1.jpg"
    real = MainProcessor(str(src), str(dest), plan_file=preview.manifest_path, **options)
    assert real.run()
    assert (dest / planned).exists()
