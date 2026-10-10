"""The manifest: one SQLite database holding what every run decided per file.

Each input file gets a stable ID that survives reruns: for a ZIP source the
hash of the ZIP name and the path inside it, for an extracted export the path
relative to the source folder. A row records where the file came from, what
it is, how it was matched and dated, the duplicate decision, where it went,
and what happened to it. Reports, resuming and undoing a run are read from
here.

It lives at dest/.takeout-organizer/manifest.db; a preview keeps its own in
the preview folder. Only the standard library is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started     TEXT NOT NULL,
    finished    TEXT,
    source      TEXT NOT NULL,
    dest        TEXT NOT NULL,
    dry_run     INTEGER NOT NULL,
    settings    TEXT NOT NULL,          -- JSON
    status      TEXT NOT NULL           -- running, completed, stopped, failed
);
CREATE TABLE IF NOT EXISTS files (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    input_id        TEXT NOT NULL,      -- stable across runs of the same export
    zip             TEXT,               -- ZIP file name, or NULL for a folder source
    rel_path        TEXT NOT NULL,      -- path inside the ZIP or the source folder
    size            INTEGER,
    sha256          TEXT,
    kind            TEXT,               -- photo, video, other
    sidecar         TEXT,               -- sidecar path inside the export
    match_rule      TEXT,
    date_chosen     TEXT,               -- ISO 8601 local time used for the folder
    date_source     TEXT,               -- sidecar, exif, filename, upload, file time
    date_rejected   TEXT,               -- why the sidecar date was not used
    dedup_role      TEXT,               -- keep, drop, flag
    kept_input_id   TEXT,               -- for a drop: the copy that was kept
    target_path     TEXT,               -- relative to dest, with / separators
    outcome         TEXT NOT NULL,      -- written, already, duplicate, other, failed
    error           TEXT,
    status          TEXT,               -- planned, done, failed, undone
    output_size     INTEGER,            -- the written file, to tell later edits apart
    output_mtime_ns INTEGER,
    derived_path    TEXT,               -- a file made from this one (a JPG copy of a HEIC)
    derived_size    INTEGER,
    derived_mtime_ns INTEGER,
    PRIMARY KEY (run_id, input_id)
);
CREATE INDEX IF NOT EXISTS files_input ON files (input_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

FILE_COLUMNS = ('input_id', 'zip', 'rel_path', 'size', 'sha256', 'kind', 'sidecar',
                'match_rule', 'date_chosen', 'date_source', 'date_rejected', 'dedup_role',
                'kept_input_id', 'target_path', 'outcome', 'error', 'status',
                'output_size', 'output_mtime_ns', 'derived_path', 'derived_size',
                'derived_mtime_ns')

# Columns added after version 1, with their types, for migrating older files.
_ADDED_COLUMNS = {2: [('files', 'status', 'TEXT')],
                  3: [('files', 'output_size', 'INTEGER'),
                      ('files', 'output_mtime_ns', 'INTEGER')],
                  4: [('files', 'derived_path', 'TEXT'), ('files', 'derived_size', 'INTEGER'),
                      ('files', 'derived_mtime_ns', 'INTEGER')]}

# Row updates are committed in batches of this size (and at the end).
UPDATE_BATCH = 200


def input_id(zip_name: Optional[str], rel_path: str) -> str:
    """Stable ID of an input file: the same file in the same export always
    gets the same ID, whatever temporary folder it was extracted to."""
    if zip_name:
        return hashlib.sha1(f"{zip_name}/{rel_path}".encode('utf-8')).hexdigest()
    return rel_path


class Manifest:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(_SCHEMA)
        self._migrate()
        self._pending = 0

    def _migrate(self) -> None:
        """Bring a manifest written by an older version up to this schema."""
        row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row[0]) if row else SCHEMA_VERSION
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for table, column, kind in _ADDED_COLUMNS.get(target, []):
                existing = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
                if column not in existing:
                    self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
        if row is None or version != SCHEMA_VERSION:
            # Only when it changes, so opening a manifest to read it (verify)
            # does not write to it.
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                            (str(SCHEMA_VERSION),))
        self.db.commit()

    def update_file(self, run_id: str, file_id: str, **fields: Any) -> None:
        """Change some columns of one row; committed in batches (see flush)."""
        unknown = set(fields) - set(FILE_COLUMNS)
        if unknown:
            raise ValueError(f"Unknown manifest columns: {sorted(unknown)}")
        assignments = ', '.join(f'{name} = ?' for name in fields)
        self.db.execute(f'UPDATE files SET {assignments} WHERE run_id = ? AND input_id = ?',
                        (*fields.values(), run_id, file_id))
        self._pending += 1
        if self._pending >= UPDATE_BATCH:
            self.flush()

    def flush(self) -> None:
        self.db.commit()
        self._pending = 0

    def start_run(self, run_id: str, source: str, dest: str, dry_run: bool,
                  settings: Dict[str, Any]) -> None:
        self.db.execute(
            'INSERT INTO runs (run_id, started, source, dest, dry_run, settings, status) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (run_id, datetime.now().isoformat(timespec='seconds'), source, dest,
             int(dry_run), json.dumps(settings, sort_keys=True), 'running'))
        self.db.commit()

    def finish_run(self, run_id: str, status: str) -> None:
        self.flush()
        self.db.execute('UPDATE runs SET finished = ?, status = ? WHERE run_id = ?',
                        (datetime.now().isoformat(timespec='seconds'), status, run_id))
        self.db.commit()

    def record_files(self, run_id: str, rows: Iterable[Dict[str, Any]],
                     batch: int = 200) -> int:
        """Write one row per input file, committing every `batch` rows."""
        sql = (f"INSERT OR REPLACE INTO files (run_id, {', '.join(FILE_COLUMNS)}) "
               f"VALUES (?, {', '.join('?' * len(FILE_COLUMNS))})")
        count = 0
        for row in rows:
            self.db.execute(sql, (run_id, *(row.get(c) for c in FILE_COLUMNS)))
            count += 1
            if count % batch == 0:
                self.db.commit()
        self.db.commit()
        return count

    def runs(self) -> List[Dict[str, Any]]:
        cursor = self.db.execute('SELECT * FROM runs ORDER BY started')
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def files(self, run_id: str) -> List[Dict[str, Any]]:
        cursor = self.db.execute('SELECT * FROM files WHERE run_id = ? ORDER BY rel_path',
                                 (run_id,))
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def library(self, exclude_run: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Files earlier real runs put in the destination and that are still
        recorded as there (not undone), by input ID; the latest run wins. Each
        row has the input's size and SHA-256, to find it again by content."""
        cursor = self.db.execute(
            "SELECT f.input_id, f.target_path, f.output_size, f.output_mtime_ns, f.outcome, "
            "f.date_chosen, f.date_source, f.size, f.sha256, f.kind, f.error "
            "FROM files f JOIN runs r ON r.run_id = f.run_id "
            "WHERE r.dry_run = 0 AND f.status = 'done' AND f.outcome IN ('written', 'other') "
            "AND f.target_path IS NOT NULL AND f.run_id IS NOT ? ORDER BY r.started",
            (exclude_run,))
        names = [d[0] for d in cursor.description]
        return {row[0]: dict(zip(names, row)) for row in cursor.fetchall()}

    def derived_files(self, exclude_run: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Files real runs made from library files (JPG copies of HEICs) and
        still recorded as there, by the path of the file they were made from;
        the latest run wins."""
        cursor = self.db.execute(
            "SELECT f.target_path, f.derived_path, f.derived_size, f.derived_mtime_ns "
            "FROM files f JOIN runs r ON r.run_id = f.run_id "
            "WHERE r.dry_run = 0 AND f.status = 'done' AND f.derived_path IS NOT NULL "
            "AND f.derived_size IS NOT NULL AND f.target_path IS NOT NULL "
            "AND f.run_id IS NOT ? ORDER BY r.started", (exclude_run,))
        names = [d[0] for d in cursor.description]
        return {row[0]: dict(zip(names, row)) for row in cursor.fetchall()}

    def close(self) -> None:
        self.flush()
        self.db.close()
