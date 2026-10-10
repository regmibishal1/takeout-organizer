"""
Main Processor for Google Photos Takeout Organizer
Orchestrates the entire processing pipeline with duplicate detection and recovery
"""

import json
import os
import re
import logging
import shutil
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Callable, Tuple
from datetime import datetime, timezone
import queue
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .session_manager import SessionManager
from .file_matcher import FileMatcher
from .dates import (EPOCH, date_from_filename, has_location_lookup, parse_exif_date,
                    parse_timezone, to_local)
from .metadata_handler import GOOGLE_FIRST, MetadataHandler, check_timestamp
from .file_processor import LAYOUTS, FileProcessor
from .filetypes import extension_mismatch, kind as content_kind
from .preflight import HEIC_EXTENSIONS, check as preflight_check, counted, default_work_root
from .runguard import STATE_DIR, KeepAwake, RunLock
from . import report as html_report
from .manifest import Manifest, input_id as make_input_id
from .deduplicator import DedupResult, build_item, deduplicate, is_image_path, sha256
from . import naming, places
from .i18n import N_


# ExifTool processes writing metadata at once (phase 4), each into its own
# files: most of a run is spent writing metadata. Four keep a spinning disk
# from seeking back and forth. Runs with fewer files than PARALLEL_FROM use
# one, as starting more would cost more than it saves.
EXIFTOOL_PROCESSES = 4
PARALLEL_FROM = 50


@dataclass
class _Tags:
    """What writing one file's metadata found, for the main thread to record:
    problems as (log level, log line, reason), an .xmp sidecar written (or ''
    when a preview counts one), and whether the file's own tags were written."""
    problems: List[Tuple[str, str, str]] = field(default_factory=list)
    xmp: Optional[str] = None
    written: bool = False


# Files read and hashed at once when looking for duplicates: about twice as fast
# as one at a time, without making a spinning disk seek back and forth.
HASH_THREADS = 4

# Prefix of the per-run working folder in the OS temp directory.
WORK_DIR_PREFIX = 'google_photos_'

# Working folders older than this are leftovers from a crashed run.
STALE_WORK_DIR_SECONDS = 24 * 60 * 60

# Files from the export that are not photos or videos are copied here.
OTHER_DIR = '_other'

# Files whose date is only a guess (the upload date or the file time) go here
# instead of a date folder, so a wrong guess never hides a photo in a wrong year.
UNDATED_DIR = '_undated'

# Album playlists and their index go here.
ALBUMS_DIR = 'Albums'

# Takeout's own year folders, which are not albums ("Photos from 2023").
_YEAR_FOLDER = re.compile(
    r"^(photos from|fotos von|photos de|fotos de|foto del|foto's uit|fotos fra|"
    r"foton fran|zdjecia z) \d{4}$", re.IGNORECASE)

# Characters a file name may not contain, for album playlist names.
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|]')

# Google's own files in an export, which are not the user's content.
TAKEOUT_BOILERPLATE = {'archive_browser.html'}

# Where each kept file's folder date came from (see _predict_date).
DATE_SOURCES = (
    ('sidecar', N_('date from Google sidecar')),
    ('exif', N_('date from photo EXIF')),
    ('filename', N_('date from file name')),
    ('upload', N_('date from upload date')),
    ('file time', N_('date from file time')),
)


def summary_text(table: Dict[str, Any], translate: Callable[[str], str] = lambda s: s,
                 number: Callable[[int], str] = lambda n: f"{n:,}") -> List[str]:
    """The accounting summary as aligned lines: in English for the log and
    the report, or with the window's translate and number functions."""
    rows = [('  ' * depth + translate(label).format(**params), number(value))
            for label, params, value, depth in table['rows']]
    total = translate(N_("Total accounted"))
    width = max([34] + [len(label) + 1 for label, _value in rows] + [len(total) + 1])
    lines = [f"{label:<{width}}{value:>9}" for label, value in rows]
    lines.append(f"{total:<{width}}{number(table['accounted']):>9}   " + (
        translate(N_("all files accounted for")) if table['balanced'] else "MISMATCH"))
    lines += [translate(note).format(**params) for note, params in table['notes']]
    return lines


class SetupError(Exception):
    """A problem found before any work starts (bad folders, ExifTool missing).
    The message is written for the user and says what to do."""


def _is_inside(child: str, parent: str) -> bool:
    """True if child is parent or lies anywhere below it."""
    child = os.path.normcase(os.path.realpath(child))
    parent = os.path.normcase(os.path.realpath(parent))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def check_folders(source_folder: str, dest_folder: str) -> None:
    """Raise SetupError if the folders cannot be used together.

    An output folder inside the Takeout folder would be read back in as input on
    the next run, and the reverse would write into the export being read.
    """
    if not os.path.isdir(source_folder):
        raise SetupError(f"The Takeout folder does not exist: {source_folder}")
    if _is_inside(dest_folder, source_folder):
        raise SetupError(
            "The destination is inside the Takeout folder. Choose a different folder."
        )
    if _is_inside(source_folder, dest_folder):
        raise SetupError(
            "The Takeout folder is inside the destination. Choose a different folder."
        )


class MainProcessor:
    """Main processing orchestrator"""

    def __init__(
        self,
        source_folder: str,
        dest_folder: str,
        dry_run: bool = False,
        preserve_originals: bool = False,
        keep_json: bool = False,
        exiftool_path: Optional[str] = None,
        session_file: Optional[str] = None,
        dedup_mode: str = "perceptual",
        progress_callback: Optional[Callable[[int, int, str, str], None]] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        confirm: Optional[Callable[[List[str]], bool]] = None,
        resume: bool = False,
        plan_file: Optional[str] = None,
        time_zone: Optional[str] = None,
        date_policy: str = GOOGLE_FIRST,
        overwrite_gps: bool = False,
        people_as_keywords: bool = False,
        fix_extensions: bool = False,
        work_dir: Optional[str] = None,
        console: bool = True,
        layout: str = 'year-month',
        albums: str = 'playlists',
        rename: Optional[str] = None,
        heic_copies: Optional[str] = None,
        place_names: bool = False,
    ):
        """
        Initialize processor

        Args:
            source_folder: Folder containing ZIP files
            dest_folder: Destination folder for organized photos
            dry_run: If True, preview without making changes. Nothing is
                written to dest_folder; the log and reports go to a preview
                folder in the OS temp directory.
            preserve_originals: Ignored. Kept so existing callers still work;
                the backups it made lived in the temp copy and were deleted.
            keep_json: If True, keep JSON files in output
            exiftool_path: Path to ExifTool executable
            session_file: Ignored; kept so existing callers still work. Use
                resume instead.
            progress_callback: Callback for progress updates(current, total, phase, details)
            log_callback: Callback for log messages(message, level)
            cancel_event: When set, the run stops after the current file
            confirm: Called with the plan summary before anything is written;
                returning False stops the run with nothing written.
            resume: Continue the last unfinished run into this destination:
                its decisions are reused and files it finished are skipped.
            plan_file: A preview's manifest.db. Its plan is applied as it is,
                provided the export and the settings have not changed.
            time_zone: Zone for capture times when neither GPS nor the camera
                gives one ('+09:00' or a name such as 'Asia/Tokyo'). Default:
                this computer's zone.
            date_policy: 'google-first' or 'camera-first', for when Google's
                date and the camera's date are different moments.
            overwrite_gps: Replace GPS the camera recorded with Google's.
            people_as_keywords: Also add people's names to the keywords.
            fix_extensions: Rename files whose extension does not match their
                content (in the working copy, so the output gets the right
                name) instead of copying them without new metadata.
            layout: Output folders by date: 'year-month' (2023/06, the default),
                'year-month-day' (2023/06/15), 'year' or 'flat'.
            albums: 'playlists' writes one playlist per album to Albums/;
                'off' writes none.
            place_names: Add the town or city a photo was taken in to its
                date folder (2023/06 Tokyo), from its location (see places).
            console: Also print log lines to the console. The CLI turns this
                off and shows progress and warnings itself.
            work_dir: Folder for the temporary working copy. Default:
                dest/.takeout-organizer/work for a run (finished files are
                moved into place), the system temp folder for a preview.

        Raises:
            SetupError: The folders cannot be used, ExifTool is missing, the
                time zone is unknown, there is not enough disk space, or
                another run is writing to the same destination. Raised before
                any file is written.
        """
        self.source_folder = source_folder
        self.dest_folder = dest_folder
        self.dry_run = dry_run
        self.keep_json = keep_json
        self.dedup_mode = dedup_mode  # "off" | "exact" | "perceptual"
        self.keep_orphans = []        # kept media with no JSON sidecar (e.g. album downloads)
        self.photos_missing: List[str] = []  # sidecar titles whose photo is not in the export
        self.match_counts: Dict[str, int] = {}
        self._started = time.time()
        self.dedup_result = None      # DedupResult, for the written duplicates report
        self.planned_dates: Dict[str, datetime] = {}  # local capture dates from sidecars
        self.upload_dates: Dict[str, datetime] = {}   # upload dates, when nothing better
        self.fix_extensions = fix_extensions
        self.other_files: List[str] = []       # not photos or videos: copied to _other/
        self.wrong_extension: Dict[str, str] = {}  # path -> real format, when not fixed
        self.sidecar_of: Dict[str, str] = {}   # media path -> its JSON sidecar
        self.inputs: List[str] = []            # every file the run is responsible for
        self.outcomes: Dict[str, str] = {}     # input path -> what happened to it
        self.date_sources: Counter = Counter()
        self.summary: Optional[Dict[str, Any]] = None
        self.summary_lines: List[str] = []
        self.summary_table: Optional[Dict[str, Any]] = None   # see summary_text
        self.exiftool_processes = 1   # writing metadata in phase 4
        self.album_of: Dict[str, str] = {}      # input media path -> album name
        self.output_paths: Dict[str, str] = {}  # input path -> where it is in dest
        self.xmp_sidecars: Dict[str, str] = {}  # media ExifTool cannot write -> its .xmp
        self.undated = 0
        self.undated_names: List[str] = []
        self.by_year: Counter = Counter()        # written files per capture year
        self.album_counts: Dict[str, int] = {}
        self.dedup_items: Dict[str, Any] = {}    # path -> MediaItem, for the report
        self.report_path: Optional[str] = None
        self.failure_reasons: Dict[str, str] = {}  # input path -> why it was not copied
        self.date_info: Dict[str, Tuple[Optional[datetime], str]] = {}  # date, its source
        self.date_rejected: Dict[str, str] = {}     # media -> why its sidecar date was not used
        self._rule_of: Dict[str, Optional[str]] = {}  # media -> sidecar match rule
        self._ids: Dict[str, str] = {}               # path -> manifest input ID
        self.metadata_errors: Dict[str, str] = {}    # media -> why its metadata failed
        self.plan: List[Dict[str, Any]] = []         # what applying will do, in order
        self.confirm = confirm
        self.declined = False
        self.problem: Optional[str] = None             # why a run stopped, for the user
        self.previous_run: Optional[str] = None        # run whose decisions are reused
        self.previous_rows: Dict[str, Dict[str, Any]] = {}  # its rows by input ID
        self.library: Dict[str, Dict[str, Any]] = {}        # files earlier runs put in dest
        # The same files by content (input size and SHA-256), so a newer export,
        # whose ZIPs have other names, finds what is already there.
        self.library_by_content: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
        self._shas: Dict[str, Optional[str]] = {}    # path -> SHA-256 of the input
        self._library_sizes: set = set()
        self.original_path: Dict[str, str] = {}     # renamed working copy -> name before
        self.run_id = datetime.now().strftime('%Y%m%d-%H%M%S-') + os.urandom(2).hex()
        self.manifest: Optional[Manifest] = None
        self.manifest_path: Optional[str] = None
        self._run_status = 'failed'
        self.time_zone_text = time_zone or "this computer's"
        self.console = console
        self.log_file: Optional[str] = None
        self.cancel_event = cancel_event
        self.cancelled = False

        # Check everything that can fail before touching the disk.
        check_folders(source_folder, dest_folder)
        try:
            zone = parse_timezone(time_zone) if time_zone else None
        except ValueError as exc:
            raise SetupError(str(exc)) from exc
        try:
            # A dry run previews the plan and never writes metadata, so
            # exiftool isn't required for it.
            self.metadata_handler = MetadataHandler(
                exiftool_path, require=not dry_run, timezone=zone, date_policy=date_policy,
                overwrite_gps=overwrite_gps, people_as_keywords=people_as_keywords,
                # One ExifTool process for the whole run; a preview uses it too,
                # when ExifTool is there, to read each file's own dates.
                keep_open=True,
            )
        except (RuntimeError, ValueError) as exc:
            raise SetupError(str(exc)) from exc

        if resume and plan_file:
            raise SetupError("Use either --resume or --plan, not both.")
        if plan_file is not None and not os.path.isfile(plan_file):
            raise SetupError(f"The plan file does not exist: {plan_file}")
        if (resume or plan_file) and dry_run:
            raise SetupError("A preview cannot resume a run or apply a plan.")
        self.resume = resume
        self.plan_file = plan_file

        if layout not in LAYOUTS:
            raise SetupError(f"Unknown layout: {layout}. Use one of: {', '.join(LAYOUTS)}")
        if albums not in ('playlists', 'off'):
            raise SetupError(f"Unknown albums setting: {albums}. Use playlists or off")
        self.layout = layout
        self.albums = albums
        # --rename: a name template (see naming); checked before anything starts.
        try:
            self.rename = naming.check_template(rename) if rename else None
        except ValueError as exc:
            raise SetupError(str(exc)) from None
        # --place-names: the town or city goes on the date folder (2023/06 Tokyo).
        if place_names and layout == 'flat':
            raise SetupError("Place names go on the date folders, and the flat layout has "
                             "none. Choose another layout, or run without --place-names.")
        self.place_names = place_names
        self.places_found = 0
        self.places_missing = 0
        # --heic-copies jpg: a JPG next to each HEIC photo; the HEIC is kept.
        self.heic_copies = heic_copies
        self.heic_copies_made = 0
        self.derived_by_target: Dict[str, Dict[str, Any]] = {}
        if heic_copies:
            from .deduplicator import HEIC_SUPPORT
            if HEIC_SUPPORT is None:
                raise SetupError("JPG copies of HEIC photos need a HEIC decoder. Install "
                                 "takeout-organizer[heic], or run without --heic-copies.")

        if work_dir is not None:
            if not os.path.isdir(work_dir):
                raise SetupError(f"The work folder does not exist: {work_dir}")
            if _is_inside(work_dir, source_folder):
                raise SetupError(
                    "The work folder is inside the Takeout folder. Choose a different folder."
                )
        self.work_root = work_dir or default_work_root(dest_folder, dry_run)
        # The default work folder of a run lies in dest/.takeout-organizer and
        # belongs to this tool alone; one given with --work-dir is the user's.
        self.own_work_root = work_dir is None and not dry_run

        # Space and export checks, before hours of extracting and copying.
        preflight = preflight_check(source_folder, dest_folder, self.work_root, dry_run,
                                    heic_copies=bool(heic_copies))
        if preflight.problems:
            raise SetupError("\n".join(preflight.problems))
        self.preflight_warnings = preflight.warnings

        # One run at a time per destination: two would copy over each other.
        self.lock = RunLock(dest_folder)
        if not dry_run:
            busy = self.lock.acquire()
            if busy:
                raise SetupError(f"Another run is using this folder ({busy}).")
        self.keep_awake = KeepAwake()

        # Where the log and reports go. A dry run must leave dest untouched.
        if dry_run:
            self.output_dir = tempfile.mkdtemp(
                prefix=f'takeout_preview_{datetime.now().strftime("%Y%m%d_%H%M%S")}_',
                dir=self.work_root
            )
        else:
            self.output_dir = dest_folder

        # Setup logging
        self._setup_logging(log_callback)

        # Run statistics for the Markdown report, kept in memory: the manifest
        # is the record a run can be resumed from.
        self.session = SessionManager(None)
        if session_file is not None:
            self.logger.warning("--session is replaced by --resume and has no effect")
        self.matcher = FileMatcher(case_sensitive=False)
        self.file_processor = FileProcessor(self.logger, work_dir=self.work_root,
                                            layout=layout)

        # Set callbacks
        self.progress_callback = progress_callback
        self.log_callback = log_callback

        # Temporary working directory
        self.temp_dir = None

    def _setup_logging(self, log_callback: Optional[Callable] = None):
        """Setup logging configuration"""
        self.logger = logging.getLogger('GooglePhotosOrganizer')
        self.logger.setLevel(logging.DEBUG)

        # Each run owns the logger's handlers. Without this, a second run in
        # the same process (the GUI) logged every line twice.
        self._close_logging()

        # File handler
        # Per-run files go in .takeout-organizer/logs, so they do not pile up
        # next to the photos; only the HTML report sits there.
        self.logs_dir = os.path.join(self.output_dir, STATE_DIR, 'logs')
        os.makedirs(self.logs_dir, exist_ok=True)
        stem = os.path.join(self.logs_dir, f'run-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
        log_file, number = stem + '.log', 1
        while os.path.exists(log_file):  # two runs in the same second get their own logs
            number += 1
            log_file = f"{stem}-{number}.log"

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        self.log_file = log_file

        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

        # Console handler
        if self.console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

        # Add callback handler if provided
        if log_callback:
            callback_handler = CallbackHandler(log_callback)
            callback_handler.setLevel(logging.INFO)
            self.logger.addHandler(callback_handler)

    def _close_logging(self):
        """Detach and close every handler on the shared logger."""
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()

    def _update_progress(self, current: int, total: int, phase: str, details: str = ""):
        """Update progress"""
        if self.progress_callback:
            self.progress_callback(current, total, phase, details)

    def _stopped(self) -> bool:
        """True once the cancel event is set; logs the stop once."""
        if self.cancel_event is None or not self.cancel_event.is_set():
            return False
        if not self.cancelled:
            self.cancelled = True
            self.logger.warning(
                "Stopped by user. Files already copied stay in the destination."
            )
        return True

    def run(self):
        """Run the complete processing pipeline"""
        self._started = time.time()
        try:
            self.logger.info("="*60)
            self.logger.info("Google Photos Takeout Organizer - Starting")
            self.logger.info("="*60)
            self.logger.info(f"Source: {self.source_folder}")
            self.logger.info(f"Destination: {self.dest_folder}")
            self.logger.info(f"Dry run: {self.dry_run}")
            if self.metadata_handler.exiftool_path:
                self.logger.info(f"ExifTool version: {self.metadata_handler.get_exiftool_version()}")
            if not has_location_lookup():
                self.logger.info(
                    "Time zones from GPS are off (install takeout-organizer[timezones]); "
                    "photos without a camera date use "
                    + ("the --timezone setting" if self.metadata_handler.timezone
                       else "this computer's time zone")
                )
            if self.own_work_root:
                self._clear_stale_work()
            else:
                self._warn_stale_work_dirs()
            for warning in self.preflight_warnings:
                self.logger.warning(warning)
            if not self.dry_run:
                removed = self.file_processor.remove_partial_copies(self.dest_folder)
                if removed:
                    self.logger.info(f"Removed {removed} half-written copies left by a stopped run")
                if self.keep_awake.start():
                    self.logger.info("This computer will not go to sleep while the run is going")

            # Save session info
            self.session.set_folders(self.source_folder, self.dest_folder)
            self._open_manifest()

            phases = (
                self._phase_extract,   # Phase 1: Extract ZIP files
                self._phase_match,     # Phase 2: Match files
                self._phase_reuse,     # Check a resumed run or saved plan still fits
                self._phase_dedup,     # Phase 2.5: Deduplicate across the merged import
                self._phase_plan,      # Phase 3: Decide date, folder and name per file
                self._phase_confirm,   # Ask before writing, when asked to
                self._phase_apply,     # Phase 4: Write metadata and copy, per the plan
            )
            for phase in phases:
                if self._stopped() or not phase():
                    if self.cancelled:
                        self.session.set_phase('stopped')
                        self._run_status = 'stopped'
                    return False

            # Write the duplicates report before temp files are cleaned up.
            self._write_dedup_report()
            summary_lines = self._account()
            try:
                self.report_path = self._write_html_report()
                self.logger.info(f"Report: {self.report_path}")
            except Exception as exc:  # the report must never fail a finished run
                self.logger.warning(f"Could not write the HTML report: {exc}")

            # Complete
            self.session.set_phase('completed')
            self.logger.info("="*60)
            self.logger.info("Processing completed successfully!")
            self.logger.info("="*60)

            # Generate report
            report_path = self.session.save_report(
                os.path.splitext(self.log_file)[0] + '.md'  # named like the run's log
            )
            with open(report_path, 'a', encoding='utf-8') as f:
                f.write("\n## Every file accounted for\n\n```\n")
                f.write("\n".join(summary_lines) + "\n```\n")
            self.logger.info(f"Report saved to: {report_path}")
            if self.dry_run:
                self.logger.info(
                    f"Dry run: nothing was written to {self.dest_folder}. "
                    f"Preview log and reports: {self.output_dir}"
                )

            self._run_status = 'completed'
            return True

        except Exception as e:
            self.logger.error(f"Fatal error during processing: {e}", exc_info=True)
            self.session.add_error(f"Fatal error: {str(e)}")
            self.session.save()
            return False

        finally:
            self._write_run_summary()
            # Phase 5: Cleanup, on every run (dry runs and failures included):
            # the working folder can be as large as the whole library.
            self.metadata_handler.close()
            if self.manifest is not None:
                self.manifest.finish_run(self.run_id, self._run_status)
                self.manifest.close()
            self._phase_cleanup()
            self.keep_awake.stop()
            self.lock.release()
            self._close_logging()

    def _write_run_summary(self) -> None:
        """summary.json next to the run's log: what the run did, in numbers
        (see diagnostics). Written for every run, finished or not; it never
        fails a run."""
        from . import __version__
        from .diagnostics import write_summary
        try:
            seconds = time.time() - self._started
            files = len(getattr(self, 'inputs', []) or [])
            media = sum(self.match_counts.values())
            written = self.summary.get('written', 0) if self.summary else 0
            data = {
                'tool_version': __version__, 'run_id': self.run_id, 'status': self._run_status,
                'dry_run': self.dry_run, 'source': self.source_folder, 'dest': self.dest_folder,
                'settings': {'layout': self.layout, 'dedup': self.dedup_mode,
                             'date_policy': self.metadata_handler.date_policy,
                             'time_zone': self.time_zone_text,
                             'rename': bool(self.rename), 'heic_copies': self.heic_copies,
                             'place_names': self.place_names,
                             'exiftool_processes': self.exiftool_processes},
                'summary': self.summary or {},
                'places_found': self.places_found if self.place_names else None,
                'places_missing': self.places_missing if self.place_names else None,
                'match_rate': round(self.match_counts.get('with_sidecar', 0) / media, 4)
                if media else None,
                'date_shares': {source: round(count / written, 4)
                                for source, count in self.date_sources.items()} if written else {},
                'failures_by_reason': dict(Counter(self.failure_reasons.values()).most_common(10)),
                'metadata_failures_by_reason': dict(
                    Counter(self.metadata_errors.values()).most_common(10)),
                'photos_missing': len(self.photos_missing),
                'seconds': round(seconds, 1),
                'seconds_per_1000_files': round(seconds * 1000 / files, 1) if files else None,
            }
            write_summary(os.path.splitext(self.log_file)[0] + '.json', data)
        except Exception as exc:
            self.logger.warning(f"Could not write the run summary: {exc}")

    def _clear_stale_work(self) -> None:
        """Delete a working copy a killed run left in the destination's work
        folder: it can be as large as the library. The run lock is held, so
        no other run is using it."""
        try:
            names = os.listdir(self.work_root)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.work_root, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if names:
            self.logger.info(f"Removed the working copy a stopped run left in {self.work_root}")

    def _warn_stale_work_dirs(self):
        """Point out working folders left in the temp dir by crashed runs."""
        temp_root = self.work_root
        cutoff = time.time() - STALE_WORK_DIR_SECONDS
        stale = []
        try:
            for name in os.listdir(temp_root):
                path = os.path.join(temp_root, name)
                if (name.startswith(WORK_DIR_PREFIX) and os.path.isdir(path)
                        and os.path.getmtime(path) < cutoff):
                    stale.append(path)
        except OSError:
            return
        if stale:
            self.logger.warning(
                f"Found {counted(len(stale), 'leftover work folder')} from earlier runs in "
                f"{temp_root} (named {WORK_DIR_PREFIX}...). They are safe to delete."
            )

    def _phase_extract(self) -> bool:
        """Phase 1: Extract ZIP files"""
        self.logger.info("\n--- Phase 1: Extracting ZIP files ---")
        self.session.set_phase('extracting')

        # Create temporary directory
        os.makedirs(self.work_root, exist_ok=True)
        self.temp_dir = tempfile.mkdtemp(prefix=WORK_DIR_PREFIX, dir=self.work_root)
        self.logger.info(f"Temporary directory: {self.temp_dir}")

        # Count ZIP files
        zip_files = []
        for root, dirs, files in os.walk(self.source_folder):
            for file in files:
                if file.lower().endswith('.zip'):
                    zip_files.append(os.path.join(root, file))

        self.session.update_statistics(total_zips=len(zip_files))
        self.logger.info(f"Found {len(zip_files)} ZIP files")

        if len(zip_files) == 0:
            # No ZIPs - treat the source as an already-extracted Takeout export.
            self.logger.info(
                "No ZIP files found; treating source as an already-extracted export"
            )
            if self.dry_run:
                # Dry run never writes, so it's safe to read the source in place.
                self.merged_dir = self.source_folder
            else:
                # Copy into the temp dir first so ExifTool's in-place metadata
                # writes touch only the copies - the user's originals are never
                # modified (matches the ZIP path and the SAFETY guarantees).
                self.logger.info("Copying source into a working directory to keep originals untouched")
                work = os.path.join(self.temp_dir, 'source')
                shutil.copytree(self.source_folder, work)
                self.merged_dir = work
            return True

        # Extract
        def progress_cb(current, total, filename):
            self._update_progress(current, total, "Reading Takeout", filename)
            self.session.increment_statistic('extracted_zips')
            self.session.save()

        extracted_dirs = self.file_processor.extract_zip_files(
            self.source_folder,
            self.temp_dir,
            progress_cb
        )

        if not extracted_dirs:
            self.logger.error("Failed to extract any ZIP files")
            return False

        # Merge Google Photos folders
        self.logger.info("Merging Google Photos folders...")
        self.merged_dir = self.file_processor.merge_takeout_folders(
            extracted_dirs,
            os.path.join(self.temp_dir, 'merged')
        )

        self.logger.info(f"Merged directory: {self.merged_dir}")
        return True

    def _phase_match(self) -> bool:
        """Phase 2: Match media files with JSON"""
        self.logger.info("\n--- Phase 2: Matching media files with JSON ---")
        self.session.set_phase('matching')

        # Find all files
        self.logger.info("Scanning for media and JSON files...")
        media_files = self.matcher.find_all_media_files(self.merged_dir)
        json_files = self.matcher.find_all_json_files(self.merged_dir)

        self.session.update_statistics(
            total_media_files=len(media_files),
            total_json_files=len(json_files)
        )

        self.logger.info(f"Found {len(media_files)} media files")
        self.logger.info(f"Found {len(json_files)} JSON files")

        # Match files
        self.logger.info("Matching files...")
        matched_pairs, unmatched_media, unmatched_json = self.matcher.match_files(self.merged_dir)

        self.session.update_statistics(
            matched_files=len(matched_pairs),
            unmatched_media=len(unmatched_media),
            unmatched_json=len(unmatched_json)
        )

        self.logger.info(f"Matched: {len(matched_pairs)}")
        by_rule = Counter(pair.get('rule', 'exact') for pair in matched_pairs)
        if by_rule:
            self.logger.info("Matched by rule: " + ", ".join(
                f"{rule} {count}" for rule, count in by_rule.most_common()))
        self.logger.info(f"Unmatched media: {len(unmatched_media)}")
        self.logger.info(f"Unmatched JSON: {len(unmatched_json)}")

        # Store matched pairs
        self.matched_pairs = matched_pairs
        self.unmatched_media = unmatched_media
        self.unmatched_json = unmatched_json

        # Everything else: media with extensions the lists do not know, and
        # files that are not photos or videos. None of it may be dropped.
        self._scan_remaining(set(media_files))
        self._check_extensions()
        self.photos_missing = self._photos_missing(self.matched_pairs, self.unmatched_media)
        self.match_counts = {'with_sidecar': len(self.matched_pairs),
                             'without_sidecar': len(self.unmatched_media)}
        if self.photos_missing:
            count = len(self.photos_missing)
            self.logger.warning(
                f"{counted(count, 'sidecar')} {'names' if count == 1 else 'name'} a photo that "
                f"is not in this export (for example {self.photos_missing[0]}). If the export "
                "came in parts, check that every part is in the folder."
            )
        self.sidecar_of = {pair['media']: pair['json'] for pair in self.matched_pairs}
        self._rule_of = {pair['media']: pair.get('rule') for pair in self.matched_pairs}
        for media in [pair['media'] for pair in self.matched_pairs] + list(self.unmatched_media):
            album = self._album_of(media)
            if album:
                self.album_of[media] = album
        self.inputs = ([pair['media'] for pair in self.matched_pairs]
                       + list(self.unmatched_media) + list(self.other_files))
        for zip_name, member in self.file_processor.unreadable_members:
            entry = f"{zip_name}: {member}"
            self.inputs.append(entry)
            self.outcomes[entry] = 'failed'
            self.failure_reasons[entry] = f"could not be read from {zip_name}"
            self.logger.error(f"{member} could not be read from {zip_name}")

        # Files without a sidecar are normal (album downloads), so this is not a
        # warning; the summary shows how each of them was dated.
        if unmatched_media:
            names = ", ".join(os.path.basename(f) for f in unmatched_media[:3])
            self.logger.info(
                f"{len(unmatched_media)} photos or videos have no Google sidecar (for example "
                f"{names}); they are dated from their own EXIF, their name, or the file time"
            )
            for file in unmatched_media:
                self.logger.debug(f"No sidecar: {file}")
                self.session.add_warning(f"Unmatched media: {file}")

        return True

    def _photos_missing(self, matched_pairs, unmatched_media) -> List[str]:
        """Titles of sidecars whose photo is nowhere in the export. Album
        folders repeat sidecars of photos kept elsewhere, and long names are
        cut short in file names, so a sidecar counts only when no file has its
        title, nor the first 20 characters of its name."""
        names, starts = set(), set()
        for path in [pair['media'] for pair in matched_pairs] + list(unmatched_media):
            name = os.path.basename(path).lower()
            names.add(name)
            starts.add(os.path.splitext(name)[0][:20])
        missing = []
        for json_file in self.unmatched_json:
            try:
                with open(json_file, encoding='utf-8') as f:
                    title = json.load(f).get('title')
            except (OSError, ValueError, AttributeError):
                continue
            if not isinstance(title, str) or not title:
                continue
            lower = title.lower()
            if lower not in names and os.path.splitext(lower)[0][:20] not in starts:
                missing.append(title)
        return missing

    def _scan_remaining(self, known_media: set) -> None:
        """Classify files the name-based scan skipped, by their content."""
        found_media = 0
        for root, dirs, files in os.walk(self.merged_dir):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                path = os.path.join(root, name)
                lower = name.lower()
                if (path in known_media or lower.endswith('.json')
                        or lower in TAKEOUT_BOILERPLATE):
                    continue
                media_kind = content_kind(path)
                if media_kind is None:
                    self.other_files.append(path)
                    continue
                found_media += 1
                json_file = self.matcher.find_json_for_media(path)
                if json_file:
                    self.matched_pairs.append({
                        'media': path, 'json': json_file,
                        'is_image': media_kind == 'image', 'is_video': media_kind == 'video',
                    })
                    if json_file in self.unmatched_json:
                        self.unmatched_json.remove(json_file)
                else:
                    self.unmatched_media.append(path)
        if found_media:
            self.logger.info(f"Found {found_media} more photos or videos by their content")
        if self.other_files:
            self.logger.info(
                f"{len(self.other_files)} files are not photos or videos; "
                f"they will be copied to {OTHER_DIR}/"
            )

    def _album_of(self, media_file: str) -> Optional[str]:
        """The album a file sits in: its folder, unless that is a Takeout year
        folder or the top. The name comes from the album's metadata.json."""
        folder = os.path.dirname(media_file)
        name = os.path.basename(folder)
        if (os.path.normcase(folder) == os.path.normcase(self.merged_dir)
                or name in ('Google Photos', 'Takeout') or _YEAR_FOLDER.match(name)):
            return None
        meta = os.path.join(folder, 'metadata.json')
        try:
            with open(meta, encoding='utf-8') as f:
                title = json.load(f).get('title')
            if isinstance(title, str) and title.strip():
                return title.strip()
        except (OSError, ValueError, AttributeError):
            pass
        return name

    def _check_extensions(self) -> None:
        """Find media whose extension does not match the content. ExifTool
        refuses to write those ("Not a valid HEIC"). With fix_extensions the
        working copy is renamed; the source is never touched."""
        renamed: Dict[str, str] = {}
        media = [pair['media'] for pair in self.matched_pairs] + list(self.unmatched_media)
        for path in media:
            mismatch = extension_mismatch(path)
            if mismatch is None:
                continue
            fmt, ext = mismatch
            name = os.path.basename(path)
            if not self.fix_extensions:
                self.wrong_extension[path] = fmt
                continue
            new_path = os.path.splitext(path)[0] + ext
            counter = 1
            while os.path.exists(new_path):
                new_path = f"{os.path.splitext(path)[0]}_{counter}{ext}"
                counter += 1
            if self.dry_run:
                self.logger.info(f"Would rename {name} (a {fmt.upper()} file) to "
                                 f"{os.path.basename(new_path)}")
                continue
            os.rename(path, new_path)
            self.original_path[new_path] = self.original_path.pop(path, path)
            if path in self.file_processor.origins:
                self.file_processor.origins[new_path] = self.file_processor.origins.pop(path)
            renamed[path] = new_path
            self.logger.info(f"Renamed {name} (a {fmt.upper()} file) to "
                             f"{os.path.basename(new_path)}")

        if renamed:
            for pair in self.matched_pairs:
                if pair['media'] in renamed:
                    pair['media'] = renamed[pair['media']]
                    media_kind = content_kind(pair['media'])
                    pair['is_image'] = media_kind == 'image'
                    pair['is_video'] = media_kind == 'video'
            self.unmatched_media = [renamed.get(p, p) for p in self.unmatched_media]
        if self.wrong_extension:
            self.logger.warning(
                f"{len(self.wrong_extension)} files have an extension that does not match "
                "their content; they will be copied without new metadata. Run again with "
                "--fix-extensions to rename and fix them."
            )

    # Settings that change what a plan decides; a reused plan must have the same.
    _PLAN_SETTINGS = ('layout', 'dedup', 'date_policy', 'time_zone', 'fix_extensions',
                      'rename', 'heic_copies', 'place_names')

    def _phase_reuse(self) -> bool:
        """With --resume or --plan, load the earlier run's decisions and check
        they still fit: the same files (by ID and size) and the same settings."""
        if not (self.resume or self.plan_file):
            return True
        if self.plan_file:
            source = Manifest(self.plan_file)
            candidates = [r for r in source.runs() if r['dry_run']]
            label = "The plan"
        else:
            source = self.manifest
            candidates = [r for r in source.runs() if r['run_id'] != self.run_id
                          and not r['dry_run']
                          and r['status'] in ('stopped', 'failed', 'running', 'declined')]
            label = "The stopped run"
        try:
            if not candidates:
                if self.plan_file:
                    self.problem = f"{self.plan_file} holds no plan. Make one with the plan command."
                    self.logger.error(self.problem)
                    return False
                self.logger.info("No unfinished run to resume; doing a full run")
                return True
            previous = candidates[-1]
            rows = {row['input_id']: row for row in source.files(previous['run_id'])}
        finally:
            if source is not self.manifest:
                source.close()
        if not rows and self.resume:
            self.logger.info("The stopped run had not planned any files yet; doing a full run")
            return True

        before = json.loads(previous['settings'])
        now = {
            'layout': self.layout, 'dedup': self.dedup_mode,
            'date_policy': self.metadata_handler.date_policy,
            'time_zone': self.time_zone_text, 'fix_extensions': self.fix_extensions,
            'rename': self.rename, 'heic_copies': self.heic_copies,
            'place_names': self.place_names,
        }
        # A setting an older version did not record was off then (None == False).
        changed = [k for k in self._PLAN_SETTINGS if (before.get(k) or None) != (now[k] or None)]
        if changed:
            self.problem = (f"{label} was made with other settings ({', '.join(changed)}). "
                            "Use the same options, or start again without --resume or --plan.")
            self.logger.error(self.problem)
            return False

        current = {}
        for path in self.inputs:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            current[self._id_of(path)] = size
        previous_sizes = {i: r['size'] for i, r in rows.items()}
        if current != previous_sizes:
            self.problem = (f"The export changed since {label.lower()} was made "
                            f"({len(set(current) ^ set(previous_sizes))} files differ). "
                            + ("Make a new plan." if self.plan_file
                               else "Run again without --resume."))
            self.logger.error(self.problem)
            return False

        if before.get('exiftool') is False and self.metadata_handler.exiftool_path:
            self.logger.warning(
                f"{label} was made without ExifTool, so its folders follow the sidecar dates "
                "only. Make a new plan with ExifTool installed for camera dates to count."
            )
        self.previous_run = previous['run_id']
        self.previous_rows = rows
        done = sum(1 for r in rows.values() if r.get('status') == 'done')
        self.logger.info(f"Reusing the decisions of run {self.previous_run} "
                         f"({done} of {len(rows)} files already done)")
        return True

    def _reuse_dedup(self) -> None:
        """The earlier run's duplicate decisions, mapped to this run's paths."""
        by_id = {self._id_of(p): p for p in self.inputs}
        drop = {}
        for row in self.previous_rows.values():
            if row.get('dedup_role') == 'drop' and row.get('kept_input_id') in by_id:
                drop[by_id[row['input_id']]] = by_id[row['kept_input_id']]
        self.dedup_result = DedupResult(drop=drop)
        for dropped in drop:
            self.outcomes[dropped] = 'duplicate'
        self.matched_pairs = [p for p in self.matched_pairs if p['media'] not in drop]
        self.keep_orphans = [m for m in self.unmatched_media if m not in drop]
        self.logger.info(f"Duplicates: {len(drop)} decided by run {self.previous_run}")

    def _phase_dedup(self) -> bool:
        """Phase 2.5: Deduplicate across the merged import.

        The same photo can arrive twice (Takeout + a downloaded shared album).
        Group duplicates, keep the best copy, and carry sidecar-less survivors
        (album photos with no JSON) forward so they still land in the output.
        This phase is the only place duplicates are decided.
        """
        if self.previous_rows:
            self._reuse_dedup()
            return True

        # Media with no JSON sidecar (typical of downloaded albums) that we keep.
        if self.dedup_mode == "off":
            self.keep_orphans = list(self.unmatched_media)
            self.logger.info(
                "\n--- Phase 2.5: Deduplication off (keeping %d sidecar-less files) ---",
                len(self.keep_orphans),
            )
            return True

        self.logger.info("\n--- Phase 2.5: Deduplicating (mode: %s) ---", self.dedup_mode)
        perceptual = self.dedup_mode == "perceptual"

        # Reading and hashing every file is most of this phase. Hashing and
        # image decoding release the GIL, so a few threads read files at once;
        # the items keep the input order, so decisions do not depend on timing.
        jobs = [(pair["media"], dict(has_sidecar=True, is_image=pair["is_image"],
                                     sidecar_time=self._sidecar_time(pair["json"])))
                for pair in self.matched_pairs]
        jobs += [(media, dict(has_sidecar=False, is_image=is_image_path(media)))
                 for media in self.unmatched_media]
        items = []
        with ThreadPoolExecutor(max_workers=HASH_THREADS) as pool:
            futures = [pool.submit(build_item, path, perceptual=perceptual, **kwargs)
                       for path, kwargs in jobs]
            for future, (path, _kwargs) in zip(futures, jobs):
                if self._stopped():
                    pool.shutdown(cancel_futures=True)
                    return False
                items.append(future.result())
                self._update_progress(len(items), len(jobs), "Finding duplicates",
                                      os.path.basename(path))

        result = deduplicate(items)
        self.dedup_result = result
        self.dedup_items = {item.path: item for item in items}
        for dropped in result.drop:
            self.outcomes[dropped] = 'duplicate'
        skipped = [it for it in items if it.note]
        if skipped:
            self.logger.info(
                "Compared by exact bytes only: %d image(s) (too large, or too little "
                "detail to tell apart visually)", len(skipped)
            )
        kept_paths = {it.path for it in result.keep}

        self.matched_pairs = [p for p in self.matched_pairs if p["media"] in kept_paths]
        self.keep_orphans = [it.path for it in result.keep if not it.has_sidecar]

        for _ in range(len(result.drop)):
            self.session.increment_statistic("skipped_duplicates")
        self.logger.info(
            "Duplicates removed: %d | possible (flagged, kept both): %d | unique kept: %d",
            len(result.drop), len(result.flagged), len(result.keep),
        )
        for dropped, kept in list(result.drop.items())[:20]:
            self.logger.info("  duplicate: %s -> keeping %s",
                             os.path.basename(dropped), os.path.basename(kept))
        for a, b, dist, reason in result.flagged[:20]:
            self.logger.warning("  possible duplicate (%s, distance %d, kept both): %s ~ %s",
                                reason, dist, os.path.basename(a), os.path.basename(b))
        return True

    def _sidecar_time(self, json_file: str) -> Optional[int]:
        """The sidecar's capture time, one of the signals dedup compares."""
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                raw = (json.load(f).get('photoTakenTime') or {}).get('timestamp')
        except (OSError, ValueError, AttributeError):
            return None
        return check_timestamp(raw)[0] if raw is not None else None

    def _write_dedup_report(self) -> None:
        """Write a markdown summary of what got merged / flagged, into the dest."""
        result = self.dedup_result
        if not result or (not result.drop and not result.flagged):
            return

        def rel(path: str) -> str:
            try:
                return os.path.relpath(path, self.merged_dir)
            except Exception:
                return os.path.basename(path)

        lines = [
            "# Duplicate report\n\n",
            f"Mode: `{self.dedup_mode}`\n\n",
            f"Removed {len(result.drop)} duplicate(s); "
            f"{len(result.flagged)} possible match(es) kept for review; "
            f"{len(result.keep)} unique file(s) kept.\n",
        ]
        if result.drop:
            lines.append("\n## Removed duplicates (kept the best copy)\n\n")
            lines.append("| Dropped | Kept instead |\n|---|---|\n")
            for dropped, kept in sorted(result.drop.items()):
                lines.append(f"| {rel(dropped)} | {rel(kept)} |\n")
        if result.flagged:
            lines.append("\n## Possible duplicates (kept both, review these)\n\n")
            lines.append("| File A | File B | Visual distance | Why both were kept |\n"
                         "|---|---|---|---|\n")
            for a, b, dist, reason in result.flagged:
                lines.append(f"| {rel(a)} | {rel(b)} | {dist} | {reason} |\n")

        path = os.path.join(self.output_dir, STATE_DIR, "duplicates.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            self.logger.info("Duplicate report written: %s", path)
        except OSError as exc:
            self.logger.warning("Could not write duplicate report: %s", exc)

    def _note_date(self, media_file: str, metadata: Dict[str, Any]) -> None:
        """Log how a file will be dated and remember the planned date."""
        name = os.path.basename(media_file)
        rejected = metadata.get('date_rejected')
        if rejected:
            self.date_rejected[media_file] = rejected
        if rejected:
            self.logger.warning(f"{name}: ignored the date in its sidecar ({rejected})")

        # Local time where the photo was taken, as the real run will write it.
        # (A camera date that is the same moment wins in the real run; it
        # differs from this at most by the zone, never by the moment.)
        local = self.metadata_handler.capture_time(metadata)
        if local is not None:
            self.planned_dates[media_file] = local.replace(tzinfo=None)
        elif 'upload_timestamp' in metadata:
            upload = to_local(metadata['upload_timestamp'], self.metadata_handler.timezone)
            self.upload_dates[media_file] = upload.replace(tzinfo=None)

    def _phase_plan(self) -> bool:
        """Phase 3: decide, for every kept file, its date, folder and name.

        Every run plans first and then applies the plan, so a preview shows
        exactly what a real run does. The date is the one the write will put
        in the file (the same decision as the write, made from the tags the
        file already has), so photos land in the folder the preview showed.
        """
        self.logger.info("\n--- Phase 3: Planning ---")
        self.session.set_phase('planning')
        media = [pair['media'] for pair in self.matched_pairs] + list(self.keep_orphans)
        can_read = bool(self.metadata_handler.exiftool_path)
        taken = set()  # planned target paths, so two files never plan the same name
        new = []       # entries planned in this run (not reused), named below
        self._load_library()

        for idx, media_file in enumerate(media, 1):
            if self._stopped():
                return False
            self._update_progress(idx, len(media), "Planning", os.path.basename(media_file))
            entry = {'path': media_file, 'json': self.sidecar_of.get(media_file),
                     'metadata': {}, 'existing': {}, 'error': None}
            if entry['json']:
                try:
                    entry['metadata'] = self.metadata_handler.extract_metadata_from_json(
                        entry['json']) or {}
                except Exception as exc:
                    entry['error'] = str(exc)
                self._note_date(media_file, entry['metadata'])
            previous = self.previous_rows.get(self._id_of(media_file)) \
                or self._in_library(media_file)
            if previous and previous.get('target_path'):
                # Decided by an earlier run (the one resumed, the saved plan, or
                # a run that already put this file in the library): same date,
                # folder and name. The write reads the file's tags itself.
                self._reuse_entry(entry, previous, taken)
                self.plan.append(entry)
                continue
            if can_read:
                entry['existing'] = self.metadata_handler.read_existing(media_file)

            date, source = self._predict_date(entry)
            self.date_info[media_file] = (date, source)
            if source in ('upload', 'file time'):
                folder = os.path.join(self.dest_folder, UNDATED_DIR)
            else:
                folder = self.file_processor.date_folder(media_file, self.dest_folder, date)
                if self.place_names:
                    folder = self._with_place(folder, entry)
            entry.update(date=date, source=source, folder=folder)
            self.plan.append(entry)
            new.append(entry)

        self._keep_pairs_together(new)
        # Names come second, once every reused file has its name: a new file
        # never takes a name an earlier run's file already has.
        group_dates = self._rename_dates(new) if self.rename else {}
        for entry in new:
            entry['name'] = self._planned_name(entry, group_dates)
            entry['target'] = self._unique_planned(entry['folder'], entry['name'], taken)
        if self.heic_copies:
            self._plan_heic_copies(taken)

        for path in self.other_files:
            previous = self.previous_rows.get(self._id_of(path)) or self._in_library(path)
            if previous and previous.get('target_path'):
                entry = {'path': path, 'other': True}
                self._reuse_entry(entry, previous, taken)
                self.plan.append(entry)
                continue
            relative = os.path.relpath(path, self.merged_dir)
            target = os.path.join(self.dest_folder, OTHER_DIR, relative)
            self.plan.append({'path': path, 'other': True, 'folder': os.path.dirname(target),
                              'target': self._unique_planned(os.path.dirname(target),
                                                             os.path.basename(target), taken)})

        self._record_plan()
        return True

    def _with_place(self, folder: str, entry: Dict[str, Any]) -> str:
        """The date folder with the photo's town or city added (2023/06 Tokyo),
        from the location the written file will carry; as it is without one."""
        position = self.metadata_handler.position(entry['metadata'], entry['existing'])
        place = places.place_name(*position) if position else None
        if place is None:
            self.places_missing += 1
            return folder
        self.places_found += 1
        return f"{folder} {place}"

    def _load_library(self) -> None:
        """What earlier real runs put in this destination, from its manifest.
        A preview reads it too, so it shows which files are already there."""
        path = os.path.join(self.dest_folder, STATE_DIR, 'manifest.db')
        if not os.path.exists(path):
            return
        if self.manifest is not None and os.path.normcase(os.path.abspath(path)) == \
                os.path.normcase(os.path.abspath(self.manifest_path)):
            self.library = self.manifest.library(exclude_run=self.run_id)
            self.derived_by_target = self.manifest.derived_files(exclude_run=self.run_id)
        else:
            library = Manifest(path)
            try:
                self.library = library.library()
                self.derived_by_target = library.derived_files()
            finally:
                library.close()
        for row in self.library.values():
            if row['sha256'] and row['size'] is not None:
                self.library_by_content.setdefault((row['size'], row['sha256']), []).append(row)
                self._library_sizes.add(row['size'])

    def _in_library(self, path: str) -> Optional[Dict[str, Any]]:
        """The library row of a file an earlier run wrote, if that file is
        still in the destination exactly as written; it is then marked done.
        Found by input ID (the same export), else by content (a newer export
        of the same account, whose ZIPs have other names)."""
        row = self.library.get(self._id_of(path))
        if row and self._still_there(row):
            return dict(row, status='done')
        if not self.library_by_content:
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size not in self._library_sizes:
            return None  # nothing of this size there: no need to read the file
        candidates = self.library_by_content.get((size, self._sha_of(path)), [])
        for row in reversed(candidates):  # the latest run first
            if self._still_there(row):
                return dict(row, status='done')
        return None

    def _still_there(self, row: Dict[str, Any]) -> bool:
        """Whether the file a library row wrote is still in the destination,
        unchanged since (same size and modification time)."""
        target = os.path.join(self.dest_folder, *row['target_path'].split('/'))
        try:
            stat = os.stat(target)
        except OSError:
            return False
        return row['output_size'] is None or (stat.st_size == row['output_size'] and
                                              stat.st_mtime_ns == row['output_mtime_ns'])

    def _sha_of(self, path: str) -> Optional[str]:
        """SHA-256 of the input as it came from the export (before metadata is
        written into it): from the duplicate check, the run being resumed, or
        read now."""
        if path not in self._shas:
            item = self.dedup_items.get(path)
            sha = getattr(item, 'sha', None) \
                or self.previous_rows.get(self._id_of(path), {}).get('sha256')
            if not sha:
                try:
                    sha = sha256(path)
                except OSError:
                    sha = None
            self._shas[path] = sha
        return self._shas[path]

    def _plan_heic_copies(self, taken: set) -> None:
        """With --heic-copies: where each HEIC photo's JPG copy goes, beside it
        and named like it, unless an earlier run already made it."""
        for entry in self.plan:
            target = entry.get('target')
            if entry.get('other') or not target or \
                    os.path.splitext(target)[1].lower() not in HEIC_EXTENSIONS:
                continue
            earlier = self.derived_by_target.get(self._relative_target(target))
            if earlier and os.path.exists(
                    os.path.join(self.dest_folder, *earlier['derived_path'].split('/'))):
                continue
            stem = os.path.splitext(os.path.basename(target))[0]
            entry['copy_target'] = self._free_copy_name(os.path.dirname(target), stem, taken)

    @staticmethod
    def _free_copy_name(folder: str, stem: str, taken: set) -> str:
        """folder/stem.jpg, or stem_1.jpg ... when a planned file or a file
        already in the library has the name (a Takeout often holds IMG_1.HEIC
        and IMG_1.jpg both), so a preview shows the name the run will use."""
        path, counter = os.path.join(folder, stem + '.jpg'), 0
        while os.path.normcase(path) in taken or os.path.exists(path):
            counter += 1
            path = os.path.join(folder, f"{stem}_{counter}.jpg")
        taken.add(os.path.normcase(path))
        return path

    def _make_heic_copy(self, entry: Dict[str, Any]) -> None:
        """Make the planned JPG copy of a HEIC photo now in place: decoded
        upright, saved at quality 92 with its color profile, given every tag of
        the HEIC, moved beside it, recorded on the HEIC's row. A failure leaves
        the HEIC as it is and is logged."""
        source, target = self.output_paths.get(entry['path']), entry['copy_target']
        if not source:
            return
        if os.path.normcase(source) != os.path.normcase(entry['target']):
            # The photo was there already under another name (IMG_1_1.HEIC):
            # the copy is named after the file it was made from.
            target = os.path.join(os.path.dirname(source),
                                  os.path.splitext(os.path.basename(source))[0] + '.jpg')
        if self.dry_run:
            self.heic_copies_made += 1
            return
        from PIL import Image
        if os.path.exists(target):
            target = self.file_processor._get_unique_filename(target)
        folder = os.path.join(self.temp_dir, 'heic-copies')
        os.makedirs(folder, exist_ok=True)
        temp = os.path.join(folder, os.path.basename(target))
        try:
            with Image.open(source) as image:
                icc = image.info.get('icc_profile')
                extra = {'icc_profile': icc} if icc else {}
                image.convert('RGB').save(temp, 'JPEG', quality=92, **extra)
            self.metadata_handler.copy_tags(source, temp)
        except Exception as e:
            self.logger.warning(f"Could not make a JPG copy of {os.path.basename(source)}: {e}")
            if os.path.exists(temp):
                os.remove(temp)
            return
        if not self._place(temp, target):
            self.logger.warning(f"Could not put the JPG copy of {os.path.basename(source)} "
                                "in place")
            return
        self.heic_copies_made += 1
        if self.manifest is not None:
            stat = os.stat(target)
            self.manifest.update_file(self.run_id, self._id_of(entry['path']),
                                      derived_path=self._relative_target(target),
                                      derived_size=stat.st_size,
                                      derived_mtime_ns=stat.st_mtime_ns)

    def _keep_pairs_together(self, new: List[Dict[str, Any]]) -> None:
        """A Live or motion photo's video goes in its photo's folder, and with
        --rename is named by the photo's date. Its own date can fall on another
        day (a video stores UTC, the photo the camera's local time), and only
        one of them may have a sidecar or a place. Both dates must be real: a
        guessed one never moves a file in or out of _undated/."""
        by_path = {e['path']: e for e in self.plan if not e.get('other')}
        for entry in new:
            if entry['source'] in ('upload', 'file time') or self._is_image(entry['path']):
                continue
            photo = by_path.get(self.matcher.photo_of_video(entry['path']))
            if photo is None or photo['source'] in ('upload', 'file time') \
                    or photo.get('date') is None:
                continue
            if entry['folder'] != photo['folder']:
                self.logger.debug(f"{os.path.basename(entry['path'])}: in its photo's folder")
            entry['folder'] = photo['folder']
            entry['pair_date'] = photo['date']

    def _rename_dates(self, entries: List[Dict[str, Any]]) -> Dict[str, datetime]:
        """With --rename: the date each sidecar's files are named by. A Live
        Photo's video and an edited copy share their photo's sidecar; naming
        them all by the photo's date keeps them paired by name."""
        dates: Dict[str, datetime] = {}
        for entry in sorted(entries, key=lambda e: (not self._is_image(e['path']),
                                                    self._rule_of.get(e['path']) == 'edited')):
            sidecar = self.sidecar_of.get(entry['path'])
            if sidecar and entry.get('date') is not None and sidecar not in dates:
                dates[sidecar] = entry['date']
        return dates

    def _planned_name(self, entry: Dict[str, Any], group_dates: Dict[str, datetime]) -> str:
        """The file's name in the library: its own, or with --rename the
        template's. A file with only a guessed date keeps its own name."""
        name = os.path.basename(entry['path'])
        if not self.rename or entry['source'] in ('upload', 'file time') \
                or entry.get('date') is None:
            return name
        date = entry.get('pair_date') or group_dates.get(self.sidecar_of.get(entry['path'])) \
            or entry['date']
        return naming.render(self.rename, name, date)

    def _reuse_entry(self, entry: Dict[str, Any], previous: Dict[str, Any], taken: set) -> None:
        """Fill a plan entry from the earlier run's row. A row it finished, whose
        file is still in the destination, is not done again."""
        target = os.path.join(self.dest_folder, *previous['target_path'].split('/'))
        date = datetime.fromisoformat(previous['date_chosen']) if previous.get('date_chosen') \
            else None
        source = previous.get('date_source') or 'file time'
        entry.update(date=date, source=source, folder=os.path.dirname(target), target=target,
                     existing=None)
        if not entry.get('other'):
            self.date_info[entry['path']] = (date, source)
        taken.add(os.path.normcase(target))
        if previous.get('status') == 'done' and os.path.exists(target):
            entry['done'] = previous

    @staticmethod
    def _unique_planned(folder: str, name: str, taken: set) -> str:
        """folder/name, or name_1, name_2 ... when another planned file has it.
        Files already in the destination are handled when copying: an identical
        one is skipped, a different one gets a free name then."""
        path = os.path.join(folder, name)
        stem, ext = os.path.splitext(name)
        counter = 0
        while os.path.normcase(path) in taken:
            counter += 1
            path = os.path.join(folder, f"{stem}_{counter}{ext}")
        taken.add(os.path.normcase(path))
        return path

    def _writes_tags(self, entry: Dict[str, Any]) -> bool:
        """Whether applying will write metadata into this file itself."""
        media_file = entry['path']
        return bool(entry.get('json')) and not entry.get('error') \
            and media_file not in self.wrong_extension \
            and self.metadata_handler.can_write(media_file)

    def _is_image(self, media_file: str) -> bool:
        if self.matcher.is_image_file(media_file):
            return True
        if self.matcher.is_video_file(media_file):
            return False
        return content_kind(media_file) != 'video'

    def _predict_date(self, entry: Dict[str, Any]) -> Tuple[Optional[datetime], str]:
        """Date that decides the output folder, and where it came from (one of
        DATE_SOURCES). A None date means the file goes to _undated/.

        In order: the date the photo will carry once written (Google's, or a
        camera date that is kept), a date already in the file, the sidecar
        date (for files whose metadata goes elsewhere, e.g. BMP or videos), a
        date in the file name, the upload date. The file time comes last
        because after extraction it is just the day of the run.
        """
        media_file = entry['path']
        if self._is_image(media_file):
            if self._writes_tags(entry):
                date, source = self.metadata_handler.predicted_capture(
                    entry['metadata'], entry['existing'])
                if date is not None:
                    return date, source
            camera = parse_exif_date(entry['existing'].get('DateTimeOriginal'))
            if camera is not None:
                return camera, 'exif'
        elif not entry['json']:
            # A video's own date (QuickTime stores UTC), e.g. in a shared-album
            # download, which has no sidecar.
            stored = parse_exif_date(entry['existing'].get('CreateDate'))
            if stored is not None:
                stamp = int((stored.replace(tzinfo=timezone.utc) - EPOCH).total_seconds())
                return (to_local(stamp, self.metadata_handler.timezone).replace(tzinfo=None),
                        'exif')

        if media_file in self.planned_dates:
            return self.planned_dates[media_file], 'sidecar'

        name = os.path.basename(media_file)
        from_name = date_from_filename(name)
        if from_name is not None:
            moment, is_utc = from_name
            if is_utc:
                stamp = (moment.replace(tzinfo=timezone.utc) - EPOCH).total_seconds()
                moment = to_local(int(stamp), self.metadata_handler.timezone).replace(tzinfo=None)
            self.logger.debug(f"{name}: dated from its file name")
            return moment, 'filename'

        if media_file in self.upload_dates:
            self.logger.warning(f"{name}: no capture date found; filed by its upload date")
            return self.upload_dates[media_file], 'upload'
        return None, 'file time'

    def plan_summary(self) -> List[str]:
        """What applying the plan will do, in a few lines (for a confirmation)."""
        media = [e for e in self.plan if not e.get('other')]
        undated = sum(1 for e in media if e['source'] in ('upload', 'file time'))
        drops = len(self.dedup_result.drop) if self.dedup_result else 0
        unreadable = len(self.file_processor.unreadable_members)
        rows = [
            ("Photos and videos to write", len(media)),
            (f"  of them in {UNDATED_DIR}/ (date is a guess)", undated),
            ("Duplicates to skip", drops),
            (f"Other files to copy to {OTHER_DIR}/", len(self.plan) - len(media)),
            ("Files that cannot be read", unreadable),
        ]
        lines = [f"{label:<40}{value:>9,}" for label, value in rows]
        return lines + [f"Destination: {self.dest_folder}"]

    def _phase_confirm(self) -> bool:
        """Ask before writing anything, when a confirm callback was given."""
        if self.dry_run or self.confirm is None:
            return True
        if self.confirm(self.plan_summary()):
            return True
        self.declined = True
        self._run_status = 'declined'
        self.logger.info("Not confirmed. Nothing was written.")
        return False

    def _phase_apply(self) -> bool:
        """Phase 4: carry out the plan, row by row, recording each outcome.

        Writing metadata is most of this phase. With many files, a few ExifTool
        processes write it at once, each into its own working copy, ahead of the
        main loop; everything else (placing files, the manifest, counts, the
        log) happens here, in plan order, as with one process."""
        self.logger.info("\n--- Phase 4: Writing metadata and copying ---")
        self.session.set_phase('organizing')
        writers = self._start_tag_writers(sum(1 for e in self.plan if self._needs_tags(e)))
        try:
            return self._apply_plan(writers)
        finally:
            for handler in writers:
                handler.close()

    def _needs_tags(self, entry: Dict[str, Any]) -> bool:
        return (not entry.get('done') and not entry.get('other')
                and bool(entry.get('json') or entry.get('source') == 'filename'))

    def _start_tag_writers(self, jobs: int) -> List[MetadataHandler]:
        """ExifTool handlers for the worker threads, or none when the run is
        small, a preview, or without ExifTool: then the main one writes."""
        self.exiftool_processes = 1
        if (self.dry_run or jobs < PARALLEL_FROM or EXIFTOOL_PROCESSES < 2
                or not self.metadata_handler.exiftool_path):
            return []
        writers: List[MetadataHandler] = []
        try:
            for _ in range(EXIFTOOL_PROCESSES):
                writers.append(self.metadata_handler.clone())
        except Exception as exc:
            self.logger.warning(f"Writing with one ExifTool process ({exc})")
            for handler in writers:
                handler.close()
            return []
        self.exiftool_processes = len(writers)
        self.logger.info(f"Writing metadata with {len(writers)} ExifTool processes")
        return writers

    def _apply_plan(self, writers: List[MetadataHandler]) -> bool:
        written = already_present = 0
        pending: Dict[int, Any] = {}   # plan index -> the tag write running for it
        pool = None
        if writers:
            free: "queue.Queue[MetadataHandler]" = queue.Queue()
            for handler in writers:
                free.put(handler)

            def write(entry):
                handler = free.get()   # one thread per ExifTool process at a time
                try:
                    return self._write_tags(entry, handler)
                finally:
                    free.put(handler)

            pool = ThreadPoolExecutor(max_workers=len(writers), thread_name_prefix='exiftool')
            queued = iter([i for i, e in enumerate(self.plan) if self._needs_tags(e)])
            ahead = 4 * len(writers)
        try:
            for idx, entry in enumerate(self.plan):
                if self._stopped():
                    return False
                if pool is not None:
                    while len(pending) < ahead:
                        nxt = next(queued, None)
                        if nxt is None:
                            break
                        pending[nxt] = pool.submit(write, self.plan[nxt])
                future = pending.pop(idx, None)
                media_file = entry['path']
                self._update_progress(idx + 1, len(self.plan), "Copying",
                                      os.path.basename(media_file))
                if not os.path.exists(media_file):
                    self._finish(media_file, 'failed', None, 'the file was not found')
                    continue
                try:
                    if entry.get('done'):
                        # Finished by the run being resumed: it is in the destination.
                        self._finish(media_file, 'already', entry['target'])
                        already_present += 1
                        if entry.get('copy_target'):
                            self._make_heic_copy(entry)
                        continue
                    if entry.get('other'):
                        self._apply_other(entry)
                        continue
                    if future is not None:
                        self._record_tags(media_file, future.result())
                    elif self._needs_tags(entry):
                        self._record_tags(media_file,
                                          self._write_tags(entry, self.metadata_handler))
                    outcome = self._copy_planned(entry)
                    written += outcome == 'written'
                    already_present += outcome == 'already'
                    if entry.get('copy_target') and outcome in ('written', 'already'):
                        self._make_heic_copy(entry)
                except Exception as e:
                    self.logger.error(f"Error organizing {os.path.basename(media_file)}: {e}")
                    self._finish(media_file, 'failed', None, str(e))
        finally:
            if pool is not None:
                # Stopped or failed: drop writes not started; let running ones end.
                pool.shutdown(wait=True, cancel_futures=True)

        self._write_albums()
        self.logger.info(
            f"Organized {written} files ({len(self.keep_orphans)} without sidecars, "
            f"{len(self.session.state['failed_files'])} without new metadata, "
            f"{already_present} already in the destination)"
        )
        return True

    def _write_tags(self, entry: Dict[str, Any], handler: MetadataHandler) -> "_Tags":
        """Write one file's metadata into its working copy with handler (one
        ExifTool process). It may run on a worker thread, so it changes nothing
        but that file; _record_tags records the result on the main thread."""
        result = _Tags()
        if entry.get('json'):
            self._write_metadata(entry, handler, result)
        if entry.get('source') == 'filename':
            self._write_name_date(entry, handler, result)
        return result

    def _record_tags(self, media_file: str, result: "_Tags") -> None:
        for level, message, reason in result.problems:
            getattr(self.logger, level)(message)
            self._metadata_failed(media_file, reason)
        if result.xmp is not None:
            self.xmp_sidecars[media_file] = result.xmp or None
        if result.written:
            self.session.mark_file_processed(media_file)

    def _write_metadata(self, entry: Dict[str, Any], handler: MetadataHandler,
                        result: "_Tags") -> None:
        """Put the sidecar's metadata into the working copy (or next to it).
        A failure is noted and the file is still copied without it."""
        media_file = entry['path']
        name = os.path.basename(media_file)
        if media_file in self.wrong_extension:
            message = (
                f"{name} is a {self.wrong_extension[media_file].upper()} file with the wrong "
                "extension. It was copied without new metadata. Run again with "
                "--fix-extensions to rename and fix it."
            )
            result.problems.append(('warning', message, message))
            return
        if entry.get('error'):
            result.problems.append(('error', f"Error processing {name}: {entry['error']}. "
                                    "It will be copied without new metadata.", entry['error']))
            return
        if self.dry_run:
            if entry['metadata'] and not handler.can_write(media_file):
                result.xmp = ''  # counted in the preview
            return
        try:
            if not handler.can_write(media_file):
                # ExifTool cannot write this format (BMP, AVI, MKV, ...): the
                # metadata goes in name.ext.xmp, copied along with it.
                xmp = media_file + '.xmp'
                if handler.write_xmp_sidecar(xmp, entry['metadata']):
                    result.xmp = xmp
            elif self._is_image(media_file):
                handler.apply_metadata_to_image(
                    media_file, entry['metadata'], existing=entry['existing'])
            else:
                handler.apply_metadata_to_video(
                    media_file, entry['metadata'], existing=entry['existing'])
            result.written = True
        except Exception as e:
            # The file is still copied, just without new metadata. Dropping it
            # would lose the photo.
            result.problems.append(('error', f"Error processing {name}: {e}. "
                                    "It will be copied without new metadata.", str(e)))

    def _write_name_date(self, entry: Dict[str, Any], handler: MetadataHandler,
                         result: "_Tags") -> None:
        """Put a date found in the file's name into the file (it has no date of
        its own, or it would not have been dated by its name). A failure is
        noted and the file is still copied."""
        media_file = entry['path']
        if (self.dry_run or entry.get('date') is None or media_file in self.wrong_extension
                or not handler.can_write(media_file)):
            return
        try:
            handler.write_name_date(media_file, entry['date'], self._is_image(media_file))
        except Exception as e:
            name = os.path.basename(media_file)
            result.problems.append(('error', f"Could not write the date from the name of "
                                    f"{name}: {e}. It will be copied without it.", str(e)))

    def _metadata_failed(self, media_file: str, message: str) -> None:
        self.metadata_errors[media_file] = message
        self.session.mark_file_failed(media_file, message)

    def _copy_planned(self, entry: Dict[str, Any]) -> str:
        """Copy a photo or video to its planned place; returns the outcome."""
        media_file, target = entry['path'], entry['target']
        # A rerun into the same destination must not copy the library a second
        # time as NAME_1.jpg: an identical copy under the name or its _N
        # variants means it is already there.
        natural = os.path.join(entry['folder'], entry.get('name') or os.path.basename(media_file))
        existing = self.file_processor.find_identical_copy(media_file, natural)
        if existing:
            self.logger.debug(f"Already in destination: {existing}")
            self._finish(media_file, 'already', existing)
            return 'already'

        if os.path.exists(target):
            target = self.file_processor._get_unique_filename(target)
        if self.dry_run:
            self.logger.info(f"Would copy: {media_file} -> {target}")
        elif self._place(media_file, target):
            self._copy_sidecar(media_file, target)
            xmp = self.xmp_sidecars.get(media_file)
            if xmp and os.path.exists(xmp) and not os.path.exists(target + '.xmp'):
                self.file_processor.copy_file_safe(xmp, target + '.xmp')
        else:
            self.logger.warning(f"Failed to copy: {os.path.basename(media_file)}")
            self._finish(media_file, 'failed', None, 'could not be copied (see the log)')
            return 'failed'

        date, source = entry['date'], entry['source']
        self.date_sources[source] += 1
        if entry['folder'].endswith(UNDATED_DIR):
            self.undated += 1
            self.undated_names.append(os.path.basename(target))
        elif date is not None:
            self.by_year[str(date.year)] += 1
        self._finish(media_file, 'written', target)
        return 'written'

    def _apply_other(self, entry: Dict[str, Any]) -> None:
        """Copy a file that is not a photo or video to _other/."""
        path, target = entry['path'], entry['target']
        natural = os.path.join(self.dest_folder, OTHER_DIR,
                               os.path.relpath(path, self.merged_dir))
        existing = self.file_processor.find_identical_copy(path, natural)
        if existing:
            self._finish(path, 'already', existing)
            return
        if os.path.exists(target):
            target = self.file_processor._get_unique_filename(target)
        if self.dry_run:
            self.logger.info(f"Would copy: {path} -> {target}")
        elif not self._place(path, target):
            self._finish(path, 'failed', None, 'could not be copied (see the log)')
            return
        self._finish(path, 'other', target)

    def _place(self, path: str, target: str) -> bool:
        """Put a finished working copy at its target, never over a file. It is
        moved when it can be (the working copy is this run's own and on the
        same drive: no second copy, and a rename is never half-done), and
        copied otherwise. A file outside the working copy is always copied."""
        if self.temp_dir and _is_inside(path, self.temp_dir):
            return self.file_processor.move_file_safe(path, target)
        return self.file_processor.copy_file_safe(path, target, overwrite=False)

    def _finish(self, path: str, outcome: str, target: Optional[str],
                error: Optional[str] = None) -> None:
        """Record what happened to one planned file, in memory and in the
        manifest (a preview leaves its rows as planned)."""
        self.outcomes[path] = outcome
        if target:
            self.output_paths[path] = target
        if error:
            self.failure_reasons[path] = error
        if self.manifest is None:
            return
        metadata_error = self.metadata_errors.get(path)
        fields = {'outcome': outcome, 'target_path': self._relative_target(target),
                  'error': error or metadata_error}
        if not self.dry_run:
            fields['status'] = 'failed' if outcome == 'failed' else 'done'
            if outcome in ('written', 'other') and target:
                # What was written, so undo can tell a file edited since apart.
                stat = os.stat(target)
                fields.update(output_size=stat.st_size, output_mtime_ns=stat.st_mtime_ns)
        self.manifest.update_file(self.run_id, self._id_of(path), **fields)

    def _write_albums(self) -> None:
        """One playlist per album in Albums/ (`.m3u8`, paths relative to it,
        so the library can move), plus albums.json. An album copy dropped as a
        duplicate points at the copy that was kept."""
        if self.albums == 'off' or not self.album_of:
            return
        drops = self.dedup_result.drop if self.dedup_result else {}
        albums: Dict[str, List[str]] = {}
        for media, album in self.album_of.items():
            kept = drops.get(media, media)
            output = self.output_paths.get(kept)
            if output:
                albums.setdefault(album, [])
                if output not in albums[album]:
                    albums[album].append(output)
        if not albums:
            return
        if self.dry_run:
            self.album_counts = {album: len(paths) for album, paths in albums.items()}
            self.logger.info(f"Would write {len(albums)} album playlists to {ALBUMS_DIR}/")
            return

        self.album_counts = {album: len(paths) for album, paths in albums.items()}
        albums_dir = os.path.join(self.dest_folder, ALBUMS_DIR)
        os.makedirs(albums_dir, exist_ok=True)
        index = {}
        for album, paths in sorted(albums.items()):
            name = _UNSAFE_NAME.sub('_', album).strip(' .') or 'Album'
            lines = ['#EXTM3U'] + [
                os.path.relpath(p, albums_dir).replace(os.sep, '/') for p in sorted(paths)]
            with open(os.path.join(albums_dir, f"{name}.m3u8"), 'w', encoding='utf-8',
                      newline='\n') as f:
                f.write('\n'.join(lines) + '\n')
            index[album] = [os.path.relpath(p, self.dest_folder).replace(os.sep, '/')
                            for p in sorted(paths)]
        with open(os.path.join(albums_dir, 'albums.json'), 'w', encoding='utf-8') as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        self.logger.info(f"Wrote {len(albums)} album playlists to {ALBUMS_DIR}/")

    def _open_manifest(self) -> None:
        """The run's record: dest/.takeout-organizer/manifest.db, or the
        preview folder's own for a dry run."""
        folder = self.output_dir if self.dry_run else os.path.join(self.dest_folder, STATE_DIR)
        self.manifest_path = os.path.join(folder, 'manifest.db')
        self.manifest = Manifest(self.manifest_path)
        self.manifest.start_run(self.run_id, self.source_folder, self.dest_folder, self.dry_run, {
            'layout': self.layout, 'dedup': self.dedup_mode, 'albums': self.albums,
            'date_policy': self.metadata_handler.date_policy, 'time_zone': self.time_zone_text,
            'keep_json': self.keep_json, 'fix_extensions': self.fix_extensions,
            'rename': self.rename, 'heic_copies': self.heic_copies,
            'place_names': self.place_names,
            'exiftool': bool(self.metadata_handler.exiftool_path),
        })

    def _origin(self, path: str) -> Tuple[Optional[str], str]:
        """(ZIP name or None, path inside the ZIP or the source folder)."""
        if ': ' in path and not os.path.exists(path):  # an entry that could not be read
            zip_name, member = path.split(': ', 1)
            return zip_name, member
        if path in self.file_processor.origins:
            # Recorded at extraction, so a working copy renamed later (by
            # --fix-extensions) keeps the name it has in the ZIP.
            return self.file_processor.origins[path]
        original = self.original_path.get(path, path)
        return None, os.path.relpath(original, self.merged_dir).replace(os.sep, '/')

    def _id_of(self, path: str) -> str:
        if path not in self._ids:
            self._ids[path] = make_input_id(*self._origin(path))
        return self._ids[path]

    def _relative_target(self, target: Optional[str]) -> Optional[str]:
        if not target:
            return None
        return os.path.relpath(target, self.dest_folder).replace(os.sep, '/')

    def _record_plan(self) -> None:
        """One manifest row per input file, as planned: the decisions, the
        target, and the intended outcome. Applying updates each row."""
        if self.manifest is None:
            return
        drops = self.dedup_result.drop if self.dedup_result else {}
        flagged = set()
        if self.dedup_result:
            for a, b, _, _ in self.dedup_result.flagged:
                flagged.update((a, b))
        planned = {entry['path']: entry for entry in self.plan}

        def row(path: str) -> Dict[str, Any]:
            zip_name, rel = self._origin(path)
            item = self.dedup_items.get(path)
            entry = planned.get(path, {})
            date, source = self.date_info.get(path, (None, None))
            sidecar = self.sidecar_of.get(path)
            if path in self.other_files:
                kind = 'other'
            elif path in self.sidecar_of or path in self.unmatched_media:
                kind = 'photo' if self._is_image(path) else 'video'
            else:
                kind = None
            role = None
            if path in drops:
                role = 'drop'
            elif path in flagged:
                role = 'flag'
            elif item is not None:
                role = 'keep'
            else:
                role = self.previous_rows.get(self._id_of(path), {}).get('dedup_role')
            if path in drops:
                outcome, status = 'duplicate', 'done'
            elif entry:
                outcome, status = ('other' if entry.get('other') else 'written'), 'planned'
            else:
                outcome, status = 'failed', 'failed'
            if self.dry_run:
                status = 'planned'
            try:
                size = os.path.getsize(path)
            except OSError:
                size = getattr(item, 'size', None)
            return {
                'input_id': self._id_of(path), 'zip': zip_name, 'rel_path': rel, 'size': size,
                # Every planned file of a real run gets its hash, so a later
                # run with a newer export can find it by content.
                'sha256': self._sha_of(path) if entry and not self.dry_run
                and os.path.isfile(path) else getattr(item, 'sha', None)
                or self.previous_rows.get(self._id_of(path), {}).get('sha256'),
                'kind': kind,
                'sidecar': os.path.relpath(sidecar, self.merged_dir).replace(os.sep, '/')
                if sidecar else None,
                'match_rule': self._rule_of.get(path),
                'date_chosen': date.isoformat() if date else None, 'date_source': source,
                'date_rejected': self.date_rejected.get(path),
                'dedup_role': role,
                'kept_input_id': self._id_of(drops[path]) if path in drops else None,
                'target_path': self._relative_target(entry.get('target')),
                # The planned JPG copy of a HEIC; its size is recorded once made.
                'derived_path': self._relative_target(entry.get('copy_target')),
                'outcome': outcome, 'error': self.failure_reasons.get(path), 'status': status,
            }

        count = self.manifest.record_files(self.run_id, (row(p) for p in self.inputs))
        self.logger.info(f"Planned {count} files in the manifest: {self.manifest_path}")

    def _write_html_report(self) -> str:
        """takeout-organizer-report.html next to the photos (in the preview
        folder for a dry run), with thumbnails for the duplicate decisions."""
        thumbs_dir = os.path.join(self.output_dir, STATE_DIR, 'report-thumbs')
        report_file = os.path.join(self.output_dir, 'takeout-organizer-report.html')
        counter = [0]

        def info(path: str, outcome: str, thumb: bool) -> html_report.FileInfo:
            item = self.dedup_items.get(path)
            entry = html_report.FileInfo(
                name=os.path.basename(path), path=path, outcome=outcome,
                size=getattr(item, 'size', 0), width=getattr(item, 'width', 0),
                height=getattr(item, 'height', 0),
                has_sidecar=getattr(item, 'has_sidecar', path in self.sidecar_of))
            if thumb and counter[0] < 2 * html_report.MAX_PAIRS:
                counter[0] += 1
                target = os.path.join(thumbs_dir, f"{counter[0]}.jpg")
                # A kept file was moved out of the working copy into place.
                placed = self.output_paths.get(path)
                source = placed if placed and os.path.exists(placed) else path
                if html_report.make_thumbnail(source, target):
                    entry.thumb = os.path.relpath(target, self.output_dir).replace(os.sep, '/')
            return entry

        if os.path.isdir(thumbs_dir):
            shutil.rmtree(thumbs_dir, ignore_errors=True)
        os.makedirs(thumbs_dir, exist_ok=True)

        flagged, dropped = [], []
        if self.dedup_result:
            for a, b, distance, reason in self.dedup_result.flagged[:html_report.MAX_PAIRS]:
                flagged.append((info(a, 'Kept (both kept)', True),
                                info(b, 'Kept (both kept)', True), distance, reason))
            for gone, kept in list(self.dedup_result.drop.items())[:html_report.MAX_PAIRS]:
                dropped.append((info(gone, 'Not copied (duplicate)', True),
                                info(kept, 'Kept', True)))

        failed = [(os.path.basename(p.split(': ', 1)[-1]),
                   self.failure_reasons.get(p, 'could not be copied (see the log)'))
                  for p in self.inputs if self.outcomes.get(p) == 'failed']
        without = [(os.path.basename(e['file']), e['error'])
                   for e in self.session.state['failed_files']
                   if self.outcomes.get(e['file']) == 'written']
        settings = [
            ("Folder layout", self.layout),
            ("Duplicates", self.dedup_mode),
            ("Date policy", self.metadata_handler.date_policy),
            ("Time zone without GPS or camera", self.time_zone_text),
            ("Time zones from GPS", "on" if has_location_lookup() else
             "off (install takeout-organizer[timezones])"),
            ("Album playlists", self.albums),
            ("Keep JSON sidecars", "yes" if self.keep_json else "no"),
            ("Fix extensions", "yes" if self.fix_extensions else "no"),
        ]
        data = html_report.ReportData(
            title_date=datetime.now(), source=self.source_folder, dest=self.dest_folder,
            dry_run=self.dry_run, settings=settings, summary_lines=self.summary_lines,
            summary=self.summary or {}, failed=failed, without_metadata=without,
            flagged=flagged, dropped=dropped, undated=sorted(self.undated_names),
            by_year=dict(self.by_year), albums=self.album_counts, log_file=self.log_file,
            total_flagged=len(self.dedup_result.flagged) if self.dedup_result else 0,
            total_dropped=len(self.dedup_result.drop) if self.dedup_result else 0,
        )
        return html_report.write(report_file, data)

    def _copy_sidecar(self, media_file: str, dest_path: str) -> None:
        """With keep_json, put the Takeout sidecar next to the copied file."""
        json_file = self.sidecar_of.get(media_file)
        if not self.keep_json or not json_file or not os.path.exists(json_file):
            return
        target = dest_path + '.json'
        if not os.path.exists(target):
            self.file_processor.copy_file_safe(json_file, target, overwrite=False)

    def _account(self) -> List[str]:
        """Summarize what happened to every input file, check that the numbers
        add up, log the table and return its lines."""
        counts = Counter(self.outcomes.get(path, 'missing') for path in self.inputs)
        without_metadata = sum(
            1 for entry in self.session.state['failed_files']
            if self.outcomes.get(entry['file']) == 'written'
        )
        total = len(self.inputs)
        accounted = sum(counts[k] for k in ('written', 'already', 'duplicate', 'other', 'failed'))
        balanced = accounted == total and not counts['missing']

        # Labels are fixed texts with {placeholders}, so the window can show
        # the table in its language; the log and the report use English.
        written = N_("Photos and videos to be written") if self.dry_run \
            else N_("Photos and videos written")
        rows = [
            (N_("Input files"), {}, total, 0),
            (written, {}, counts['written'], 1),
        ]
        rows += [(label, {}, self.date_sources[key], 2) for key, label in DATE_SOURCES]
        rows += [
            (N_("in {folder}/ (date is a guess)"), {'folder': UNDATED_DIR}, self.undated, 2),
            (N_("metadata in an .xmp sidecar"), {}, sum(
                1 for m in self.xmp_sidecars if self.outcomes.get(m) == 'written'), 2),
            (N_("without new metadata"), {}, without_metadata, 2),
        ] + ([(N_("JPG copies of HEIC photos"), {}, self.heic_copies_made, 2)]
             if self.heic_copies else []) + [
            (N_("Already in the destination"), {}, counts['already'], 1),
            (N_("Duplicates not copied"), {}, counts['duplicate'], 1),
            (N_("Other files (in {folder}/)"), {'folder': OTHER_DIR}, counts['other'], 1),
            (N_("Failed to copy"), {}, counts['failed'], 1),
        ]
        notes = []
        flagged = len(self.dedup_result.flagged) if self.dedup_result else 0
        if flagged:
            notes.append((N_("Possible duplicates kept: {count} (see the report)"),
                          {'count': flagged}))
        if self.wrong_extension:
            notes.append((N_("Wrong file extension: {count} (run with --fix-extensions)"),
                          {'count': len(self.wrong_extension)}))
        if self.photos_missing:
            notes.append((N_("Photos missing from the export: {count} (their sidecars are "
                             "there; check that every part is here)"),
                          {'count': len(self.photos_missing)}))
        self.summary_table = {'rows': rows, 'accounted': accounted, 'balanced': balanced,
                              'notes': notes}
        lines = summary_text(self.summary_table)

        self.logger.info("\n--- Summary ---")
        for line in lines:
            self.logger.info(line)
        if not balanced:
            missing = [os.path.basename(p) for p in self.inputs if p not in self.outcomes]
            self.logger.error(f"Files with no recorded outcome: {', '.join(missing[:20])}")

        self.summary_lines = lines
        self.summary = {
            'input': total, 'written': counts['written'], 'already': counts['already'],
            'duplicates': counts['duplicate'], 'other': counts['other'],
            'failed': counts['failed'], 'without_metadata': without_metadata,
            'date_sources': dict(self.date_sources), 'balanced': balanced,
            'heic_copies': self.heic_copies_made,
        }
        return lines

    def _phase_cleanup(self):
        """Phase 5: Cleanup"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            self.logger.info("\n--- Phase 5: Cleanup ---")
            self.logger.info(f"Cleaning up temporary directory: {self.temp_dir}")
            self.file_processor.clean_temp_directory(self.temp_dir)
        if self.own_work_root:
            try:
                os.rmdir(self.work_root)  # only when empty
            except OSError:
                pass


class CallbackHandler(logging.Handler):
    """Custom logging handler that calls a callback function"""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        """Emit log record via callback"""
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            self.callback(msg, level)
        except Exception:
            self.handleError(record)
