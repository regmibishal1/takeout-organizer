"""Sidecar naming cases from tests/fixtures/takeout_names.csv.

Takeout changes its naming without notice. Each row is one case: a media file,
the sidecar next to it (and its `title`), any other files in the folder, and
which sidecar and rule must win (empty: none). A newly reported pattern
becomes one row here plus a fix. Non-ASCII characters are written as \\uXXXX.
"""

from __future__ import annotations

import csv
import json
import os
import unicodedata
from pathlib import Path

import pytest

from takeout_organizer.file_matcher import FileMatcher

FIXTURE = Path(__file__).parent / "fixtures" / "takeout_names.csv"


def _decode(text: str) -> str:
    return text.encode("ascii").decode("unicode_escape")


def _rows():
    with open(FIXTURE, encoding="ascii", newline="") as f:
        return [pytest.param({k: _decode(v) for k, v in row.items()}, id=row["case"])
                for row in csv.DictReader(f)]


@pytest.mark.parametrize("row", _rows())
def test_takeout_naming(tmp_path, row):
    (tmp_path / row["media"]).write_bytes(b"media")
    for extra in filter(None, row["extra_files"].split(";")):
        (tmp_path / extra).write_bytes(b"media")
    sidecar = {} if row["title"] == "-" else {"title": row["title"]}
    (tmp_path / row["sidecar"]).write_text(json.dumps(sidecar), encoding="utf-8")

    pairs, _, _ = FileMatcher().match_files(str(tmp_path))

    def nfc(name: str) -> str:
        return unicodedata.normalize("NFC", name)

    found = {nfc(os.path.basename(p["media"])): p for p in pairs}
    pair = found.get(nfc(row["media"]))
    if not row["expected_sidecar"]:
        assert pair is None, f"matched {pair and os.path.basename(pair['json'])}"
        return
    assert pair is not None, "no sidecar found"
    assert nfc(os.path.basename(pair["json"])) == nfc(row["expected_sidecar"])
    assert pair["rule"] == row["expected_rule"]
