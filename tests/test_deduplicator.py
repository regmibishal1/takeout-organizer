"""Deduplicator: perceptual grouping, second signals, best-copy selection, flagging."""

from __future__ import annotations

import random
import shutil
import time
from datetime import datetime

import pytest
from PIL import Image

from takeout_organizer.deduplicator import (
    BORDERLINE_DISTANCE,
    CERTAIN_DISTANCE,
    MediaItem,
    _HashIndex,
    build_item,
    deduplicate,
    dhash,
    hamming,
    sha256,
)


def _make_image(path, seed, size=(64, 64), quality=90, exif=None):
    """A deterministic 8x8-block image, upscaled. Low-frequency block content is
    scale- and compression-stable (like a real photo), so the same seed yields the
    same dHash at any size/quality, and different seeds differ a lot."""
    rng = random.Random(seed)
    base = Image.new("RGB", (8, 8))
    bpx = base.load()
    for y in range(8):
        for x in range(8):
            bpx[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    img = base.resize(size, Image.NEAREST)
    if exif is not None:
        img.save(path, "JPEG", quality=quality, exif=exif)
    else:
        img.save(path, "JPEG", quality=quality)
    return str(path)


def _item(path, has_sidecar=False, sidecar_time=None):
    return build_item(str(path), has_sidecar=has_sidecar, sidecar_time=sidecar_time)


def _hand(path, phash, *, sidecar=False, size=(100, 100), color=(100.0, 100.0, 100.0),
          exif_time=None, subsec=False, sidecar_time=None):
    """An item with hand-set signals, to pin down one rule at a time."""
    return MediaItem(path=path, size=1000, width=size[0], height=size[1], sha=path,
                     phash=phash, mean_color=color, has_sidecar=sidecar,
                     exif_time=exif_time, exif_subsec=subsec, sidecar_time=sidecar_time)


# A hash with 32 bits set: far from the low-information limits.
BASE = 0x0F0F0F0F0F0F0F0F
NEAR = BASE ^ 0b11  # distance 2


def test_recompressed_copy_is_a_perceptual_duplicate(tmp_path):
    hi = _make_image(tmp_path / "hi.jpg", seed=1, quality=95)
    lo = _make_image(tmp_path / "lo.jpg", seed=1, quality=25)
    # Different bytes, same picture -> small perceptual distance.
    assert sha256(hi) != sha256(lo)
    assert hamming(dhash(hi), dhash(lo)) <= CERTAIN_DISTANCE

    res = deduplicate([_item(lo), _item(hi)])
    assert len(res.keep) == 1
    assert res.keep[0].path == hi          # larger (higher quality) file wins
    assert res.drop.get(lo) == hi


def test_sidecar_copy_wins_even_at_lower_resolution(tmp_path):
    big = _make_image(tmp_path / "big.jpg", seed=1, size=(96, 96), quality=95)
    small = _make_image(tmp_path / "small.jpg", seed=1, size=(64, 64), quality=95)
    res = deduplicate([_item(big, has_sidecar=False), _item(small, has_sidecar=True)])
    assert len(res.keep) == 1
    assert res.keep[0].path == small       # sidecar beats resolution
    assert res.drop.get(big) == small


def test_different_photos_are_not_merged(tmp_path):
    a = _make_image(tmp_path / "a.jpg", seed=1)
    b = _make_image(tmp_path / "b.jpg", seed=140)
    assert hamming(dhash(a), dhash(b)) > BORDERLINE_DISTANCE
    res = deduplicate([_item(a), _item(b)])
    assert len(res.keep) == 2 and not res.drop and not res.flagged


def test_exact_byte_duplicate_collapses(tmp_path):
    a = _make_image(tmp_path / "a.jpg", seed=3)
    b = tmp_path / "copy.jpg"
    shutil.copyfile(a, b)
    res = deduplicate([_item(a), _item(b)])
    assert len(res.keep) == 1 and len(res.drop) == 1


def test_borderline_distance_is_flagged_and_both_kept():
    d = CERTAIN_DISTANCE + 2
    assert CERTAIN_DISTANCE < d <= BORDERLINE_DISTANCE
    a = _hand("a.jpg", BASE)
    b = _hand("b.jpg", BASE ^ ((1 << d) - 1))
    res = deduplicate([a, b])
    assert len(res.keep) == 2          # nothing dropped on an uncertain match
    assert not res.drop
    assert res.flagged and res.flagged[0][2] == d


def test_non_image_without_phash_is_kept():
    vid = MediaItem(path="clip.mp4", is_image=False, size=999, sha="v1", phash=None)
    res = deduplicate([vid])
    assert res.keep and res.keep[0].path == "clip.mp4"


def test_burst_frames_from_takeout_are_never_merged():
    # Both have sidecars: two separate Google Photos items, however alike.
    a = _hand("burst_1.jpg", BASE, sidecar=True)
    b = _hand("burst_2.jpg", NEAR, sidecar=True)
    res = deduplicate([a, b])
    assert len(res.keep) == 2 and not res.drop
    assert res.flagged[0][3] == "separate Google Photos items"


def test_burst_frames_stay_apart_whichever_copy_comes_first():
    # A sidecar-less album copy must not pull two Takeout bursts into one group.
    album = _hand("album.jpg", BASE)
    b1 = _hand("burst_1.jpg", NEAR, sidecar=True)
    b2 = _hand("burst_2.jpg", BASE ^ 0b101, sidecar=True)
    for order in ([album, b1, b2], [b1, album, b2], [b1, b2, album]):
        res = deduplicate(order)
        kept = {it.path for it in res.keep}
        assert {"burst_1.jpg", "burst_2.jpg"} <= kept, [it.path for it in order]
        assert len(res.drop) <= 1


def test_times_are_checked_against_every_copy_in_a_group():
    anchor = _hand("anchor.jpg", BASE)  # no time known
    early = _hand("early.jpg", NEAR, exif_time=datetime(2023, 6, 15, 8, 30, 0))
    late = _hand("late.jpg", BASE ^ 0b101, exif_time=datetime(2023, 6, 15, 8, 30, 10))
    res = deduplicate([anchor, early, late])
    # late matches the timeless anchor but not early, which is in its group.
    assert res.drop == {"anchor.jpg": "early.jpg"}
    assert {it.path for it in res.keep} == {"early.jpg", "late.jpg"}
    assert res.flagged == [("late.jpg", "early.jpg", 2, "different capture times")]


def test_reported_keeper_is_never_itself_dropped():
    # copy.jpg is an exact duplicate of a.jpg; a.jpg then loses its visual group
    # to a better copy. Both drops must name the final keeper.
    a = _hand("a.jpg", BASE)
    copy = _hand("copy.jpg", BASE)
    copy.sha = a.sha = "same-bytes"
    best = _hand("best.jpg", NEAR, sidecar=True)
    res = deduplicate([a, copy, best])
    assert set(res.drop.values()) == {"best.jpg"}
    assert set(res.drop) == {"a.jpg", "copy.jpg"}


def test_different_capture_times_are_not_merged():
    a = _hand("a.jpg", BASE, exif_time=datetime(2023, 6, 15, 8, 30, 45))
    b = _hand("b.jpg", NEAR, exif_time=datetime(2023, 6, 15, 8, 30, 50))
    res = deduplicate([a, b])
    assert not res.drop and res.flagged[0][3] == "different capture times"


def test_burst_within_one_second_told_apart_by_subseconds():
    a = _hand("a.jpg", BASE, exif_time=datetime(2023, 6, 15, 8, 30, 45, 100000), subsec=True)
    b = _hand("b.jpg", NEAR, exif_time=datetime(2023, 6, 15, 8, 30, 45, 400000), subsec=True)
    assert not deduplicate([a, b]).drop


def test_sidecar_utc_time_matches_local_exif_time_across_zones():
    utc = 1686839445  # 2023-06-15 14:30:45 UTC
    takeout = _hand("takeout.jpg", BASE, sidecar=True, sidecar_time=utc)
    for local, same in (
        (datetime(2023, 6, 15, 23, 30, 45), True),    # Tokyo, +09:00
        (datetime(2023, 6, 15, 20, 0, 45), True),     # India, +05:30
        (datetime(2023, 6, 15, 23, 31, 30), False),   # 45 s off any zone
    ):
        album = _hand("album.jpg", NEAR, exif_time=local)
        assert bool(deduplicate([takeout, album]).drop) is same, local


def test_different_shape_or_color_is_not_merged():
    for other in (
        _hand("b.jpg", NEAR, size=(100, 80)),
        _hand("b.jpg", NEAR, color=(100.0, 100.0, 160.0)),
    ):
        res = deduplicate([_hand("a.jpg", BASE), other])
        assert not res.drop and res.flagged


def test_flat_images_of_different_colors_are_all_kept(tmp_path):
    # A uniform image hashes to 0, so all of these looked "certainly the same".
    specs = [((220, 20, 20), (64, 48), "JPEG", "jpg"), ((20, 20, 220), (32, 32), "PNG", "png"),
             ((20, 200, 20), (40, 40), "GIF", "gif"), ((250, 250, 250), (50, 40), "BMP", "bmp"),
             ((0, 0, 0), (64, 64), "WEBP", "webp"), ((128, 128, 128), (30, 30), "TIFF", "tif")]
    items = []
    for i, (color, size, fmt, ext) in enumerate(specs):
        path = tmp_path / f"flat{i}.{ext}"
        Image.new("RGB", size, color).save(path, fmt)
        items.append(_item(path))
    assert all(it.low_information for it in items)
    res = deduplicate(items)
    assert len(res.keep) == len(specs) and not res.drop


def test_rotated_copy_still_matches(tmp_path):
    # The Takeout copy stores sideways pixels plus an orientation tag; the album
    # copy has the pixels already turned upright.
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    tagged = _make_image(tmp_path / "tagged.jpg", seed=5, size=(96, 64), quality=95, exif=exif)
    with Image.open(tagged) as im:
        im.transpose(Image.Transpose.ROTATE_270).save(tmp_path / "upright.jpg", "JPEG",
                                                      quality=60)

    takeout, album = _item(tagged, has_sidecar=True), _item(tmp_path / "upright.jpg")
    assert (takeout.width, takeout.height) == (64, 96) == (album.width, album.height)
    res = deduplicate([takeout, album])
    assert res.drop == {album.path: takeout.path}


def test_hash_index_finds_exactly_what_a_full_scan_finds():
    rng = random.Random(7)
    hashes = [rng.getrandbits(64) for _ in range(2000)]
    # Plant near neighbours at every distance up to the guaranteed radius.
    for d in range(_HashIndex.MAX_RADIUS + 1):
        flip = 0
        for bit in rng.sample(range(64), d):
            flip |= 1 << bit
        hashes.append(hashes[d] ^ flip)
    index = _HashIndex()
    for i, h in enumerate(hashes):
        index.add(h, i)
    for query in hashes[:60] + [rng.getrandbits(64) for _ in range(40)]:
        for radius in (CERTAIN_DISTANCE, BORDERLINE_DISTANCE, _HashIndex.MAX_RADIUS):
            found = sorted(i for _, i in index.search(query, radius))
            expected = [i for i, h in enumerate(hashes) if hamming(query, h) <= radius]
            assert found == expected


def test_large_library_dedups_quickly():
    # Was quadratic: 5.4 s at 5,000 items, about 36 minutes at 100,000.
    rng = random.Random(11)
    items = [_hand(f"{i}.jpg", rng.getrandbits(64)) for i in range(20000)]
    start = time.perf_counter()
    deduplicate(items)
    assert time.perf_counter() - start < 60


def test_heic_copy_is_compared_visually(tmp_path):
    from takeout_organizer import deduplicator
    if deduplicator.HEIC_SUPPORT is None:
        pytest.skip("HEIC support is the optional [heic] extra")
    # Writing a test HEIC needs an encoder: pillow-heif, a development dependency.
    # The tool itself only decodes.
    pillow_heif = pytest.importorskip("pillow_heif", reason="needed to write a test HEIC")
    pillow_heif.register_heif_opener()
    original = _make_image(tmp_path / "IMG_1.jpg", seed=9, size=(96, 96), quality=95)
    with Image.open(original) as im:
        im.save(tmp_path / "IMG_1.HEIC", "HEIF", quality=60)

    heic = _item(tmp_path / "IMG_1.HEIC", has_sidecar=True)
    assert heic.phash is not None and (heic.width, heic.height) == (96, 96)
    res = deduplicate([heic, _item(original)])
    assert res.drop == {original: heic.path}
