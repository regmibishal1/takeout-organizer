"""Takeout ZIPs on a NAS or a USB disk: entries that cannot be read from the
ZIP are read again from a local copy, once, on every OS; damaged entries are
counted as unreadable without one."""

from __future__ import annotations

import logging
import zipfile

import pytest

from takeout_organizer import file_processor
from takeout_organizer.file_processor import FileProcessor

NAMES = ("a.jpg", "b.jpg", "c.jpg")


@pytest.fixture
def export(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    target = src / "takeout-001.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
        for name in NAMES:
            zf.writestr(f"Takeout/Google Photos/Trip/{name}", name.encode() * 500)
    with zipfile.ZipFile(target) as zf:
        b_offset = zf.getinfo("Takeout/Google Photos/Trip/b.jpg").header_offset
        entries_end = zf.start_dir      # the ZIP's list of files follows the entries
    return src, target, range(b_offset, entries_end)


def _flaky(monkeypatch, fails):
    """Reads of a ZIP fail where fails(path, position) says so."""
    real_read = file_processor._SourceFile._read

    def read(self, size):
        if fails(self.name, self.tell()):
            raise OSError(5, "The network path was not found")
        return real_read(self, size)
    monkeypatch.setattr(file_processor._SourceFile, "_read", read)


def _extract(tmp_path, src):
    processor = FileProcessor(logging.getLogger("test"), work_dir=str(tmp_path))
    dirs = processor.extract_zip_files(str(src), str(tmp_path / "out"))
    return processor, dirs


def _extracted(tmp_path):
    return sorted(p.name for p in (tmp_path / "out").rglob("*.jpg"))


def test_entries_that_could_not_be_read_are_read_from_a_copy(tmp_path, export, monkeypatch,
                                                             caplog):
    src, target, b_to_end = export
    # The share drops out from b.jpg on, but only when read in place.
    _flaky(monkeypatch, lambda path, pos: path == str(target) and pos in b_to_end)
    caplog.set_level(logging.INFO)
    processor, dirs = _extract(tmp_path, src)
    assert len(dirs) == 1 and processor.unreadable_members == []
    assert _extracted(tmp_path) == list(NAMES)
    assert (tmp_path / "out" / "takeout-001" / "Takeout" / "Google Photos" / "Trip" / "b.jpg"
            ).read_bytes() == b"b.jpg" * 500
    assert "again from a copy of takeout-001.zip" in caplog.text
    assert "Read 2 of 2 entries from the copy" in caplog.text
    assert not list(tmp_path.glob("zip_copy_*"))   # the copy is removed


def test_a_zip_that_cannot_be_opened_in_place_is_read_from_a_copy(tmp_path, export,
                                                                  monkeypatch):
    src, target, _ = export
    _flaky(monkeypatch, lambda path, pos: path == str(target))
    processor, dirs = _extract(tmp_path, src)
    assert len(dirs) == 1 and _extracted(tmp_path) == list(NAMES)


def test_entries_still_unreadable_are_counted(tmp_path, export, monkeypatch):
    src, target, b_to_end = export
    _flaky(monkeypatch, lambda path, pos: path.endswith(".zip") and pos in b_to_end)
    processor, _ = _extract(tmp_path, src)
    assert _extracted(tmp_path) == ["a.jpg"]
    assert sorted(m for _, m in processor.unreadable_members) == [
        "Takeout/Google Photos/Trip/b.jpg", "Takeout/Google Photos/Trip/c.jpg"]


def test_a_damaged_entry_is_not_copied_for_and_leaves_no_partial_file(tmp_path, export,
                                                                     monkeypatch):
    src, target, b_to_end = export
    data = bytearray(target.read_bytes())
    data[b_to_end.start + 100] ^= 0xFF          # inside b.jpg's data: its checksum fails
    target.write_bytes(bytes(data))
    copies = []
    monkeypatch.setattr(FileProcessor, "_copy_with_progress",
                        lambda self, a, b: copies.append(a))
    processor, _ = _extract(tmp_path, src)
    assert copies == []
    assert _extracted(tmp_path) == ["a.jpg", "c.jpg"]
    assert processor.unreadable_members == [("takeout-001.zip",
                                             "Takeout/Google Photos/Trip/b.jpg")]
