"""The balance line is the tool's promise: every input file is written,
already there, a duplicate, copied to _other/, or failed, exactly once.
Hypothesis builds random exports to look for one that breaks it."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from hypothesis import HealthCheck, Phase, given, settings, strategies as st
from PIL import Image

from takeout_organizer.main_processor import MainProcessor

G = "Takeout/Google Photos"
FOLDERS = ("Photos from 2023", "Photos from 2024", "Trip")

photo = st.fixed_dictionaries({
    "folder": st.sampled_from(FOLDERS),
    "kind": st.sampled_from(("jpg", "png", "heic-named-jpg")),
    "sidecar": st.sampled_from(("own", "none", "elsewhere")),
    "copies": st.integers(0, 2),        # identical copies in other folders
    "video": st.booleans(),             # a Live Photo video beside it
    "damaged": st.booleans(),           # its ZIP entry fails its checksum
    "zip": st.integers(0, 1),
})
export = st.fixed_dictionaries({
    "photos": st.lists(photo, max_size=6),
    "others": st.integers(0, 2),        # files that are not photos or videos
    "stray_sidecars": st.integers(0, 2),  # sidecars whose photo is not in the export
    "rerun": st.booleans(),
})


def _image(seed: int, kind: str) -> bytes:
    data = io.BytesIO()
    pixels = bytes((seed * 37 + i * 11) % 256 for i in range(16 * 12 * 3))
    Image.frombytes("RGB", (16, 12), pixels).save(data, "PNG" if kind == "png" else "JPEG")
    return data.getvalue()


def _write_export(src: Path, spec: dict) -> int:
    """The export as ZIPs; returns the number of input files it holds."""
    entries = {0: [], 1: []}
    damaged = set()
    inputs = 0
    for i, p in enumerate(spec["photos"]):
        ext = {"jpg": ".jpg", "png": ".png", "heic-named-jpg": ".heic"}[p["kind"]]
        name = f"IMG_{i:04d}{ext}"
        body = _image(i, p["kind"])
        path = f"{G}/{p['folder']}/{name}"
        entries[p["zip"]].append((path, body))
        inputs += 1
        if p["damaged"]:
            damaged.add(path)
        for c in range(p["copies"]):
            folder = FOLDERS[(FOLDERS.index(p["folder"]) + c + 1) % len(FOLDERS)]
            entries[p["zip"]].append((f"{G}/{folder}/{name}", body))
            inputs += 1
        if p["video"]:
            entries[p["zip"]].append((f"{G}/{p['folder']}/IMG_{i:04d}.MP4",
                                      b"\0\0\0\x18ftypisom\0\0\x02\0isommp41" + bytes(64)))
            inputs += 1
        meta = json.dumps({"title": name, "photoTakenTime": {"timestamp": str(1686839445 + i)}})
        if p["sidecar"] == "own":
            entries[p["zip"]].append((f"{path}.supplemental-metadata.json", meta))
        elif p["sidecar"] == "elsewhere":
            entries[1 - p["zip"]].append((f"{G}/Trip/{name}.json", meta))
    for i in range(spec["others"]):
        entries[0].append((f"{G}/Trip/notes_{i}.txt", f"note {i}"))
        inputs += 1
    for i in range(spec["stray_sidecars"]):
        entries[1].append((f"{G}/Trip/GONE_{i}.jpg.json",
                           json.dumps({"title": f"GONE_{i}.jpg"})))
    entries[0].append(("Takeout/archive_browser.html", "<html></html>"))   # not counted

    for number, files in entries.items():
        if not files:
            continue
        target = src / f"takeout-00{number + 1}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
            for path, body in files:
                zf.writestr(path, body)
        if damaged & {path for path, _ in files}:
            _damage(target, damaged)
    return inputs


def _damage(target: Path, members: set) -> None:
    """Flip a byte inside each listed member, so its CRC check fails."""
    data = bytearray(target.read_bytes())
    with zipfile.ZipFile(target) as zf:
        for info in zf.infolist():
            if info.filename in members:
                start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
                data[start + info.compress_size // 2] ^= 0xFF
    target.write_bytes(bytes(data))


def _check(processor: MainProcessor, inputs: int) -> None:
    s = processor.summary
    assert s["balanced"], processor.summary_lines
    assert s["input"] == inputs
    assert s["written"] + s["already"] + s["duplicates"] + s["other"] + s["failed"] == inputs


def _in_other(dest: Path) -> int:
    return sum(1 for p in (dest / "_other").rglob("*") if p.is_file())


# Each example is a whole run, so a failing one is reported as found: shrinking
# it would take minutes. Every CI job tries other random exports.
@settings(max_examples=12, deadline=None, phases=[Phase.explicit, Phase.generate],
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(spec=export)
def test_every_input_file_is_accounted_for_exactly_once(spec):
    base = Path(tempfile.mkdtemp(prefix="balance_"))
    try:
        src, dest = base / "src", base / "out"
        src.mkdir()
        inputs = _write_export(src, spec)
        exiftool = shutil.which("exiftool") is not None
        first = MainProcessor(str(src), str(dest), dry_run=not exiftool, time_zone="+00:00")
        first.run()
        _check(first, inputs)
        if exiftool:
            assert _in_other(dest) == first.summary["other"]
        if exiftool and spec["rerun"]:
            # A second run over the same export adds nothing and still balances.
            again = MainProcessor(str(src), str(dest), time_zone="+00:00")
            again.run()
            _check(again, inputs)
            assert again.summary["written"] == 0 and again.summary["other"] == 0
            assert _in_other(dest) == first.summary["other"]
    finally:
        shutil.rmtree(base, ignore_errors=True)
