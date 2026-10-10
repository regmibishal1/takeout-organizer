"""Command-line interface for Takeout Organizer.

Runs the organizer headless - no GUI, no Tkinter - so it works over SSH and
inside containers. `takeout-organizer gui` still launches the desktop app when
a display is available.

Output follows the Command Line Interface Guidelines (clig.dev): the summary
is the command's output and goes to stdout; progress, warnings and errors are
messages and go to stderr. The full log is always in the log file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional, TextIO

from . import __version__
from .file_processor import LAYOUTS
from .main_processor import MainProcessor, SetupError, check_folders
from .metadata_handler import MetadataHandler
from .preflight import check as preflight_check, counted, default_work_root, human


class Display:
    """Progress and messages for a terminal, or plain lines for logs and cron.

    On a terminal the progress of the current phase is one line, redrawn in
    place. Elsewhere a line is printed every 5 seconds or 5%, so a log file
    gets a readable record instead of carriage returns.
    """

    PLAIN_INTERVAL_SECONDS = 5
    PLAIN_STEP = 0.05

    def __init__(self, stream: TextIO = sys.stderr, quiet: bool = False, verbose: bool = False):
        self.stream = stream
        self.quiet = quiet
        self.verbose = verbose
        self.tty = stream.isatty()
        self._phase: Optional[str] = None
        self._phase_started = 0.0
        self._last_plain = (0.0, -1.0)  # time, fraction
        self._line_open = False

    def _width(self) -> int:
        return max(40, shutil.get_terminal_size((100, 20)).columns - 1)

    def _end_line(self) -> None:
        if self._line_open:
            self.stream.write("\n")
            self._line_open = False

    def progress(self, current: int, total: int, phase: str, details: str) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if phase != self._phase:
            self._end_line()
            self._phase, self._phase_started = phase, now
            self._last_plain = (0.0, -1.0)

        fraction = current / total if total else 0.0
        parts = [f"{phase:<18}", f"{current:,}/{total:,}" if total else "",
                 f"{fraction:4.0%}" if total else ""]
        elapsed = now - self._phase_started
        if total and 0 < current < total and elapsed > 10:
            left = elapsed / current * (total - current)
            parts.append(f"about {_duration(left)} left")
        parts.append(details)
        line = "  ".join(p for p in parts if p)

        if self.tty:
            self.stream.write("\r" + line[: self._width()].ljust(self._width()))
            self._line_open = True
        else:
            last_time, last_fraction = self._last_plain
            if (now - last_time >= self.PLAIN_INTERVAL_SECONDS
                    or fraction - last_fraction >= self.PLAIN_STEP or current == total):
                self.stream.write(line + "\n")
                self._last_plain = (now, fraction)
        self.stream.flush()

    def message(self, text: str, level: str) -> None:
        level = level.lower()
        if level in ("warning", "error", "critical"):
            if self.quiet and level == "warning":
                return
            self._end_line()
            label = "Warning" if level == "warning" else "Error"
            self.stream.write(f"{label}: {text}\n")
        elif self.verbose:
            self._end_line()
            self.stream.write(text + "\n")
        self.stream.flush()

    def finish(self) -> None:
        self._end_line()
        self.stream.flush()


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def _stop_on_ctrl_c(stop: threading.Event):
    """First Ctrl+C: stop after the current file. Second: quit at once.
    Returns the previous handler, to restore afterwards."""
    def handler(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()
        sys.stderr.write("\nStopping after the current file. "
                         "Press Ctrl+C again to quit at once.\n")
        sys.stderr.flush()

    try:
        return signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread: leave Ctrl+C alone
        return None


def _should_ask(args, dry_run: bool) -> bool:
    """A real run on a terminal asks before writing; scripts and cron do not."""
    return not dry_run and not getattr(args, "yes", False) and sys.stdin.isatty()


def _ask_first(display: Display):
    """Show the plan and ask whether to go ahead with it."""
    def confirm(plan_lines) -> bool:
        display.finish()
        sys.stdout.write("\nPlan\n\n" + "\n".join(plan_lines) + "\n\n")
        sys.stdout.flush()
        try:
            answer = input("Proceed? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")

    return confirm


def _add_folder_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, type=Path,
                        help="Folder containing Takeout ZIP files (or extracted export)")
    parser.add_argument("--dest", required=True, type=Path,
                        help="Destination folder for organized output")
    parser.add_argument("--exiftool", type=Path, default=None,
                        help="Path to the exiftool executable (default: search PATH)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="Folder for the temporary working copy. Default: "
                             ".takeout-organizer/work in the destination, so finished files "
                             "are moved into place, not copied. Needs about the size of the "
                             "export")


def _add_run_arguments(parser: argparse.ArgumentParser, with_dry_run: bool) -> None:
    _add_folder_arguments(parser)
    if with_dry_run:
        parser.add_argument("--dry-run", action="store_true",
                            help="Preview the full plan without modifying any files "
                                 "(same as the plan command)")
        parser.add_argument("--yes", "-y", action="store_true",
                            help="Do not ask before writing (a run on a terminal shows the "
                                 "plan and asks first)")
        reuse = parser.add_mutually_exclusive_group()
        reuse.add_argument("--resume", action="store_true",
                           help="Continue the last stopped run into this destination, "
                                "skipping the files it finished")
        reuse.add_argument("--plan", type=Path, default=None, metavar="FILE",
                           help="Apply a plan made by the plan command (its manifest.db) "
                                "exactly, without deciding again")
    # Removed: the backups it made lived in the temporary working copy and
    # were deleted with it. Still accepted so existing scripts keep working.
    parser.add_argument("--preserve-originals", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--keep-json", action="store_true",
                        help="Put each Takeout JSON sidecar next to its photo in the output")
    parser.add_argument("--dedup", choices=["off", "exact", "perceptual"], default="perceptual",
                        help="Duplicate handling: off, exact (byte-identical only), or "
                             "perceptual (also catches re-compressed copies). Default: perceptual")
    # Replaced by --resume; still accepted so existing scripts keep working.
    parser.add_argument("--session", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--timezone", default=None, metavar="ZONE",
                        help="Time zone for photos whose location and camera give none, "
                             "e.g. +09:00 or Asia/Tokyo. Default: this computer's zone")
    parser.add_argument("--date-policy", choices=["google-first", "camera-first"],
                        default="google-first",
                        help="When Google's date and the camera's are different moments: keep "
                             "Google's (it has your edits; default) or the camera's")
    parser.add_argument("--overwrite-gps", action="store_true",
                        help="Replace a location the camera recorded with Google's")
    parser.add_argument("--people-as-keywords", action="store_true",
                        help="Also add people's names to the keywords (they always go in "
                             "the IPTC PersonInImage field)")
    parser.add_argument("--fix-extensions", action="store_true",
                        help="Rename files whose extension does not match their content "
                             "(e.g. a .heic that is a JPEG) so they get metadata; the output "
                             "copy gets the right extension")
    parser.add_argument("--layout", choices=list(LAYOUTS), default="year-month",
                        help="Folders by date: year-month (2023/06, default), "
                             "year-month-day (2023/06/15), year, or flat")
    parser.add_argument("--albums", choices=["playlists", "off"], default="playlists",
                        help="Write one playlist per album to Albums/ (default) or none")
    parser.add_argument("--rename", metavar="TEMPLATE", type=_name_template, default=None,
                        help="Give photos names that sort by date, from a template with "
                             "{date}, {date:FORMAT} and {name}, e.g. "
                             "\"{date:%%Y%%m%%d_%%H%%M%%S}_{name}\". The extension is kept; "
                             "files with only a guessed date keep their own names")
    parser.add_argument("--heic-copies", choices=["jpg"], default=None,
                        help="Also put a JPG copy beside each HEIC photo, for devices that "
                             "cannot show HEIC; the HEIC is always kept. Needs the [heic] "
                             "extra (the standalone apps have it)")
    parser.add_argument("--place-names", action="store_true",
                        help="Add the town or city each photo was taken in to its date "
                             "folder (2023/06 Tokyo), from its location, offline. Place "
                             "names from GeoNames (geonames.org), CC BY 4.0")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--quiet", action="store_true",
                        help="Show only errors and the summary")
    output.add_argument("--verbose", action="store_true",
                        help="Also show every step (the log file always has them)")
    parser.add_argument("--json", action="store_true",
                        help="Print the summary as JSON on stdout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="takeout-organizer",
        description="Restore metadata and organize Google Photos Takeout exports.",
        epilog="Start with: takeout-organizer check --source DIR --dest DIR",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="Check the export, ExifTool and disk space; "
                                         "writes nothing")
    _add_folder_arguments(check)

    plan = sub.add_parser("plan", help="Preview everything a run would do; writes nothing "
                                       "to the destination")
    _add_run_arguments(plan, with_dry_run=False)

    run = sub.add_parser("run", help="Process a Takeout export (headless)")
    _add_run_arguments(run, with_dry_run=True)

    undo = sub.add_parser("undo", help="Delete the files a run wrote into the destination "
                                       "(your Takeout files are not affected)")
    undo.add_argument("--dest", required=True, type=Path,
                      help="Destination folder the run wrote to")
    undo.add_argument("--run", default=None, metavar="ID",
                      help="The run to undo (default: the latest run that wrote files)")
    undo.add_argument("--yes", "-y", action="store_true",
                      help="Do not ask; needed when not on a terminal")

    verify = sub.add_parser("verify", help="Check that the files the runs put in the "
                                           "destination are still there, unchanged and "
                                           "with their dates; changes nothing")
    verify.add_argument("--dest", required=True, type=Path,
                        help="Destination folder the runs wrote to")
    verify.add_argument("--exiftool", type=Path, default=None,
                        help="Path to the exiftool executable (default: search PATH)")
    verify.add_argument("--json", action="store_true",
                        help="Print the result as JSON (every file, for scripts)")

    diagnose = sub.add_parser("diagnose", help="Print versions and the last run's summary "
                                               "and warnings for a bug report, with your "
                                               "folder paths left out")
    diagnose.add_argument("--dest", type=Path, default=None,
                          help="Destination folder of the run to describe (default: the one "
                               "last chosen in the window, if any)")

    sub.add_parser("gui", help="Launch the desktop GUI")
    return parser


def _check(args) -> int:
    """`check`: report what a run would find, and stop there."""
    out = sys.stdout
    source, dest = str(args.source), str(args.dest)
    work = str(args.work_dir) if args.work_dir else None
    problems = []
    out.write(f"Takeout Organizer {__version__}\n\n")

    try:
        check_folders(source, dest)
    except SetupError as exc:
        out.write(f"Problem: {exc}\n")
        return 2
    if work is not None and not os.path.isdir(work):
        out.write(f"Problem: The work folder does not exist: {work}\n")
        return 2

    result = preflight_check(source, dest, work or default_work_root(dest, dry_run=False),
                             dry_run=False)
    zips = f"{result.zip_count} ZIP files" if result.zip_count else "an extracted export"
    out.write(f"{'Source':<10}{source}  ({zips}, {human(result.source_bytes)})\n")

    handler = MetadataHandler(str(args.exiftool) if args.exiftool else None, require=False)
    if handler.exiftool_path:
        out.write(f"{'ExifTool':<10}{handler.get_exiftool_version()} at "
                  f"{handler.exiftool_path}\n")
    else:
        problems.append(MetadataHandler._MISSING_MSG)
        out.write(f"{'ExifTool':<10}not found{'PROBLEM':>40}\n")

    def space_line(label, needed, free, drive, first):
        status = "OK" if free is None or needed * 1.05 <= free else "PROBLEM"
        free_text = f"{human(free)} free" if free is not None else "free space unknown"
        out.write(f"{'Space' if first else '':<10}needs about {human(needed)} {label} on "
                  f"{drive}, {free_text}   {status}\n")

    if result.same_drive:
        # Finished files are moved out of the working copy: room for one is enough.
        space_line("for the working copy, moved into place",
                   max(result.work_needed, result.dest_needed),
                   result.work_free, result.dest_drive, True)
    else:
        space_line("working space", result.work_needed, result.work_free, result.work_drive, True)
        space_line("for the output", result.dest_needed, result.dest_free, result.dest_drive,
                   False)

    for warning in result.warnings:
        out.write(f"\nWarning: {warning}\n")
    problems += result.problems
    for problem in result.problems:
        out.write(f"\nProblem: {problem}\n")

    next_step = f"takeout-organizer plan --source {_quote(source)} --dest {_quote(dest)}"
    if problems:
        count = len(problems)
        out.write(f"\n{count} problem{'s' if count > 1 else ''} found. Fix "
                  f"{'them' if count > 1 else 'it'}, then run:\n  {next_step}\n")
        return 2
    out.write(f"\nNo problems found. Next, preview the plan:\n  {next_step}\n")
    return 0


def _undo(args) -> int:
    """`undo`: delete what one run wrote, after saying exactly what."""
    from . import undo
    from .runguard import RunLock

    dest = str(args.dest)
    plan, reason = undo.plan(dest, args.run)
    if plan is None:
        sys.stderr.write(f"{reason}\n")
        return 2
    started = plan.started.replace('T', ' ')[:16]
    for path in plan.changed:
        sys.stderr.write(f"Kept, edited since the run: {path}\n")
    if not plan.files:
        sys.stdout.write(f"The run of {started} left nothing to delete.\n")
        return 0
    question = (f"Delete {counted(len(plan.files), 'file')} ({human(plan.total_bytes)}) "
                f"written by the "
                f"run of {started}? Your Takeout files are not affected.")
    if not args.yes:
        if not sys.stdin.isatty():
            sys.stderr.write(f"{question}\nRun with --yes to delete without asking.\n")
            return 2
        try:
            answer = input(f"{question} [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            sys.stderr.write("Nothing was deleted.\n")
            return 3

    lock = RunLock(dest)
    busy = lock.acquire()
    if busy:
        sys.stderr.write(f"Another run is using this folder ({busy}).\n")
        return 2
    try:
        deleted = undo.apply(dest, plan)
    finally:
        lock.release()
    sys.stdout.write(f"Deleted {counted(deleted, 'file')} written by run {plan.run_id}.\n")
    if plan.changed:
        sys.stdout.write(f"{counted(len(plan.changed), 'file')} edited since the run were kept.\n")
    return 0


def _last_window_dest():
    """The destination last chosen in the window, or None."""
    try:
        from .gui_support import Settings
        return Settings.load().dest or None
    except Exception:
        return None


def _name_template(text: str) -> str:
    """argparse type for --rename: a usable name template, or exit 2."""
    from .naming import check_template
    try:
        return check_template(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


def _run(args, dry_run: bool) -> int:
    if args.preserve_originals:
        sys.stderr.write("Note: --preserve-originals no longer does anything and will be "
                         "removed. Your Takeout files are never changed.\n")
    display = Display(quiet=args.quiet, verbose=args.verbose)
    stop = threading.Event()
    try:
        processor = MainProcessor(
            source_folder=str(args.source),
            dest_folder=str(args.dest),
            dry_run=dry_run,
            keep_json=args.keep_json,
            dedup_mode=args.dedup,
            exiftool_path=str(args.exiftool) if args.exiftool else None,
            resume=getattr(args, "resume", False) or args.session is not None,
            plan_file=str(args.plan) if getattr(args, "plan", None) else None,
            progress_callback=display.progress,
            log_callback=display.message,
            time_zone=args.timezone,
            date_policy=args.date_policy,
            overwrite_gps=args.overwrite_gps,
            people_as_keywords=args.people_as_keywords,
            fix_extensions=args.fix_extensions,
            work_dir=str(args.work_dir) if args.work_dir else None,
            cancel_event=stop,
            confirm=_ask_first(display) if _should_ask(args, dry_run) else None,
            console=False,
            layout=args.layout,
            albums=args.albums,
            rename=args.rename,
            heic_copies=args.heic_copies,
            place_names=args.place_names,
        )
    except SetupError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    previous = _stop_on_ctrl_c(stop)
    try:
        ok = processor.run()
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        display.finish()

    if args.json:
        sys.stdout.write(json.dumps({
            "ok": ok, "stopped": processor.cancelled, "dry_run": dry_run,
            "summary": processor.summary, "log": processor.log_file,
            "report": processor.report_path, "manifest": processor.manifest_path,
        }, indent=2) + "\n")
    elif processor.summary_lines:
        title = "Plan (nothing was written)" if dry_run else "Summary"
        sys.stdout.write(f"\n{title}\n\n" + "\n".join(processor.summary_lines) + "\n")
        if processor.report_path:
            sys.stdout.write(f"\nReport: {processor.report_path}")
        sys.stdout.write(f"\nLog: {processor.log_file}\n")

    if processor.declined:
        sys.stderr.write("Nothing was written.\n")
        return 3
    if processor.cancelled:
        sys.stderr.write("Stopped. Files already copied stay in the destination. Run the same "
                         "command again to continue; files already there are not copied "
                         "twice.\n")
        return 3
    if not ok:
        if processor.problem:
            sys.stderr.write(f"{processor.problem}\n")
        sys.stderr.write(f"The run did not finish. See the log: {processor.log_file}\n")
        return 1
    if dry_run and processor.manifest_path and not args.json:
        sys.stdout.write(
            "\nTo apply exactly this plan:\n  takeout-organizer run --source "
            f"{_quote(str(args.source))} --dest {_quote(str(args.dest))} "
            f"--plan {_quote(processor.manifest_path)}\n")
    # 1 also when the run finished but files failed to copy or the numbers
    # do not add up, so scripts and schedulers can tell.
    summary = processor.summary or {}
    problems = summary.get('failed', 0) > 0 or summary.get('balanced') is False
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "gui":
        try:
            from .gui import GooglePhotosOrganizerGUI
        except Exception as exc:  # tkinter missing / no display
            print(f"GUI unavailable: {exc}", file=sys.stderr)
            return 2
        app = GooglePhotosOrganizerGUI()
        if os.environ.get("TAKEOUT_ORGANIZER_SELFTEST") == "1":
            # Proves a build starts (Tk, the theme files) without showing a
            # window: build it hidden, quit after half a second, exit 0.
            app.root.withdraw()
            app.root.after(500, app.root.quit)
        app.run()
        return 0

    if args.command == "verify":
        from . import verify
        return verify.main(str(args.dest), as_json=args.json,
                           exiftool_path=str(args.exiftool) if args.exiftool else None)
    if args.command == "diagnose":
        from . import diagnostics
        return diagnostics.main(str(args.dest) if args.dest else _last_window_dest())
    if args.command == "undo":
        return _undo(args)
    if args.command == "check":
        return _check(args)
    if args.command == "plan":
        return _run(args, dry_run=True)
    if args.command == "run":
        return _run(args, dry_run=args.dry_run)

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
