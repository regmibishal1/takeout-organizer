"""Check a library later against what the runs recorded.

`takeout-organizer verify --dest DIR` reads the manifest and looks at every
file the runs put in the destination: is it still there, unchanged since it
was written (same size and modification time), and does a photo or video
that was given a date still hold one? It only reads; nothing is changed.
Use it after moving a library, on a NAS schedule, or before deleting the
Takeout.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .manifest import Manifest
from .preflight import counted
from .runguard import STATE_DIR

# Files read by one ExifTool command.
BATCH = 200

# Where a date can be once written: photos (any group, an .xmp sidecar too)
# and videos (QuickTime, or Keys with its zone).
_DATE_TAGS = ['-DateTimeOriginal', '-QuickTime:CreateDate', '-Keys:CreationDate']

# Dates a run writes into the file (a guess goes to _undated/ and is not).
_WRITTEN_SOURCES = {'sidecar', 'exif', 'filename'}

SHOWN = 50   # problems listed per kind


@dataclass
class VerifyResult:
    checked: int = 0
    missing: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    without_date: List[str] = field(default_factory=list)
    dates_checked: bool = False

    @property
    def fine(self) -> int:
        return self.checked - len(self.missing) - len(self.changed) - len(self.without_date)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.changed or self.without_date)


def _has_date(tags: Dict[str, object]) -> bool:
    for key, value in tags.items():
        name = key.split(':', 1)[-1]
        if name in ('DateTimeOriginal', 'CreateDate', 'CreationDate') \
                and str(value or '').strip()[:4] not in ('', '0000'):
            return True
    return False


def check(dest: str, exiftool_path: Optional[str] = None) -> Optional[VerifyResult]:
    """Check the library in dest; None when it has no manifest."""
    path = os.path.join(dest, STATE_DIR, 'manifest.db')
    if not os.path.exists(path):
        return None
    manifest = Manifest(path)
    try:
        rows = sorted(manifest.library().values(), key=lambda row: row['target_path'])
        # JPG copies of HEIC photos, checked like the files the runs wrote.
        rows += [{'target_path': row['derived_path'], 'output_size': row['derived_size'],
                  'output_mtime_ns': row['derived_mtime_ns'], 'outcome': 'copy',
                  'date_source': None}
                 for row in sorted(manifest.derived_files().values(),
                                   key=lambda row: row['derived_path'])]
    finally:
        manifest.close()

    result = VerifyResult(checked=len(rows))
    dated = []   # (relative path, file whose tags hold the date)
    for row in rows:
        relative = row['target_path']
        target = os.path.join(dest, *relative.split('/'))
        try:
            stat = os.stat(target)
        except OSError:
            result.missing.append(relative)
            continue
        if row['output_size'] is not None and (stat.st_size != row['output_size'] or
                                               stat.st_mtime_ns != row['output_mtime_ns']):
            result.changed.append(relative)
            continue
        # A file copied without new metadata (its error says why) was never
        # given a date to keep.
        if row['outcome'] == 'written' and row['date_source'] in _WRITTEN_SOURCES \
                and not row.get('error'):
            xmp = target + '.xmp'
            dated.append((relative, xmp if os.path.exists(xmp) else target))

    from .metadata_handler import MetadataHandler
    handler = MetadataHandler(exiftool_path, require=False, keep_open=True)
    if handler.exiftool_path and dated:
        result.dates_checked = True
        try:
            for start in range(0, len(dated), BATCH):
                batch = dated[start:start + BATCH]
                found = _read_dates(handler, [file for _, file in batch])
                for relative, file in batch:
                    if not _has_date(found.get(os.path.normcase(os.path.abspath(file)), {})):
                        result.without_date.append(relative)
        finally:
            handler.close()
    return result


def _read_dates(handler, files: List[str]) -> Dict[str, Dict[str, object]]:
    """Date tags of many files in one ExifTool command, by normalized path."""
    run = handler._run_exiftool(['-json', '-G1', '-api', 'LargeFileSupport=1', *_DATE_TAGS,
                                 *[os.path.abspath(f) for f in files]])
    try:
        data = json.loads(run.stdout or '[]')
    except ValueError:
        return {}
    return {os.path.normcase(os.path.abspath(item.get('SourceFile', ''))): item for item in data}


def main(dest: str, as_json: bool = False, exiftool_path: Optional[str] = None) -> int:
    """Print what verify found. Exit 0 when all is well, 1 when a file is
    missing, changed or without its date, 2 when dest has no manifest."""
    result = check(dest, exiftool_path)
    if result is None:
        sys.stderr.write(f"No library made by this tool in {dest} (no "
                         f"{STATE_DIR}/manifest.db).\n")
        return 2
    if as_json:
        sys.stdout.write(json.dumps({
            'checked': result.checked, 'fine': result.fine, 'missing': result.missing,
            'changed': result.changed, 'without_date': result.without_date,
            'dates_checked': result.dates_checked}, indent=2) + '\n')
        return 0 if result.ok else 1

    out = sys.stdout
    out.write(f"Checked {counted(result.checked, 'file')} the runs put in {dest}\n")
    for title, paths, what in (
            ("Missing", result.missing, "no longer in the library"),
            ("Changed since written", result.changed, "edited, or damaged, since the run"),
            ("Without their date", result.without_date, "the date written is no longer inside")):
        if paths:
            out.write(f"\n{title}: {len(paths):,} ({what})\n")
            out.writelines(f"  {path}\n" for path in paths[:SHOWN])
            if len(paths) > SHOWN:
                out.write(f"  ... and {len(paths) - SHOWN:,} more (use --json for all)\n")
    if not result.dates_checked and result.checked:
        out.write("\nExifTool was not found, so the dates inside the files were not checked.\n")
    out.write(f"\n{'All files are as the runs left them.' if result.ok else ''}"
              f"{'' if result.ok else counted(result.fine, 'file') + ' are fine.'}\n")
    return 0 if result.ok else 1
