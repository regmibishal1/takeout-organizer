"""Tests for FileMatcher - the media<->JSON pairing logic.

Each test targets a filename pattern Google Photos Takeout actually produces.
"""

from __future__ import annotations

import json
import os

from takeout_organizer.file_matcher import FileMatcher


def _match_map(directory):
    """Return {media_basename: json_basename} for matched pairs."""
    matcher = FileMatcher(case_sensitive=False)
    pairs, _unmatched_media, _unmatched_json = matcher.match_files(str(directory))
    return {
        os.path.basename(p["media"]): os.path.basename(p["json"]) for p in pairs
    }


def test_exact_match(takeout_dir):
    assert _match_map(takeout_dir)["photo.jpg"] == "photo.jpg.json"


def test_no_extension_sidecar(takeout_dir):
    assert _match_map(takeout_dir)["sunset.jpg"] == "sunset.json"


def test_edited_variant_shares_original_sidecar(takeout_dir):
    result = _match_map(takeout_dir)
    # Both the original and the -edited version resolve to the same sidecar.
    assert result["beach.jpg"] == "beach.jpg.json"
    assert result["beach-edited.jpg"] == "beach.jpg.json"


def test_duplicate_counter_pattern(takeout_dir):
    # IMG_1(1).jpg  ->  IMG_1.jpg(1).json  (Google moves the counter)
    assert _match_map(takeout_dir)["IMG_1(1).jpg"] == "IMG_1.jpg(1).json"


def test_live_photo_video_uses_photo_sidecar(takeout_dir):
    result = _match_map(takeout_dir)
    # The .MOV component has no sidecar of its own; it borrows the .HEIC's.
    assert result["IMG_2.MOV"] == "IMG_2.HEIC.json"
    assert result["IMG_2.HEIC"] == "IMG_2.HEIC.json"


def test_orphan_media_is_unmatched(takeout_dir):
    matcher = FileMatcher(case_sensitive=False)
    _pairs, unmatched_media, _unmatched_json = matcher.match_files(str(takeout_dir))
    assert any(os.path.basename(m) == "orphan.png" for m in unmatched_media)


def test_media_and_video_type_detection():
    matcher = FileMatcher()
    assert matcher.is_image_file("a.JPG") and not matcher.is_video_file("a.JPG")
    assert matcher.is_video_file("a.mov") and not matcher.is_image_file("a.mov")
    assert not matcher.is_media_file("notes.txt")


def test_case_insensitive_sidecar(tmp_path):
    # Sidecar written with different case than the media file.
    (tmp_path / "Shot.JPG").write_bytes(b"x")
    (tmp_path / "shot.jpg.json").write_text("{}", encoding="utf-8")
    matcher = FileMatcher(case_sensitive=False)
    found = matcher.find_json_for_media(str(tmp_path / "Shot.JPG"))
    assert found is not None and found.lower().endswith("shot.jpg.json")


def _touch(directory, *names):
    for name in names:
        (directory / name).write_bytes(b"x")


def test_similar_name_never_takes_another_files_sidecar(tmp_path):
    # IMG_7.jpg has no sidecar. It must not borrow IMG_77's: that would write
    # another photo's date and place into it.
    _touch(tmp_path, "IMG_7.jpg", "IMG_77.jpg", "IMG_77.jpg.json")
    matcher = FileMatcher(case_sensitive=False)
    pairs, unmatched, _ = matcher.match_files(str(tmp_path))
    assert {os.path.basename(p["media"]) for p in pairs} == {"IMG_77.jpg"}
    assert [os.path.basename(m) for m in unmatched] == ["IMG_7.jpg"]


def test_supplemental_metadata_sidecar_names(tmp_path):
    # Naming used by Takeout since late 2024, including cut-short suffixes and
    # the duplicate counter moved behind the suffix.
    _touch(
        tmp_path,
        "IMG_1234.jpg", "IMG_1234.jpg.supplemental-metadata.json",
        "IMG_1234(1).jpg", "IMG_1234.jpg.supplemental-metadata(1).json",
        "PXL_20230615_123456789.jpg", "PXL_20230615_123456789.jpg.supplemental-me.json",
        "IMG_5(2).jpg", "IMG_5.jpg.supplem(2).json",
        "IMG_9-edited.jpg", "IMG_9.jpg", "IMG_9.jpg.supplemental-metadata.json",
    )
    result = _match_map(tmp_path)
    assert result["IMG_1234.jpg"] == "IMG_1234.jpg.supplemental-metadata.json"
    assert result["IMG_1234(1).jpg"] == "IMG_1234.jpg.supplemental-metadata(1).json"
    assert result["PXL_20230615_123456789.jpg"] == "PXL_20230615_123456789.jpg.supplemental-me.json"
    assert result["IMG_5(2).jpg"] == "IMG_5.jpg.supplem(2).json"
    assert result["IMG_9-edited.jpg"] == "IMG_9.jpg.supplemental-metadata.json"


def test_counter_never_takes_the_unnumbered_sidecar(tmp_path):
    _touch(tmp_path, "IMG_1234.jpg", "IMG_1234(1).jpg", "IMG_1234.jpg.supplemental-metadata.json")
    result = _match_map(tmp_path)
    assert result == {"IMG_1234.jpg": "IMG_1234.jpg.supplemental-metadata.json"}


def test_motion_photo_and_other_formats_are_media(tmp_path):
    matcher = FileMatcher()
    for name in ("PXL_1.MP", "PXL_1.MV", "clip.mts", "clip.m2ts"):
        assert matcher.is_video_file(name), name
    for name in ("pic.avif", "raw.CR2", "raw.cr3", "raw.NEF", "raw.arw", "raw.orf", "raw.rw2"):
        assert matcher.is_image_file(name), name

    _touch(tmp_path, "PXL_2.MP", "PXL_2.MP.supplemental-metadata.json")
    assert _match_map(tmp_path) == {"PXL_2.MP": "PXL_2.MP.supplemental-metadata.json"}


def test_pixel_motion_video_uses_still_sidecar(tmp_path):
    _touch(tmp_path, "PXL_3.MP", "PXL_3.MP.jpg", "PXL_3.MP.jpg.json")
    result = _match_map(tmp_path)
    assert result["PXL_3.MP"] == "PXL_3.MP.jpg.json"


def test_truncated_sidecar_requires_matching_title(tmp_path):
    long_name = "Screenshot_2023-06-15-12-34-56-123_com.example.app.jpg"
    other = "Screenshot_2023-06-15-12-34-56-123_com.example.other.jpg"
    (tmp_path / long_name).write_bytes(b"x")
    (tmp_path / other).write_bytes(b"x")
    sidecar = tmp_path / f"{long_name[:46]}.json"
    sidecar.write_text(json.dumps({"title": long_name}), encoding="utf-8")

    result = _match_map(tmp_path)
    assert result[long_name] == sidecar.name
    assert other not in result


def _sidecar_file(path, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": title}), encoding="utf-8")


def test_a_sidecar_is_used_once_by_the_strongest_match(tmp_path):
    # IMG.jpeg would take IMG.jpg.json by the alternative-extension rule, but
    # IMG.jpg holds it by exact name.
    _touch(tmp_path, "IMG.jpg", "IMG.jpeg")
    _sidecar_file(tmp_path / "IMG.jpg.json", "IMG.jpg")
    pairs, unmatched, _ = FileMatcher().match_files(str(tmp_path))
    assert [(os.path.basename(p["media"]), p["rule"]) for p in pairs] == [("IMG.jpg", "exact")]
    assert [os.path.basename(m) for m in unmatched] == ["IMG.jpeg"]


def test_title_decides_between_two_formats_of_one_name(tmp_path):
    _touch(tmp_path, "photo.jpg", "photo.png")
    _sidecar_file(tmp_path / "photo.json", "photo.png")
    assert _match_map(tmp_path) == {"photo.png": "photo.json"}


def test_sidecar_in_another_folder_is_found_by_its_title(tmp_path):
    photo = tmp_path / "Photos from 2023" / "IMG_5.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"x")
    _sidecar_file(tmp_path / "Trip" / "IMG_5.jpg.json", "IMG_5.jpg")
    pairs, unmatched, _ = FileMatcher().match_files(str(tmp_path))
    assert [(os.path.basename(p["json"]), p["rule"]) for p in pairs] == \
        [("IMG_5.jpg.json", "title-index")]
    assert not unmatched


def test_title_lookup_refuses_when_more_than_one_could_fit(tmp_path):
    # Camera counters repeat over the years: two IMG_0001.jpg sidecars elsewhere.
    photo = tmp_path / "Photos from 2023" / "IMG_0001.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"x")
    _sidecar_file(tmp_path / "Trip 2019" / "IMG_0001.jpg.json", "IMG_0001.jpg")
    _sidecar_file(tmp_path / "Trip 2021" / "IMG_0001.jpg.json", "IMG_0001.jpg")
    assert _match_map(tmp_path) == {}

    # ... and two photos of that name with one sidecar elsewhere.
    other = tmp_path / "Photos from 2024" / "IMG_0002.jpg"
    other.parent.mkdir()
    other.write_bytes(b"x")
    (tmp_path / "Photos from 2023" / "IMG_0002.jpg").write_bytes(b"y")
    _sidecar_file(tmp_path / "Trip 2022" / "IMG_0002.jpg.json", "IMG_0002.jpg")
    assert "IMG_0002.jpg" not in _match_map(tmp_path)


def test_google_metadata_files_are_not_sidecars(tmp_path):
    _touch(tmp_path, "IMG.jpg")
    _sidecar_file(tmp_path / "IMG.jpg.json", "IMG.jpg")
    for name in ("metadata.json", "shared_album_comments.json"):
        _sidecar_file(tmp_path / name, "Album")
    _, _, unmatched_json = FileMatcher().match_files(str(tmp_path))
    assert unmatched_json == []
