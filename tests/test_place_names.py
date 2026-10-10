"""--place-names: the town or city a photo was taken in goes on its date
folder, offline, from the GeoNames data that ships with the tool."""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import cli, places
from takeout_organizer.main_processor import MainProcessor, SetupError
from takeout_organizer.naming import _NOT_IN_NAMES

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
G = "Takeout/Google Photos/Photos from 2023"
TOKYO_TOWER = (35.6586, 139.7454)
EIFFEL_TOWER = (48.8584, 2.2945)
JUNE_2023 = "1686839445"


# --- the lookup ------------------------------------------------------------------

@pytest.mark.parametrize("position, name", [
    (TOKYO_TOWER, "Tokyo"),               # a city, not the ward the tower stands in
    ((35.4437, 139.638), "Yokohama"),     # a big city next to a bigger one keeps its name
    (EIFFEL_TOWER, "Paris"),
    ((48.8049, 2.1204), "Versailles"),
    ((27.7154, 85.3123), "Kathmandu"),
    ((69.6492, 18.9553), "Troms\u00f8"),  # far north, where degrees of longitude are short
    ((-18.1416, 178.4419), "Suva"),
])
def test_photos_get_the_town_or_city_they_were_taken_in(position, name):
    assert places.place_name(*position) == name


@pytest.mark.parametrize("position", [(0.0, -150.0), (0.0, 0.0), (89.9, 0.0), (91.0, 0.0),
                                      (10.0, 181.0)])
def test_no_place_far_from_towns_or_for_a_position_that_is_not_one(position):
    assert places.place_name(*position) is None


@pytest.fixture
def one_town(monkeypatch):
    """The data replaced by single towns, to measure the radius exactly."""
    def use(*towns):
        cells = {}
        for lat, lon, population, name in towns:
            cells.setdefault(places._cell(lat, lon), []).append((lat, lon, population, name))
        monkeypatch.setattr(places, "_cells", cells)
    return use


def test_the_place_must_be_within_30_km(one_town):
    one_town((10.0, 20.0, 50000, "Town"))
    degree = places._KM_PER_DEGREE
    assert places.place_name(10.0 + 29 / degree, 20.0) == "Town"
    assert places.place_name(10.0 + 31 / degree, 20.0) is None


def test_the_lookup_crosses_the_180th_meridian(one_town):
    one_town((-16.5, 179.95, 50000, "East"))
    assert places.place_name(-16.5, -179.95) == "East"   # about 11 km away


def test_a_much_larger_city_nearby_wins_over_a_small_town_at_the_spot(one_town):
    one_town((10.0, 20.0, 20000, "Suburb"), (10.03, 20.0, 5000000, "City"))
    assert places.place_name(10.0, 20.0) == "City"
    one_town((10.0, 20.0, 20000, "Town"), (10.2, 20.0, 5000000, "City"))   # 22 km off
    assert places.place_name(10.0, 20.0) == "Town"


def test_every_name_in_the_data_can_be_a_folder_name():
    with open(places._DATA, encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert "GeoNames (geonames.org), CC BY 4.0" in lines[0]
    rows = [line.split("\t") for line in lines if not line.startswith("#")]
    assert len(rows) > 30000 and all(len(row) == 4 for row in rows)
    for _, _, _, name in rows:
        assert name and name == name.strip(" .") and not _NOT_IN_NAMES.search(name), name


# --- in a run ------------------------------------------------------------------------

def _photo(tmp_path, name: str, gps=None) -> Path:
    path = tmp_path / name
    noise = random.Random(name).randbytes(16 * 16 * 3)   # a different picture for each
    Image.frombytes("RGB", (16, 16), noise).save(path, "JPEG")
    if gps:
        lat, lon = gps
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-GPSLatitude={lat}",
                        "-GPSLatitudeRef=N", f"-GPSLongitude={lon}", "-GPSLongitudeRef=E",
                        str(path)], check=True)
    return path


def _export(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()

    def geo(position):
        return {"latitude": position[0], "longitude": position[1], "altitude": 0.0}

    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        # Google's location only.
        zf.write(_photo(tmp_path, "TOKYO.jpg"), f"{G}/TOKYO.jpg")
        zf.writestr(f"{G}/TOKYO.jpg.json", json.dumps(
            {"title": "TOKYO.jpg", "photoTakenTime": {"timestamp": JUNE_2023},
             "geoData": geo(TOKYO_TOWER)}))
        # No location at all (Google writes 0,0).
        zf.write(_photo(tmp_path, "NOWHERE.jpg"), f"{G}/NOWHERE.jpg")
        zf.writestr(f"{G}/NOWHERE.jpg.json", json.dumps(
            {"title": "NOWHERE.jpg", "photoTakenTime": {"timestamp": JUNE_2023},
             "geoData": geo((0.0, 0.0))}))
        # The camera recorded Paris; Google says Tokyo.
        zf.write(_photo(tmp_path, "CAMERA.jpg", EIFFEL_TOWER), f"{G}/CAMERA.jpg")
        zf.writestr(f"{G}/CAMERA.jpg.json", json.dumps(
            {"title": "CAMERA.jpg", "photoTakenTime": {"timestamp": JUNE_2023},
             "geoData": geo(TOKYO_TOWER)}))
        # Only an upload date: the date is a guess, so no date folder and no place.
        zf.write(_photo(tmp_path, "GUESS.jpg"), f"{G}/GUESS.jpg")
        zf.writestr(f"{G}/GUESS.jpg.json", json.dumps(
            {"title": "GUESS.jpg", "creationTime": {"timestamp": JUNE_2023},
             "geoData": geo(TOKYO_TOWER)}))
    return src


def _names(dest: Path) -> list:
    return sorted(str(p.relative_to(dest)).replace("\\", "/") for p in dest.rglob("*")
                  if p.is_file() and ".takeout-organizer" not in p.parts
                  and p.suffix not in (".html", ".m3u8", ".json"))


@needs_exiftool
def test_a_run_puts_the_place_on_the_date_folder(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    processor = MainProcessor(str(src), str(dest), place_names=True, time_zone="+00:00")
    assert processor.run()
    assert _names(dest) == [
        "2023/06 Paris/CAMERA.jpg",      # the camera's location is kept, so it decides
        "2023/06 Tokyo/TOKYO.jpg",
        "2023/06/NOWHERE.jpg",
        "_undated/GUESS.jpg",
    ]
    assert (processor.places_found, processor.places_missing) == (2, 1)
    summary = json.loads(Path(processor.log_file).with_suffix(".json").read_text("utf-8"))
    assert summary["places_found"] == 2 and summary["settings"]["place_names"] is True


@needs_exiftool
def test_with_overwrite_gps_googles_location_decides(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), place_names=True, overwrite_gps=True,
                         time_zone="+00:00").run()
    assert "2023/06 Tokyo/CAMERA.jpg" in _names(dest)


@needs_exiftool
def test_verify_undo_and_reruns_see_the_place_folders(tmp_path, capsys):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), place_names=True, rename="{date:%Y%m%d}_{name}",
                         time_zone="+00:00").run()
    again = MainProcessor(str(src), str(dest), place_names=True, rename="{date:%Y%m%d}_{name}",
                          time_zone="+00:00")
    assert again.run()
    assert again.summary["written"] == 0 and again.summary["already"] == 4
    assert "2023/06 Tokyo/20230615_TOKYO.jpg" in _names(dest)
    assert cli.main(["verify", "--dest", str(dest)]) == 0
    assert cli.main(["undo", "--dest", str(dest), "--yes"]) == 0
    capsys.readouterr()
    assert _names(dest) == []


@needs_exiftool
def test_turning_it_on_later_leaves_the_library_where_it_is(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), time_zone="+00:00").run()
    before = _names(dest)
    later = MainProcessor(str(src), str(dest), place_names=True, time_zone="+00:00")
    assert later.run()
    assert later.summary["already"] == 4 and _names(dest) == before


@needs_exiftool
def test_a_saved_plan_made_without_place_names_is_refused(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    preview = MainProcessor(str(src), str(dest), dry_run=True)
    assert preview.run()
    other = MainProcessor(str(src), str(dest), plan_file=preview.manifest_path,
                          place_names=True)
    assert other.run() is False
    assert "other settings (place_names)" in other.problem


@needs_exiftool
def test_a_plan_from_before_place_names_still_applies(tmp_path):
    # A plan from an older version records no place_names setting; it was off then.
    src, dest = _export(tmp_path), tmp_path / "out"
    preview = MainProcessor(str(src), str(dest), dry_run=True)
    assert preview.run()
    with sqlite3.connect(preview.manifest_path) as db:
        for run_id, settings in db.execute("SELECT run_id, settings FROM runs").fetchall():
            old = {k: v for k, v in json.loads(settings).items() if k != "place_names"}
            db.execute("UPDATE runs SET settings = ? WHERE run_id = ?", (json.dumps(old), run_id))
    db.close()
    assert MainProcessor(str(src), str(dest), plan_file=preview.manifest_path).run()


def test_the_flat_layout_has_no_folders_to_name(tmp_path, capsys):
    (tmp_path / "src").mkdir()
    with pytest.raises(SetupError, match="flat layout"):
        MainProcessor(str(tmp_path / "src"), str(tmp_path / "out"), dry_run=True,
                      layout="flat", place_names=True)
    code = cli.main(["run", "--source", str(tmp_path / "src"), "--dest", str(tmp_path / "out"),
                     "--layout", "flat", "--place-names", "--yes"])
    assert code == 2 and "flat layout" in capsys.readouterr().err
