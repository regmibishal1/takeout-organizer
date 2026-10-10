"""Undo a run: delete exactly the files it wrote into the destination.

The Takeout files are never touched by a run, so removing what a run wrote
puts the destination back as it was. A file is only removed when it is still
the one the run wrote (same size and modification time as recorded), so a
photo edited since is left in place and listed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .manifest import Manifest
from .runguard import STATE_DIR

# Files the tool puts next to a photo it writes.
COMPANIONS = ('.xmp', '.json')


@dataclass
class UndoPlan:
    run_id: str
    started: str
    files: List[str] = field(default_factory=list)      # absolute paths to delete
    total_bytes: int = 0
    changed: List[str] = field(default_factory=list)    # edited since: kept
    missing: int = 0                                     # already gone


def manifest_path(dest: str) -> str:
    return os.path.join(dest, STATE_DIR, 'manifest.db')


def find_run(manifest: Manifest, run_id: Optional[str]) -> Optional[dict]:
    """The run to undo: the one named, or the latest real run that wrote files."""
    runs = [r for r in manifest.runs() if not r['dry_run'] and r['status'] != 'undone']
    if run_id:
        return next((r for r in runs if r['run_id'] == run_id), None)
    for run in reversed(runs):
        if any(f['status'] == 'done' and (f['outcome'] in ('written', 'other')
                                          or f.get('derived_path'))
               for f in manifest.files(run['run_id'])):
            return run
    return None


def plan(dest: str, run_id: Optional[str] = None) -> Tuple[Optional[UndoPlan], str]:
    """What undoing a run would delete. Returns (plan, '') or (None, reason)."""
    path = manifest_path(dest)
    if not os.path.exists(path):
        return None, f"No runs are recorded in {dest}."
    manifest = Manifest(path)
    try:
        run = find_run(manifest, run_id)
        if run is None:
            return None, (f"Run {run_id} was not found, or was already undone." if run_id
                          else "No run with files to undo was found.")
        result = UndoPlan(run_id=run['run_id'], started=run['started'])
        for row in manifest.files(run['run_id']):
            if row['status'] != 'done':
                continue
            if row['outcome'] in ('written', 'other') and row['target_path']:
                _add(result, dest, row['target_path'], row['output_size'],
                     row['output_mtime_ns'], COMPANIONS)
            if row.get('derived_path') and row.get('derived_size') is not None:
                # A file the run made from this one (a JPG copy of a HEIC); a
                # run can make one for a photo that was already there.
                _add(result, dest, row['derived_path'], row['derived_size'],
                     row['derived_mtime_ns'], ())
        return result, ''
    finally:
        manifest.close()


def _add(result: UndoPlan, dest: str, relative: str, size, mtime_ns, companions) -> None:
    """Plan to delete one file a run wrote, unless it is gone or was edited."""
    target = os.path.join(dest, *relative.split('/'))
    try:
        stat = os.stat(target)
    except OSError:
        result.missing += 1
        return
    if size is not None and (stat.st_size != size or stat.st_mtime_ns != mtime_ns):
        result.changed.append(target)
        return
    result.files.append(target)
    result.total_bytes += stat.st_size
    for suffix in companions:
        if os.path.isfile(target + suffix):
            result.files.append(target + suffix)
            result.total_bytes += os.path.getsize(target + suffix)


def apply(dest: str, undo: UndoPlan) -> int:
    """Delete the planned files, tidy what they leave behind, record it."""
    deleted = 0
    folders = set()
    for path in undo.files:
        try:
            os.remove(path)
            deleted += 1
            folders.add(os.path.dirname(path))
        except OSError:
            pass
    _prune_playlists(dest)
    _remove_empty_folders(dest, folders)

    manifest = Manifest(manifest_path(dest))
    try:
        manifest.db.execute("UPDATE files SET status = 'undone' WHERE run_id = ? AND "
                            "status = 'done' AND outcome IN ('written', 'other')",
                            (undo.run_id,))
        manifest.finish_run(undo.run_id, 'undone')
    finally:
        manifest.close()
    return deleted


def _prune_playlists(dest: str) -> None:
    """Drop playlist lines for files that are gone; remove empty playlists."""
    albums = os.path.join(dest, 'Albums')
    if not os.path.isdir(albums):
        return
    index_path = os.path.join(albums, 'albums.json')
    if os.path.isfile(index_path):
        with open(index_path, encoding='utf-8') as f:
            index = json.load(f)
        index = {album: [p for p in paths if os.path.exists(os.path.join(dest, *p.split('/')))]
                 for album, paths in index.items()}
        index = {album: paths for album, paths in index.items() if paths}
        if index:
            with open(index_path, 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=2, ensure_ascii=False)
        else:
            os.remove(index_path)
    for name in os.listdir(albums):
        if not name.endswith('.m3u8'):
            continue
        path = os.path.join(albums, name)
        with open(path, encoding='utf-8') as f:
            lines = [line.rstrip('\n') for line in f]
        kept = [line for line in lines[1:]
                if os.path.exists(os.path.normpath(os.path.join(albums, line)))]
        if kept:
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write('\n'.join(['#EXTM3U'] + kept) + '\n')
        else:
            os.remove(path)
    try:
        os.rmdir(albums)  # only when nothing is left in it
    except OSError:
        pass


def _remove_empty_folders(dest: str, folders) -> None:
    """Remove folders emptied by the undo, up to (not including) dest."""
    root = os.path.normcase(os.path.abspath(dest))
    for folder in sorted(folders, key=len, reverse=True):
        current = os.path.abspath(folder)
        while os.path.normcase(current) != root and os.path.normcase(current).startswith(root):
            try:
                os.rmdir(current)  # only succeeds when empty
            except OSError:
                break
            current = os.path.dirname(current)
