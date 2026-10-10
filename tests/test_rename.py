"""--rename: names that sort by capture time, decided in the plan, so undo,
verify, reruns and saved plans all see the same names."""

from __future__ import annotations

import json
import shutil
import struct
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli
from takeout_organizer.main_processor import MainProcessor, SetupError
from takeout_organizer.naming import check_template, render

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
G = "Takeout/Google Photos/Photos from 2023"
TEMPLATE = "{date:%Y%m%d_%H%M%S}_{name}"


def _mp4(timescale: int = 1000) -> bytes:
    def box(kind, payload):
        return struct.pack(">I", 8 + len(payload)) + kind + payload
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">IIIH", timescale, 0, 0x10000, 0x100)
               + bytes(10) + matrix + bytes(24) + struct.pack(">I", 2))
    return box(b"ftyp", b"isom\0\0\x02\0isommp41") + box(b"moov", mvhd)


def _export(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i, name in enumerate(("A.jpg", "LIVE.jpg")):
            image = tmp_path / name
            Image.new("RGB", (8, 8), (90 * i, 9, 9)).save(image)
            zf.write(image, f"{G}/{name}")
            zf.writestr(f"{G}/{name}.json", json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": str(1686839445 + 60 * i)}}))
        zf.writestr(f"{G}/LIVE.MP4", _mp4())          # the video of the live photo
        # A Pixel motion photo: the still PXL_3.MP.jpg and its video PXL_3.MP.
        still = tmp_path / "PXL_3.MP.jpg"
        Image.new("RGB", (8, 8), (9, 9, 90)).save(still, "JPEG")
        zf.write(still, f"{G}/PXL_3.MP.jpg")
        zf.writestr(f"{G}/PXL_3.MP.jpg.json", json.dumps(
            {"title": "PXL_3.MP.jpg", "photoTakenTime": {"timestamp": "1686839565"}}))
        zf.writestr(f"{G}/PXL_3.MP", _mp4(600))
        undated = tmp_path / "DSC00042.jpg"
        Image.new("RGB", (8, 8), (9, 90, 9)).save(undated)
        zf.write(undated, f"{G}/DSC00042.jpg")         # no sidecar, no date: a guess
    return src


def _names(dest: Path) -> list:
    return sorted(str(p.relative_to(dest)).replace("\\", "/") for p in dest.rglob("*")
                  if p.is_file() and ".takeout-organizer" not in p.parts
                  and p.suffix not in (".html", ".m3u8", ".json"))


# --- the template ------------------------------------------------------------

def test_templates_render_sortable_names():
    when = datetime(2023, 6, 15, 23, 30, 45)
    assert render(TEMPLATE, "IMG_1.jpg", when) == "20230615_233045_IMG_1.jpg"
    assert render("{date}", "IMG_1.HEIC", when) == "20230615_233045.HEIC"
    # Characters a file name cannot hold become _.
    assert render("{date:%Y-%m-%d %H:%M} {name}", "a.jpg", when) == "2023-06-15 23_30 a.jpg"


@pytest.mark.parametrize("bad", ["{place}_{name}", "fixed name", "{date:%Y", "{0}"])
def test_unusable_templates_are_refused(bad):
    with pytest.raises(ValueError):
        check_template(bad)


def test_a_bad_template_stops_before_anything_starts(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["run", "--source", str(tmp_path), "--dest", str(tmp_path / "out"),
                  "--rename", "{place}"])
    assert stop.value.code == 2
    assert "it can use {date}, {date:FORMAT} and {name}" in capsys.readouterr().err
    (tmp_path / "src").mkdir()
    with pytest.raises(SetupError, match="must use"):
        MainProcessor(str(tmp_path / "src"), str(tmp_path / "out"), dry_run=True, rename="same")


# --- in a run ------------------------------------------------------------------

@needs_exiftool
def test_a_run_names_files_by_date_and_keeps_pairs_together(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest), rename=TEMPLATE, time_zone="+00:00")
    assert processor.run()
    assert _names(dest) == [
        "2023/06/20230615_143045_A.jpg",
        "2023/06/20230615_143145_LIVE.MP4",   # named by its photo: the pair stays a pair
        "2023/06/20230615_143145_LIVE.jpg",
        "2023/06/20230615_143245_PXL_3.MP",   # the motion video, named by its still
        "2023/06/20230615_143245_PXL_3.MP.jpg",
        "2023/06/20230615_143245_PXL_3.MP.xmp",   # its metadata, named with it
        "_undated/DSC00042.jpg",              # a guessed date never goes in a name
    ]
    album = (dest / "Albums").exists()
    assert not album or "20230615_143045_A.jpg" in (dest / "Albums").read_text()


@needs_exiftool
def test_reruns_verify_and_undo_see_the_new_names(tmp_path, capsys):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), rename=TEMPLATE).run()
    again = MainProcessor(str(src), str(dest), rename=TEMPLATE)
    assert again.run()
    assert again.summary["written"] == 0 and again.summary["already"] == 6

    assert cli.main(["verify", "--dest", str(dest)]) == 0
    assert cli.main(["undo", "--dest", str(dest), "--yes"]) == 0
    capsys.readouterr()
    assert _names(dest) == []


@needs_exiftool
def test_a_saved_plan_with_another_template_is_refused(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    preview = MainProcessor(str(src), str(dest), dry_run=True, rename=TEMPLATE)
    assert preview.run()
    other = MainProcessor(str(src), str(dest), plan_file=preview.manifest_path,
                          rename="{date:%Y%m%d}_{name}")
    assert other.run() is False
    assert "other settings (rename)" in other.problem
