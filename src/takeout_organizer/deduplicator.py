"""Find duplicate photos across a merged Takeout + shared-album import.

The same photo often arrives twice: once from your Takeout (with a JSON sidecar)
and once from a downloaded shared album (usually without one, and re-compressed so
the bytes differ). Exact hashing misses that second case, so we add a perceptual
hash (dHash) that matches images by visual content regardless of re-compression.

A perceptual match alone never deletes a photo. Burst frames, HDR brackets and
retakes of the same pose look almost identical too, so grouping is layered and
conservative:
  - identical bytes                                        -> same photo (dedupe)
  - dHash distance <= CERTAIN_DISTANCE and every second
    signal agrees (see _same_photo)                        -> same photo (dedupe)
  - dHash distance <= BORDERLINE_DISTANCE otherwise        -> possible match (flag,
                                                              keep both, never drop)
  - larger distance                                        -> different photos

Low-information images (blank pages, solid colors, lens-cap shots) all hash
alike, so they are compared by exact bytes only.

Within a confirmed-duplicate group we keep the *best* copy: the one with a JSON
sidecar (so metadata restores), then one with an embedded capture date, then the
highest resolution, then the largest file, then the shortest path.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

try:
    from PIL import Image, ImageOps, ImageStat
except ImportError:  # pragma: no cover - Pillow is a core dependency
    Image = None
    ImageOps = None
    ImageStat = None

# HEIC/HEIF (iPhone photos) can only be compared visually with a HEIC decoder:
# pi-heif (`pip install takeout-organizer[heic]`, decode only, which is all this
# needs, and what the standalone builds ship), or pillow-heif when that is what is
# installed. Without one they are compared by their bytes only.
HEIC_SUPPORT = None
for _module in ('pi_heif', 'pillow_heif'):
    try:
        __import__(_module).register_heif_opener()
    except ImportError:
        continue
    HEIC_SUPPORT = _module.replace('_', '-')
    break

# dHash Hamming-distance thresholds (out of 64 bits).
CERTAIN_DISTANCE = 4      # <= this, with every second signal agreeing: same image
BORDERLINE_DISTANCE = 10  # <= this otherwise: possible match, keep both and flag

# Second signals for a perceptual match.
ASPECT_TOLERANCE = 0.01      # width/height may differ by 1%
COLOR_TOLERANCE = 255 * 0.10  # mean of each RGB channel may differ by 10%
TIME_TOLERANCE_SECONDS = 2   # capture times (whole seconds) may differ by this
SUBSEC_TOLERANCE_SECONDS = 0.05  # ... or by this when both carry sub-seconds

# Low-information guard: a dHash with this few (or this many) bits set, or a
# nearly flat thumbnail, says little about the picture.
MIN_HASH_BITS = 8
MAX_HASH_BITS = 56
MIN_THUMB_STDDEV = 4.0

# A sidecar time is UTC while EXIF times are local wall-clock time; they agree
# when they differ by a whole time-zone offset (a multiple of 15 minutes).
_ZONE_STEP_SECONDS = 15 * 60
_MAX_ZONE_OFFSET_SECONDS = 14 * 60 * 60

_ORIENTATION = 0x0112
_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_SUBSEC_TIME_ORIGINAL = 37521

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
               ".heic", ".heif", ".avif"}


def _dhash_bits(gray, size: int = 8) -> int:
    """dHash of a (size+1 x size) grayscale image."""
    bits = 0
    i = 0
    for row in range(size):
        for col in range(size):
            if gray.getpixel((col, row)) > gray.getpixel((col + 1, row)):
                bits |= 1 << i
            i += 1
    return bits


def _thumb(im, size: int = 8):
    return im.convert("L").resize((size + 1, size), Image.LANCZOS)


def dhash(path: str, size: int = 8) -> Optional[int]:
    """Perceptual difference-hash: a 64-bit int, or None if the image can't be read.

    Resizes to (size+1 x size) grayscale and encodes whether each pixel is brighter
    than its right neighbour. Robust to re-compression, small edits, and resizing.
    """
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            return _dhash_bits(_thumb(ImageOps.exif_transpose(im), size), size)
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class MediaItem:
    """One media file and the signals used to compare and rank it."""

    path: str
    is_image: bool = True
    has_sidecar: bool = False
    size: int = 0
    width: int = 0
    height: int = 0
    sha: Optional[str] = None
    phash: Optional[int] = None
    mean_color: Optional[Tuple[float, float, float]] = None
    low_information: bool = False
    exif_time: Optional[datetime] = None    # embedded DateTimeOriginal (local, naive)
    exif_subsec: bool = False               # exif_time carries sub-seconds
    sidecar_time: Optional[int] = None      # sidecar photoTakenTime (UTC epoch)
    note: str = ""                          # why perceptual comparison was skipped

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def comparable(self) -> bool:
        """Can take part in perceptual matching at all."""
        return self.phash is not None and not self.low_information

    def rank(self) -> tuple:
        # Higher is better: prefer a sidecar, then an embedded date, then
        # resolution, then file size, then the shorter path.
        return (1 if self.has_sidecar else 0, 1 if self.exif_time else 0,
                self.pixels, self.size, -len(self.path))


@dataclass
class DedupResult:
    keep: list = field(default_factory=list)          # MediaItem to keep
    drop: dict = field(default_factory=dict)          # dropped path -> kept path
    flagged: list = field(default_factory=list)       # (path_a, path_b, distance, reason)


def _aspect_matches(a: MediaItem, b: MediaItem) -> bool:
    if not (a.width and a.height and b.width and b.height):
        return False
    ra = a.width / a.height
    rb = b.width / b.height
    return abs(ra - rb) <= ASPECT_TOLERANCE * max(ra, rb)


def _colors_match(a: MediaItem, b: MediaItem) -> bool:
    if a.mean_color is None or b.mean_color is None:
        return False
    return all(abs(x - y) <= COLOR_TOLERANCE for x, y in zip(a.mean_color, b.mean_color))


def _naive_epoch(moment: datetime) -> float:
    """Seconds of a naive wall-clock time, as if it were UTC."""
    return (moment.replace(tzinfo=timezone.utc)
            - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()


def _times_agree(a: MediaItem, b: MediaItem) -> Optional[bool]:
    """True/False when both capture times are known, None when either is not."""
    if a.exif_time and b.exif_time:
        diff = abs(_naive_epoch(a.exif_time) - _naive_epoch(b.exif_time))
        limit = SUBSEC_TOLERANCE_SECONDS if a.exif_subsec and b.exif_subsec \
            else TIME_TOLERANCE_SECONDS
        return diff <= limit
    if a.sidecar_time is not None and b.sidecar_time is not None:
        return abs(a.sidecar_time - b.sidecar_time) <= TIME_TOLERANCE_SECONDS

    # One local EXIF time against one UTC sidecar time: equal up to a zone offset.
    local = a.exif_time or b.exif_time
    utc = a.sidecar_time if a.sidecar_time is not None else b.sidecar_time
    if local is None or utc is None:
        return None
    diff = abs(_naive_epoch(local) - utc)
    if diff > _MAX_ZONE_OFFSET_SECONDS + TIME_TOLERANCE_SECONDS:
        return False
    off_step = diff % _ZONE_STEP_SECONDS
    return min(off_step, _ZONE_STEP_SECONDS - off_step) <= TIME_TOLERANCE_SECONDS


def _same_photo(a: MediaItem, b: MediaItem, distance: int) -> Tuple[bool, str]:
    """Whether a close perceptual match is safe to merge, and if not, why."""
    if distance > CERTAIN_DISTANCE:
        return False, "visually similar"
    if a.has_sidecar and b.has_sidecar:
        # Both are separate Google Photos items: a burst, bracket or retake.
        return False, "separate Google Photos items"
    if not _aspect_matches(a, b):
        return False, "different shape"
    if not _colors_match(a, b):
        return False, "different colors"
    if _times_agree(a, b) is False:
        return False, "different capture times"
    return True, ""


def _masks(bits: int, max_set: int) -> List[int]:
    """Every `bits`-wide mask with at most `max_set` bits set."""
    return [m for m in range(1 << bits) if bin(m).count("1") <= max_set]


class _HashIndex:
    """Finds stored 64-bit hashes near a query without scanning all of them.

    Multi-index hashing: the hash is split into four 16-bit blocks. If two
    hashes differ in at most MAX_RADIUS bits, at least one block differs in at
    most 2 (otherwise the total would be 3 * 4 = 12 or more), so looking up each
    block's near variants finds every match. A BK-tree prunes almost nothing at
    these radii and stays quadratic.
    """

    BLOCKS = 4
    BLOCK_BITS = 16
    BLOCK_RADIUS = 2
    MAX_RADIUS = BLOCKS * (BLOCK_RADIUS + 1) - 1  # 11
    _MASKS = _masks(BLOCK_BITS, BLOCK_RADIUS)

    def __init__(self) -> None:
        self._tables: List[Dict[int, list]] = [{} for _ in range(self.BLOCKS)]

    def _blocks(self, phash: int) -> Iterator[Tuple[int, int]]:
        for i in range(self.BLOCKS):
            yield i, (phash >> (i * self.BLOCK_BITS)) & 0xFFFF

    def add(self, phash: int, item) -> None:
        for i, block in self._blocks(phash):
            self._tables[i].setdefault(block, []).append((phash, item))

    def search(self, phash: int, radius: int) -> Iterator[Tuple[int, object]]:
        """Yield (distance, item) for every stored hash within radius."""
        if radius > self.MAX_RADIUS:
            raise ValueError(f"radius above {self.MAX_RADIUS} is not guaranteed")
        seen = set()
        for i, block in self._blocks(phash):
            table = self._tables[i]
            for mask in self._MASKS:
                for stored, item in table.get(block ^ mask, ()):
                    if id(item) in seen:
                        continue
                    seen.add(id(item))
                    d = hamming(phash, stored)
                    if d <= radius:
                        yield d, item


def deduplicate(items: list) -> DedupResult:
    """Group duplicates and pick the best copy of each.

    Never drops on an uncertain match; those are kept and reported with the
    reason. Non-images, low-information images and images without a usable
    dHash fall back to exact-hash only.
    """
    result = DedupResult()

    # 1) Exact-byte duplicates collapse first (fast, certain).
    by_sha: Dict[str, MediaItem] = {}
    unique: List[MediaItem] = []
    for it in items:
        if it.sha and it.sha in by_sha:
            keep = by_sha[it.sha]
            if it.rank() > keep.rank():  # new item is better; swap representative
                by_sha[it.sha] = it
                _replace_representative(unique, keep, it)
                result.drop[keep.path] = it.path
            else:
                result.drop[it.path] = keep.path
        elif it.sha:
            by_sha[it.sha] = it
            unique.append(it)
        else:
            unique.append(it)

    # 2) Perceptual clustering over the byte-unique, comparable images. Each
    #    cluster is indexed by its first image (the anchor), but a new image
    #    joins only if it is the same photo as *every* member: otherwise two
    #    burst frames could both join a sidecar-less copy and one be dropped.
    index = _HashIndex()
    clusters: Dict[int, List[MediaItem]] = {}  # id(anchor) -> members
    for it in unique:
        if not it.comparable:
            result.keep.append(it)  # can't perceptually compare -> keep as-is
            continue

        merged_into = None
        nearest = None  # (distance, other path, reason) of the closest non-merge
        for d, anchor in sorted(index.search(it.phash, BORDERLINE_DISTANCE),
                                key=lambda pair: pair[0]):
            same, reason, other, dist = True, "", anchor, d
            for member in clusters[id(anchor)]:
                dist = hamming(it.phash, member.phash)
                same, reason = _same_photo(it, member, dist)
                if not same:
                    other = member
                    break
            if same:
                merged_into = anchor
                break
            if nearest is None:
                nearest = (dist, other.path, reason)

        if merged_into is not None:
            clusters[id(merged_into)].append(it)
            continue
        if nearest is not None:
            d, other_path, reason = nearest
            result.flagged.append((it.path, other_path, d, reason))
        index.add(it.phash, it)
        clusters[id(it)] = [it]

    # 3) One winner per confirmed cluster; losers are dropped.
    for members in clusters.values():
        winner = max(members, key=lambda m: m.rank())
        result.keep.append(winner)
        for m in members:
            if m is not winner:
                result.drop[m.path] = winner.path

    # A drop or flag may name a copy that was itself dropped later (an exact
    # duplicate's keeper that lost its cluster); point it at the final winner.
    def final(path: str) -> str:
        while path in result.drop:
            path = result.drop[path]
        return path

    result.drop = {dropped: final(kept) for dropped, kept in result.drop.items()}
    result.flagged = [(final(a), final(b), d, why) for a, b, d, why in result.flagged]

    return result


def _replace_representative(unique: list, old, new) -> None:
    for idx, it in enumerate(unique):
        if it is old:
            unique[idx] = new
            return


def is_image_path(path: str) -> bool:
    dot = path.rfind(".")
    return dot != -1 and path[dot:].lower() in _IMAGE_EXTS


def _exif_time(im) -> Tuple[Optional[datetime], bool]:
    """Embedded DateTimeOriginal (with sub-seconds when present)."""
    try:
        ifd = im.getexif().get_ifd(_EXIF_IFD)
        raw = str(ifd.get(_DATETIME_ORIGINAL, "")).strip()
        moment = datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None, False
    subsec = str(ifd.get(_SUBSEC_TIME_ORIGINAL, "")).strip()
    if subsec.isdigit():
        moment = moment.replace(microsecond=int(subsec[:6].ljust(6, "0")))
        return moment, True
    return moment, False


def _read_image_signals(item: MediaItem) -> None:
    """Open the image once: size, dHash, mean color, flatness, embedded date."""
    try:
        with Image.open(item.path) as im:
            item.exif_time, item.exif_subsec = _exif_time(im)
            # Full size as displayed: orientations 5 to 8 swap the axes.
            width, height = im.size
            if im.getexif().get(_ORIENTATION) in (5, 6, 7, 8):
                width, height = height, width
            item.width, item.height = width, height

            if im.format == "JPEG":
                im.draft("RGB", (64, 64))  # DCT scaling: decode at reduced size
            im = ImageOps.exif_transpose(im)

            rgb = im.convert("RGB").resize((32, 32), Image.BILINEAR)
            item.mean_color = tuple(ImageStat.Stat(rgb).mean)
            gray = _thumb(im)
            item.phash = _dhash_bits(gray)
            bits = bin(item.phash).count("1")
            if (bits < MIN_HASH_BITS or bits > MAX_HASH_BITS
                    or ImageStat.Stat(gray).stddev[0] < MIN_THUMB_STDDEV):
                item.low_information = True
                item.note = "too little detail for visual comparison"
    except Image.DecompressionBombError:
        item.phash = None
        item.note = "too large for visual comparison"
    except Exception:
        item.phash = None


def build_item(path: str, *, has_sidecar: bool = False, is_image: Optional[bool] = None,
               perceptual: bool = True, sidecar_time: Optional[int] = None) -> MediaItem:
    """Gather the comparison signals for one media file. When ``perceptual`` is
    off (or Pillow is unavailable), only the exact hash is used."""
    if is_image is None:
        is_image = is_image_path(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    item = MediaItem(path=path, is_image=is_image, has_sidecar=has_sidecar,
                     size=size, sha=sha256(path), sidecar_time=sidecar_time)
    if is_image and perceptual and Image is not None:
        _read_image_signals(item)
    return item
