"""What a bug report needs, and nothing personal.

`takeout-organizer diagnose` (and the window's Copy diagnostics button) print
the versions of the tool, Python, Tk, the libraries and ExifTool, the
operating system, and the last run's summary and warnings. Every folder path
is replaced: the destination, the Takeout folder, the home and temp folders
by a placeholder, any other path by its file name, since paths hold folder
and people's names.

Each run also writes summary.json next to its log: the counts and a few rates
(match rate, where dates came from, failures by reason, time per 1,000
files), so a change can be judged by its numbers. Nothing is sent anywhere.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import sys
import tempfile
from importlib import metadata
from typing import Any, Dict, List, Optional

from . import __version__
from .runguard import STATE_DIR

WARNING_LINES = 50

# An absolute path not covered by a known folder: a drive or UNC path on
# Windows, or a path of two parts or more elsewhere. A space ends it.
_OTHER_PATH = re.compile(r"(?:[A-Za-z]:[/\\]|[/\\]{2}|(?<![\w.~>])/)[^\s\"'<>|]*[/\\][^\s\"'<>|]*")


def shorten_paths(text: str, folders: Optional[Dict[str, str]] = None) -> str:
    """Replace the known folders by their placeholder (longest first, in both
    slash styles), then any other absolute path by its file name."""
    known = dict(folders or {})
    known.setdefault('<home>', os.path.expanduser('~'))
    known.setdefault('<temp>', tempfile.gettempdir())
    pairs = []
    for name, path in known.items():
        if not path or len(path) < 4:
            continue
        for form in {path, path.replace('\\', '/'), path.replace('/', '\\'),
                     os.path.realpath(path)}:
            pairs.append((form.rstrip('/\\'), name))
    for path, name in sorted(pairs, key=lambda pair: -len(pair[0])):
        text = re.sub(re.escape(path), name.replace('\\', '\\\\'), text, flags=re.IGNORECASE)
    return _OTHER_PATH.sub(lambda m: re.split(r'[/\\]', m.group(0).rstrip('/\\'))[-1] or
                           m.group(0), text)


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return 'not installed'


def versions() -> List[tuple]:
    """(name, version) for everything a bug may depend on."""
    rows = [('Takeout Organizer', __version__),
            ('Python', f"{platform.python_version()} ({platform.python_implementation()})"),
            ('Operating system', f"{platform.system()} {platform.release()} "
                                 f"({platform.machine()})")]
    try:
        import tkinter
        rows.append(('Tk', tkinter.Tcl().eval('info patchlevel')))
    except Exception:  # no tkinter, or Tcl cannot start
        rows.append(('Tk', 'not available'))
    for package in ('pillow', 'sv-ttk', 'pi-heif', 'pillow-heif', 'tzfpy'):
        rows.append((package, _version(package)))
    from .metadata_handler import MetadataHandler
    handler = MetadataHandler(require=False)
    if handler.exiftool_path:
        from .metadata_handler import bundled_exiftool
        if handler.exiftool_path == bundled_exiftool():
            where = 'bundled with this build'
        else:
            where = os.path.basename(handler.exiftool_path)
        rows.append(('ExifTool', f"{handler.get_exiftool_version()} ({where})"))
    else:
        rows.append(('ExifTool', 'not found'))
    return rows


def write_summary(path: str, data: Dict[str, Any]) -> None:
    """summary.json for one run (see the module docstring)."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)


def last_run(dest: str) -> Optional[Dict[str, Any]]:
    """The newest summary.json under dest/.takeout-organizer/logs, with the
    path of the log it belongs to, or None."""
    found = sorted(glob.glob(os.path.join(dest, STATE_DIR, 'logs', 'run-*.json')),
                   key=os.path.getmtime)
    if not found:
        return None
    try:
        with open(found[-1], encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    data['_log'] = os.path.splitext(found[-1])[0] + '.log'
    return data


def warnings_of(log_file: str, limit: int = WARNING_LINES) -> List[str]:
    """The last warnings and errors of a run's log, without their time."""
    try:
        with open(log_file, encoding='utf-8', errors='replace') as f:
            lines = [line.rstrip() for line in f
                     if ' - WARNING - ' in line or ' - ERROR - ' in line]
    except OSError:
        return []
    return [line.split(' - ', 1)[1] for line in lines[-limit:]]


def report(dest: Optional[str] = None) -> str:
    """The text to paste into a bug report."""
    out = ['Takeout Organizer diagnostics', '']
    out += [f"{name:<18}{value}" for name, value in versions()]
    run = last_run(dest) if dest else None
    if dest and run is None:
        out += ['', 'No run found in the destination folder.']
    if run:
        folders = {'<dest>': run.get('dest') or dest, '<source>': run.get('source', '')}
        summary = run.get('summary', {})
        out += ['', f"Last run: {run.get('run_id', '?')} "
                    f"({'preview' if run.get('dry_run') else 'run'}, {run.get('status', '?')})"]
        out += [f"  {key}: {value}" for key, value in sorted(summary.items())]
        for key in ('match_rate', 'seconds_per_1000_files', 'photos_missing'):
            if key in run:
                out.append(f"  {key}: {run[key]}")
        if run.get('failures_by_reason'):
            out.append('  failures by reason:')
            out += [f"    {count} x {reason}" for reason, count
                    in run['failures_by_reason'].items()]
        lines = warnings_of(run['_log'])
        out += ['', f"Last {len(lines)} warnings and errors:" if lines
                else 'No warnings or errors.']
        out += [f"  {line}" for line in lines]
        return shorten_paths('\n'.join(out), folders)
    return shorten_paths('\n'.join(out), {'<dest>': dest} if dest else None)


def main(dest: Optional[str]) -> int:
    sys.stdout.write(report(dest) + '\n')
    return 0
