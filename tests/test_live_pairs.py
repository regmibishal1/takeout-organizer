"""Live and motion photos: the video goes in its photo's folder, even when
neither has a sidecar and their own dates fall on different days."""

from __future__ import annotations

import shutil
import struct
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer.main_processor import MainProcessor

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
ALBUM = "Takeout/Google Photos/Trip"
EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)


def _mp4(created: datetime) -> bytes:
    """A minimal MP4 whose QuickTime CreateDate (UTC) is created."""
    def box(kind, payload):
        return struct.pack(">I", 8 + len(payload)) + kind + payload
    stamp = int((created - EPOCH_1904).total_seconds())
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", bytes(4) + struct.pack(">II", stamp, stamp)
               + struct.pack(">IIIH", 1000, 0, 0x10000, 0x100)
               + bytes(10) + matrix + bytes(24) + struct.pack(">I", 2))
    return box(b"ftyp", b"isom\0\0\x02\0isommp41") + box(b"moov", mvhd)


def _photo(tmp_path, name: str, taken: str) -> Path:
    path = tmp_path / name
    Image.new("RGB", (16, 12), (len(name) * 30 % 255, 80, 20)).save(path, "JPEG")
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-DateTimeOriginal={taken}",
                    str(path)], check=True)
    return path


def _export(tmp_path) -> Path:
    """A shared-album download: no sidecars. The photo was taken at 23:30 on
    30 June, local time; its video stores the same moment as 1 July in UTC."""
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "album.zip", "w") as zf:
        zf.write(_photo(tmp_path, "IMG_1.jpg", "2023:06:30 23:30:00"), f"{ALBUM}/IMG_1.jpg")
        zf.writestr(f"{ALBUM}/IMG_1.MP4", _mp4(datetime(2023, 7, 1, 5, 30, tzinfo=timezone.utc)))
        # A video that is not part of a pair keeps its own date.
        zf.writestr(f"{ALBUM}/CLIP.MP4", _mp4(datetime(2023, 7, 1, 5, 30, tzinfo=timezone.utc)))
    return src


def _names(dest: Path) -> list:
    return sorted(str(p.relative_to(dest)).replace("\\", "/") for p in dest.rglob("*")
                  if p.is_file() and ".takeout-organizer" not in p.parts
                  and p.suffix not in (".html", ".m3u8", ".json"))


@needs_exiftool
def test_a_video_goes_in_its_photos_folder(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), layout="year-month-day", dedup_mode="off",
                         time_zone="+00:00").run()
    assert _names(dest) == ["2023/06/30/IMG_1.MP4", "2023/06/30/IMG_1.jpg",
                            "2023/07/01/CLIP.MP4"]


@needs_exiftool
def test_with_rename_the_video_is_named_by_its_photos_date(tmp_path):
    src, dest = _export(tmp_path), tmp_path / "out"
    assert MainProcessor(str(src), str(dest), dedup_mode="off", time_zone="+00:00",
                         rename="{date:%Y%m%d_%H%M}_{name}").run()
    assert _names(dest) == ["2023/06/20230630_2330_IMG_1.MP4", "2023/06/20230630_2330_IMG_1.jpg",
                            "2023/07/20230701_0530_CLIP.MP4"]
