"""The HTML report: content, structure for screen readers and keyboards, contrast."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from PIL import Image

from takeout_organizer import report
from takeout_organizer.main_processor import MainProcessor


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []          # (tag, attrs)
        self.focusable = []     # tags a keyboard reaches, in order
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag in ("a", "button", "input", "summary") and attrs.get("href", "x"):
            self.focusable.append((tag, attrs))

    def handle_data(self, data):
        self.text.append(data)


def _parse(page: str) -> _Collector:
    collector = _Collector()
    collector.feed(page)
    return collector


def _blocky(path, seed, size=(64, 64), quality=90):
    rng = random.Random(seed)
    base = Image.new("RGB", (8, 8))
    base.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                  for _ in range(64)])
    base.resize(size, Image.NEAREST).save(path, "JPEG", quality=quality)


@pytest.fixture
def export_with_duplicates(tmp_path):
    root = tmp_path / "src"
    photos = root / "Takeout" / "Google Photos" / "Photos from 2023"
    album = root / "Takeout" / "Google Photos" / "Shared Album"
    photos.mkdir(parents=True)
    album.mkdir(parents=True)
    _blocky(photos / "IMG.jpg", seed=7, quality=92)
    (photos / "IMG.jpg.json").write_text(json.dumps(
        {"title": "IMG.jpg", "photoTakenTime": {"timestamp": "1686839445"}}))
    with Image.open(photos / "IMG.jpg") as im:
        im.save(album / "DUP.jpg", "JPEG", quality=25)
    _blocky(album / "DSC_0001.jpg", seed=200)       # no date anywhere: _undated
    return root


def test_dry_run_report_has_the_decisions_and_thumbnails(export_with_duplicates, tmp_path):
    processor = MainProcessor(str(export_with_duplicates), str(tmp_path / "out"), dry_run=True)
    assert processor.run()
    page_path = Path(processor.report_path)
    assert page_path.parent == Path(processor.output_dir)  # never in dest for a dry run
    page = page_path.read_text(encoding="utf-8")

    assert "Preview only: nothing was written." in page
    assert "Duplicates removed" in page and "DUP.jpg" in page
    assert "Not copied (duplicate)" in page and "Kept" in page
    assert "DSC_0001.jpg" in page  # listed under guessed dates
    images = [attrs for tag, attrs in _parse(page).tags if tag == "img"]
    assert len(images) == 2
    for attrs in images:
        assert (page_path.parent / attrs["src"]).exists()
        assert attrs["alt"] and attrs["loading"] == "lazy"


def test_report_structure_for_screen_readers_and_keyboards(export_with_duplicates, tmp_path):
    processor = MainProcessor(str(export_with_duplicates), str(tmp_path / "out"), dry_run=True)
    assert processor.run()
    page = Path(processor.report_path).read_text(encoding="utf-8")
    parsed = _parse(page)
    tags = [tag for tag, _ in parsed.tags]

    html_attrs = dict(parsed.tags[0][1]) if parsed.tags[0][0] == "html" else {}
    assert html_attrs.get("lang") == "en"
    assert re.search(r"<title>Takeout Organizer report, \d+ \w+ \d{4}</title>", page)
    for landmark in ("header", "nav", "main", "footer"):
        assert landmark in tags
    first = parsed.focusable[0]
    assert first[1].get("href") == "#summary" and "skip" in first[1].get("class", "")
    assert any(attrs.get("role") == "status" for _, attrs in parsed.tags)
    assert tags.count("table") == tags.count("caption")
    assert all(attrs.get("scope") == "col" for tag, attrs in parsed.tags if tag == "th")
    assert all(attrs.get("alt") for tag, attrs in parsed.tags if tag == "img")
    ids = [attrs["id"] for _, attrs in parsed.tags if "id" in attrs]
    assert len(ids) == len(set(ids))
    for _, attrs in parsed.tags:  # every in-page link has a target
        if attrs.get("href", "").startswith("#"):
            assert attrs["href"][1:] in ids


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_colors_meet_wcag_contrast(scheme):
    blocks = re.findall(r":root \{([^}]*)\}", report._CSS)
    tokens = dict(re.findall(r"--([\w-]+): (#[0-9A-Fa-f]{6})", blocks[0 if scheme == "light" else 1]))
    for surface in ("surface", "surface-alt"):
        for text in ("text", "text-muted", "primary", "success", "warning", "error"):
            assert _contrast(tokens[text], tokens[surface]) >= 4.5, (text, surface)
        for part in ("border-input", "focus"):
            assert _contrast(tokens[part], tokens[surface]) >= 3, (part, surface)
    assert _contrast(tokens["primary"], tokens["track"]) >= 3


def test_names_are_escaped_and_mismatch_is_shown():
    data = report.ReportData(
        title_date=datetime(2026, 10, 9, 5, 25), source="src", dest="dest", dry_run=False,
        settings=[("Folder layout", "year-month-day")], summary_lines=["Input files  1"],
        summary={"input": 1, "balanced": False},
        failed=[('<script>alert(1)</script>.jpg', "could not be copied")],
    )
    page = report.render(data)
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "MISMATCH" in page and 'class="banner problem"' in page


def test_real_run_puts_the_report_next_to_the_photos(export_with_duplicates, tmp_path):
    if shutil.which("exiftool") is None:
        pytest.skip("ExifTool not installed")
    dest = tmp_path / "out"
    processor = MainProcessor(str(export_with_duplicates), str(dest))
    assert processor.run()
    assert Path(processor.report_path) == dest / "takeout-organizer-report.html"
    thumbs = dest / ".takeout-organizer" / "report-thumbs"
    assert sorted(os.listdir(thumbs)) == ["1.jpg", "2.jpg"]
