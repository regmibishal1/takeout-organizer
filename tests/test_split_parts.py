"""A Takeout split into parts can hold the same path in two parts: the same
file twice (kept once), or two different files (both kept)."""

from __future__ import annotations

import io
import json
import shutil
import zipfile

import pytest
from PIL import Image

from takeout_organizer.main_processor import MainProcessor

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")
G = "Takeout/Google Photos/Photos from 2023"


def _jpeg(color) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(data, "JPEG")
    return data.getvalue()


def _sidecar(name: str) -> str:
    return json.dumps({"title": name, "photoTakenTime": {"timestamp": "1686839445"}})


@needs_exiftool
def test_the_same_path_in_two_parts(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    same, first, second = _jpeg((200, 30, 30)), _jpeg((30, 200, 30)), _jpeg((30, 30, 200))
    for part, other in (("001", first), ("002", second)):
        with zipfile.ZipFile(src / f"takeout-20230615T120000Z-{part}.zip", "w") as zf:
            zf.writestr(f"{G}/SAME.jpg", same)               # byte for byte the same
            zf.writestr(f"{G}/SAME.jpg.json", _sidecar("SAME.jpg"))
            zf.writestr(f"{G}/OTHER.jpg", other)             # a different photo each time
    processor = MainProcessor(str(src), str(dest), dedup_mode="off", time_zone="+00:00")
    assert processor.run()
    names = sorted(p.name for p in dest.rglob("*.jpg") if ".takeout-organizer" not in p.parts)
    assert names == ["OTHER.jpg", "OTHER_1.jpg", "SAME.jpg"]
    assert processor.summary["balanced"] and processor.summary["written"] == 3
    contents = {p.read_bytes()[-50:] for p in dest.rglob("OTHER*.jpg")}
    assert len(contents) == 2   # neither photo replaced the other
