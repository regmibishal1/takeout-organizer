"""Recognizing files by content, and extensions that do not match it."""

from __future__ import annotations

import struct

import pytest
from PIL import Image

from takeout_organizer.filetypes import extension_mismatch, kind, sniff


@pytest.mark.parametrize("fmt, ext, expected", [
    ("JPEG", "jpg", "jpeg"), ("PNG", "png", "png"), ("GIF", "gif", "gif"),
    ("WEBP", "webp", "webp"), ("BMP", "bmp", "bmp"), ("TIFF", "tif", "tiff"),
])
def test_images_are_recognized_by_content(tmp_path, fmt, ext, expected):
    path = tmp_path / f"x.{ext}"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(path, fmt)
    assert sniff(str(path)) == expected and kind(str(path)) == "image"


def _box(kind_: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind_ + payload


@pytest.mark.parametrize("head, expected", [
    (_box(b"ftyp", b"isom\0\0\x02\0isommp41"), "mp4"),
    (_box(b"ftyp", b"qt  \0\0\0\0qt  "), "mp4"),
    (_box(b"ftyp", b"heic\0\0\0\0mif1heic"), "heic"),
    (_box(b"ftyp", b"avif\0\0\0\0avifmif1"), "avif"),
    (_box(b"ftyp", b"crx \0\0\0\x01crx "), "cr3"),
    (_box(b"moov", b"\0" * 16), "mp4"),
    (b"RIFF\0\0\0\0AVI LIST", "avi"),
    (b"\x1a\x45\xdf\xa3" + b"\0" * 12, "mkv"),
    (b"\x00\x00\x01\xba" + b"\0" * 12, "mpeg-ps"),
])
def test_containers_are_recognized_by_content(tmp_path, head, expected):
    path = tmp_path / "clip.bin"
    path.write_bytes(head + b"\0" * 64)
    assert sniff(str(path)) == expected


def test_camcorder_transport_streams(tmp_path):
    ts = tmp_path / "a.ts"
    ts.write_bytes((b"\x47" + b"\0" * 187) * 4)
    m2ts = tmp_path / "b.mts"
    m2ts.write_bytes((b"\0\0\0\0\x47" + b"\0" * 187) * 4)
    assert sniff(str(ts)) == "mpeg-ts" and sniff(str(m2ts)) == "mpeg-ts"


def test_non_media_is_not_recognized(tmp_path):
    for name, data in (("notes.txt", b"hello"), ("page.html", b"<html></html>"),
                       ("empty.jpg", b"")):
        path = tmp_path / name
        path.write_bytes(data)
        assert sniff(str(path)) is None and kind(str(path)) is None


def test_wrong_extension_is_detected(tmp_path):
    fake_heic = tmp_path / "IMG_1.HEIC"
    Image.new("RGB", (8, 8)).save(fake_heic, "JPEG")
    assert extension_mismatch(str(fake_heic)) == ("jpeg", ".jpg")

    for name, fmt in (("ok.jpeg", "JPEG"), ("raw.DNG", "TIFF"), ("ok.png", "PNG")):
        path = tmp_path / name
        Image.new("RGB", (8, 8)).save(path, fmt)
        assert extension_mismatch(str(path)) is None, name

    unknown = tmp_path / "data.heic"
    unknown.write_bytes(b"not an image")
    assert extension_mismatch(str(unknown)) is None  # unknown content: no claim
