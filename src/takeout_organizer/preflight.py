"""Checks before a run starts: space, export completeness, damaged ZIPs.

Most failed runs fail on setup. Finding out in seconds beats finding out after
hours of extracting and copying.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .i18n import Text
from .runguard import STATE_DIR

# Required free space on top of the estimate.
SPACE_MARGIN = 1.05

# FAT32 cannot hold a file of 4 GB or more.
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1

# Limits for a ZIP that is damaged, or built to fill the disk (a ZIP bomb).
# Takeout parts hold photos and videos, which barely compress, so they expand
# to about their own size. Small ZIPs of text may compress more, so the ratio
# only counts above EXPANSION_FLOOR.
MAX_EXPANSION = 20
EXPANSION_FLOOR = 1024 ** 3
MAX_ENTRIES = 1_000_000

# takeout-20261001T120000Z-001.zip, -002.zip, ...
_PART_RE = re.compile(r'^(?P<series>.+)-(?P<part>\d{3})\.zip$', re.IGNORECASE)


@dataclass
class PreflightResult:
    problems: List[str] = field(default_factory=list)   # the run cannot start
    warnings: List[str] = field(default_factory=list)   # worth knowing, not blocking
    work_needed: int = 0
    dest_needed: int = 0
    work_free: Optional[int] = None
    dest_free: Optional[int] = None
    work_drive: str = ''
    dest_drive: str = ''
    same_drive: bool = False
    zip_count: int = 0
    source_bytes: int = 0      # what the export holds once extracted


def human(size: int) -> str:
    """'52 GB' style size, as the space messages show it."""
    value = float(size)
    unit = 'bytes'
    for next_unit in ('KB', 'MB', 'GB', 'TB'):
        if value < 1024:
            break
        value /= 1024
        unit = next_unit
    if unit == 'bytes' or value >= 10:
        return f"{value:.0f} {unit}"
    return f"{value:.1f} {unit}"


def counted(number: int, noun: str) -> str:
    """'1 file', '8,135 files': a count with its noun in the right number."""
    return f"{number:,} {noun}" + ("" if number == 1 else "s")


def default_work_root(dest: str, dry_run: bool) -> str:
    """Where the working copy goes without --work-dir. For a run, next to the
    organized photos: finished files are then moved into place instead of
    copied, and the system drive needs no room. A preview writes nothing into
    the destination, so it works in the system temp folder."""
    return tempfile.gettempdir() if dry_run else os.path.join(dest, STATE_DIR, 'work')


def _existing_ancestor(path: str) -> str:
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _same_drive(a: str, b: str) -> bool:
    try:
        return os.stat(_existing_ancestor(a)).st_dev == os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return False


def _free(path: str) -> Optional[int]:
    try:
        return shutil.disk_usage(_existing_ancestor(path)).free
    except OSError:
        return None


def filesystem_name(path: str) -> Optional[str]:
    """File system of the drive holding path ('NTFS', 'FAT32', ...), Windows only."""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        root = os.path.splitdrive(_existing_ancestor(path))[0] + '\\'
        name = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, None, None, None, name, len(name))
        return name.value if ok else None
    except Exception:
        return None


def _zip_sizes(zip_paths: List[str], result: PreflightResult) -> Dict[str, List[int]]:
    """Uncompressed member sizes per readable ZIP; damaged ones become warnings,
    and one past the limits above stops the run, named."""
    sizes = {}
    for path in zip_paths:
        name = os.path.basename(path)
        try:
            with zipfile.ZipFile(path) as zf:
                members = [info.file_size for info in zf.infolist() if not info.is_dir()]
            packed = os.path.getsize(path)
        except (zipfile.BadZipFile, OSError):
            result.warnings.append(Text(
                "{zip} is damaged (it may not have finished downloading). Download it again; "
                "your other files are fine.", zip=name))
            continue
        expanded = sum(members)
        if len(members) > MAX_ENTRIES:
            result.problems.append(Text(
                "{zip} holds {count} files, more than a Takeout part ever does. It is probably "
                "damaged or not from Google Takeout. Move it out of the folder and run again.",
                zip=name, count=len(members)))
        elif expanded > EXPANSION_FLOOR and expanded > MAX_EXPANSION * packed:
            result.problems.append(Text(
                "{zip} would expand from {packed} to {expanded}, more than {times} times its "
                "size; Takeout parts barely compress. It is probably damaged or not from "
                "Google Takeout. Move it out of the folder and run again.",
                zip=name, packed=human(packed), expanded=human(expanded), times=MAX_EXPANSION))
        else:
            sizes[path] = members
    return sizes


def _missing_parts(zip_paths: List[str]) -> List[str]:
    series: Dict[str, set] = {}
    for path in zip_paths:
        match = _PART_RE.match(os.path.basename(path))
        if match:
            series.setdefault(match['series'], set()).add(int(match['part']))
    messages = []
    for parts in series.values():
        highest = max(parts)
        for part in range(1, highest + 1):
            if part not in parts:
                messages.append(Text(
                    "Part {part} of {last} is missing. Download it into the same folder, or "
                    "the photos in it will not be organized.",
                    part=f"{part:03d}", last=f"{highest:03d}"))
    return messages


# A JPG copy of a HEIC photo takes about this share of the HEIC's size.
HEIC_COPY_SHARE = 0.4
HEIC_EXTENSIONS = ('.heic', '.heif')


def _heic_bytes(source: str, zip_paths: List[str]) -> int:
    """Total size of the HEIC photos in the export."""
    total = 0
    for path in zip_paths:
        try:
            with zipfile.ZipFile(path) as zf:
                total += sum(info.file_size for info in zf.infolist()
                             if info.filename.lower().endswith(HEIC_EXTENSIONS))
        except (zipfile.BadZipFile, OSError):
            pass
    if not zip_paths:
        for root, _, files in os.walk(source):
            for name in files:
                if name.lower().endswith(HEIC_EXTENSIONS):
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
    return total


def check(source: str, dest: str, work_dir: str, dry_run: bool,
          heic_copies: bool = False) -> PreflightResult:
    """Estimate the space a run needs and look for problems in the export.
    With heic_copies, the JPG copies of HEIC photos are counted (an estimate)."""
    result = PreflightResult()

    zip_paths = sorted(
        os.path.join(root, name)
        for root, _, files in os.walk(source)
        for name in files if name.lower().endswith('.zip')
    )
    result.zip_count = len(zip_paths)
    if zip_paths:
        sizes = _zip_sizes(zip_paths, result)
        result.warnings += _missing_parts(zip_paths)
        file_sizes = [size for member_sizes in sizes.values() for size in member_sizes]
        # ZIPs are extracted into the working folder even for a dry run.
        result.work_needed = sum(file_sizes)
    else:
        file_sizes = []
        for root, _, files in os.walk(source):
            for name in files:
                try:
                    file_sizes.append(os.path.getsize(os.path.join(root, name)))
                except OSError:
                    pass
        # An extracted export is copied to the working folder for a real run.
        result.work_needed = 0 if dry_run else sum(file_sizes)

    result.source_bytes = sum(file_sizes)
    result.dest_needed = 0 if dry_run else sum(file_sizes)
    if heic_copies and not dry_run:
        result.dest_needed += int(_heic_bytes(source, zip_paths) * HEIC_COPY_SHARE)

    work_free, dest_free = _free(work_dir), _free(dest)
    shared = _same_drive(work_dir, dest)
    result.work_free, result.dest_free, result.same_drive = work_free, dest_free, shared
    result.work_drive, result.dest_drive = _existing_ancestor(work_dir), _existing_ancestor(dest)
    work_need = result.work_needed * SPACE_MARGIN
    dest_need = result.dest_needed * SPACE_MARGIN
    dest_has_files = os.path.isdir(dest) and any(os.scandir(dest))

    if shared and work_free is not None:
        # On one drive, finished files are moved out of the working copy into
        # place, so the run needs room for the larger of the two, not both.
        if max(work_need, dest_need) > work_free:
            values = dict(drive=_existing_ancestor(dest), free=human(work_free),
                          need=human(int(max(work_need, dest_need))))
            if dest_has_files and work_need <= work_free:
                # A rerun skips files already there, so it may need far less.
                result.warnings.append(Text(
                    "Not enough space on {drive}: the run needs about {need}, {free} is free. "
                    "Free some space, or use --work-dir on another drive. (It may fit if most "
                    "files are already there.)", **values))
            else:
                result.problems.append(Text(
                    "Not enough space on {drive}: the run needs about {need}, {free} is free. "
                    "Free some space, or use --work-dir on another drive.", **values))
    else:
        if work_free is not None and work_need > work_free:
            result.problems.append(Text(
                "Not enough space for the working copy on {drive}: needs about {need}, {free} "
                "is free. Use --work-dir on a larger drive.", drive=_existing_ancestor(work_dir),
                need=human(int(work_need)), free=human(work_free)))
        if dest_free is not None and dest_need > dest_free:
            values = dict(drive=_existing_ancestor(dest), need=human(int(dest_need)),
                          free=human(dest_free))
            if dest_has_files:
                result.warnings.append(Text(
                    "Not enough space on {drive} for the organized photos: needs about {need}, "
                    "{free} is free. (It may fit if most files are already there.)", **values))
            else:
                result.problems.append(Text(
                    "Not enough space on {drive} for the organized photos: needs about {need}, "
                    "{free} is free.", **values))

    if not dry_run and (filesystem_name(dest) or '').upper() in ('FAT32', 'FAT'):
        too_big = sum(1 for size in file_sizes if size > FAT32_MAX_FILE)
        if too_big:
            result.warnings.append(Text(
                "This drive is FAT32 and cannot hold files over 4 GB. {count} of the files is "
                "larger and will fail to copy. Use a drive formatted as exFAT or NTFS.",
                "This drive is FAT32 and cannot hold files over 4 GB. {count} of the files are "
                "larger and will fail to copy. Use a drive formatted as exFAT or NTFS.",
                count=too_big))
    return result
