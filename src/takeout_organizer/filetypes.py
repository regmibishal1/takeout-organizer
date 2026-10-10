"""What a file really is, from its first bytes rather than its name.

Takeout contains files whose extension does not match their content (for
example `.heic` files that are JPEGs), and media with extensions the name-based
lists do not know. ExifTool refuses to write a file whose extension disagrees
with its content, so both need the content checked.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

# Format -> (kind, extension used when fixing a wrong one)
FORMATS = {
    'jpeg': ('image', '.jpg'),
    'png': ('image', '.png'),
    'gif': ('image', '.gif'),
    'webp': ('image', '.webp'),
    'bmp': ('image', '.bmp'),
    'tiff': ('image', '.tif'),
    'heic': ('image', '.heic'),
    'avif': ('image', '.avif'),
    'cr3': ('image', '.cr3'),
    'mp4': ('video', '.mp4'),   # ISO media: MP4, MOV, 3GP and relatives
    'avi': ('video', '.avi'),
    'mkv': ('video', '.mkv'),
    'mpeg-ts': ('video', '.mts'),
    'mpeg-ps': ('video', '.mpg'),
}

# Extensions each format may carry. Camera RAW files are TIFF inside.
_EXTENSIONS = {
    'jpeg': {'.jpg', '.jpeg', '.jpe', '.jfif'},
    'png': {'.png'},
    'gif': {'.gif'},
    'webp': {'.webp'},
    'bmp': {'.bmp', '.dib'},
    'tiff': {'.tif', '.tiff', '.dng', '.cr2', '.nef', '.arw', '.orf', '.rw2', '.raw', '.pef',
             '.srw', '.erf', '.3fr', '.nrw', '.sr2'},
    'heic': {'.heic', '.heif', '.hif'},
    'avif': {'.avif'},
    'cr3': {'.cr3'},
    'mp4': {'.mp4', '.mov', '.m4v', '.3gp', '.3g2', '.mp', '.mv', '.qt'},
    'avi': {'.avi'},
    'mkv': {'.mkv', '.webm'},
    'mpeg-ts': {'.mts', '.m2ts', '.ts'},
    'mpeg-ps': {'.mpg', '.mpeg', '.vob', '.mod'},
}

_HEIC_BRANDS = {b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'mif1', b'msf1'}
_AVIF_BRANDS = {b'avif', b'avis'}


def sniff(path: str) -> Optional[str]:
    """The format of a file from its first bytes, or None if not recognized."""
    try:
        with open(path, 'rb') as f:
            head = f.read(32)
    except OSError:
        return None

    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    if head[:2] == b'BM' and len(head) >= 14:
        return 'bmp'
    if head[:4] in (b'II*\x00', b'MM\x00*', b'IIRO', b'IIU\x00'):
        return 'tiff'  # also most camera RAW formats
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'mkv'
    if head[:4] == b'\x00\x00\x01\xba':
        return 'mpeg-ps'
    if head[:1] == b'\x47' or head[4:5] == b'\x47':
        # MPEG transport stream: sync byte every 188 bytes (192 in M2TS).
        if _is_transport_stream(path):
            return 'mpeg-ts'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return 'heic'
        if brand in _AVIF_BRANDS:
            return 'avif'
        if brand == b'crx ':
            return 'cr3'
        return 'mp4'
    if head[4:8] in (b'moov', b'mdat', b'wide', b'free', b'skip'):
        return 'mp4'  # old QuickTime files without an ftyp box
    return None


def _is_transport_stream(path: str) -> bool:
    """MPEG-TS has the sync byte 0x47 every 188 bytes; M2TS (camcorder .mts)
    every 192 bytes, after a 4-byte timestamp."""
    try:
        with open(path, 'rb') as f:
            data = f.read(4 + 192 * 3 + 1)
    except OSError:
        return False
    for offset, packet in ((0, 188), (4, 192)):
        positions = [offset + packet * i for i in range(3)]
        if positions[-1] < len(data) and all(data[p] == 0x47 for p in positions):
            return True
    return False


def kind(path: str) -> Optional[str]:
    """'image' or 'video' from the content, or None if not recognized."""
    fmt = sniff(path)
    return FORMATS[fmt][0] if fmt else None


def extension_mismatch(path: str) -> Optional[Tuple[str, str]]:
    """(format, correct extension) when the content is a known format that the
    file's extension does not belong to; None when they agree or when the
    content is not recognized."""
    fmt = sniff(path)
    if fmt is None:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXTENSIONS[fmt]:
        return None
    return fmt, FORMATS[fmt][1]
