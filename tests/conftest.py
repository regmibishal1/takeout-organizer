"""Shared fixtures: build synthetic Google Takeout directories on disk.

These reproduce the real-world filename patterns Google Photos Takeout
produces - including the awkward ones (truncated JSON names, duplicate
counters, edited variants, live-photo pairs) that trip up naive matchers.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# A test still running this long is taken as hung and the run ends, instead of
# holding a CI runner until the job times out. pytest prints every thread's
# stack first (faulthandler_timeout in pyproject.toml, which is shorter).
HANG_SECONDS = 180

# The window follows the system's language; tests read its English text.
os.environ["LANGUAGE"] = "en"


@pytest.fixture(autouse=True)
def _english_after_each_test():
    yield
    from takeout_organizer import i18n
    i18n.activate("en")


def _hung(name: str) -> None:
    sys.__stderr__.write(f"\n{name} ran longer than {HANG_SECONDS} seconds; ending the run.\n")
    sys.__stderr__.flush()
    os._exit(1)


@pytest.fixture(autouse=True)
def _end_the_run_if_a_test_hangs(request):
    timer = threading.Timer(HANG_SECONDS, _hung, args=(request.node.nodeid,))
    timer.daemon = True
    timer.start()
    yield
    timer.cancel()


def _write_media(path: Path, content: bytes = b"\xff\xd8\xff\xe0fake-jpeg") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_sidecar(path: Path, title: str, timestamp: str = "1609459200") -> None:
    """Write a minimal Takeout JSON sidecar (photoTakenTime is what matters)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": title,
        "photoTakenTime": {"timestamp": timestamp, "formatted": "Jan 1, 2021"},
        "geoData": {"latitude": 0.0, "longitude": 0.0},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def takeout_dir(tmp_path: Path) -> Path:
    """A synthetic Takeout export covering the main matching scenarios."""
    root = tmp_path / "Takeout" / "Google Photos" / "Album"

    # 1. Exact match: photo.jpg + photo.jpg.json
    _write_media(root / "photo.jpg")
    _write_sidecar(root / "photo.jpg.json", "photo.jpg")

    # 2. No-extension sidecar: sunset.jpg + sunset.json
    _write_media(root / "sunset.jpg")
    _write_sidecar(root / "sunset.json", "sunset.jpg")

    # 3. Edited variant: beach.jpg + beach-edited.jpg + beach.jpg.json
    _write_media(root / "beach.jpg")
    _write_media(root / "beach-edited.jpg")
    _write_sidecar(root / "beach.jpg.json", "beach.jpg")

    # 4. Duplicate counter: IMG_1.jpg(1).json  <-  IMG_1(1).jpg
    _write_media(root / "IMG_1(1).jpg")
    _write_sidecar(root / "IMG_1.jpg(1).json", "IMG_1.jpg")

    # 5. Live-photo pair: IMG_2.HEIC + IMG_2.MOV share IMG_2.HEIC.json
    _write_media(root / "IMG_2.HEIC")
    _write_media(root / "IMG_2.MOV")
    _write_sidecar(root / "IMG_2.HEIC.json", "IMG_2.HEIC")

    # 6. Unmatched media (no sidecar at all)
    _write_media(root / "orphan.png")

    return root
