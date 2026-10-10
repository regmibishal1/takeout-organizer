"""Pipeline-level dedup: a re-compressed album copy is dropped, the sidecar copy
is kept, and a unique sidecar-less group photo is carried into the output.

Runs in dry-run (no ExifTool needed) and inspects the resulting plan.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

from PIL import Image

from takeout_organizer.main_processor import MainProcessor


def _blocky(path, seed, size=(64, 64), quality=90):
    rng = random.Random(seed)
    base = Image.new("RGB", (8, 8))
    bp = base.load()
    for y in range(8):
        for x in range(8):
            bp[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    base.resize(size, Image.NEAREST).save(path, "JPEG", quality=quality)


def test_dedup_drops_recompressed_copy_and_keeps_unique_orphan(tmp_path):
    root = tmp_path / "src"
    photos = root / "Takeout" / "Google Photos" / "Photos from 2023"
    album = root / "Takeout" / "Google Photos" / "Shared Album"
    photos.mkdir(parents=True)
    album.mkdir(parents=True)

    # Takeout copy (has sidecar) ...
    _blocky(photos / "IMG.jpg", seed=7, quality=92)
    (photos / "IMG.jpg.json").write_text(json.dumps({
        "title": "IMG.jpg", "photoTakenTime": {"timestamp": "1686839445"}}))
    # ... and a re-compressed copy of the same photo from a shared album (no sidecar).
    with Image.open(photos / "IMG.jpg") as im:
        im.save(album / "DUP.jpg", "JPEG", quality=25)
    # A genuinely different group photo, also with no sidecar.
    _blocky(album / "GROUP.jpg", seed=200, quality=90)

    proc = MainProcessor(
        source_folder=str(root), dest_folder=str(tmp_path / "out"),
        dry_run=True, dedup_mode="perceptual",
    )
    assert proc.run()

    kept_with_sidecar = {os.path.basename(p["media"]) for p in proc.matched_pairs}
    orphans = {os.path.basename(p) for p in proc.keep_orphans}

    assert kept_with_sidecar == {"IMG.jpg"}     # sidecar copy survives
    assert "GROUP.jpg" in orphans               # unique group photo carried forward
    assert "DUP.jpg" not in orphans             # re-compressed duplicate dropped

    # A duplicates report is written (even in dry-run) so decisions are
    # reviewable; a dry run puts it in the preview folder, never in the dest.
    assert not (tmp_path / "out").exists()
    report = (Path(proc.output_dir) / ".takeout-organizer" / "duplicates.md").read_text(
        encoding="utf-8")
    assert "Removed 1 duplicate" in report
    assert "DUP.jpg" in report and "IMG.jpg" in report


def test_dedup_off_keeps_all_sidecarless_media(tmp_path):
    root = tmp_path / "src" / "Takeout" / "Google Photos" / "Album"
    root.mkdir(parents=True)
    _blocky(root / "A.jpg", seed=1)
    _blocky(root / "B.jpg", seed=2)
    proc = MainProcessor(
        source_folder=str(tmp_path / "src"), dest_folder=str(tmp_path / "out"),
        dry_run=True, dedup_mode="off",
    )
    assert proc.run()
    assert {os.path.basename(p) for p in proc.keep_orphans} == {"A.jpg", "B.jpg"}
