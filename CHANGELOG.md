# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Changes to how sidecars are matched are always
listed, since they change which date a photo gets.

## [0.2.0] - 2026-10-11

### Changed

- The problems the window shows are in its language too: a folder that cannot be used, the
  checks before a run (space, missing or damaged ZIP parts, FAT32), an unknown time zone, a
  name template that cannot work, another run using the folder, a plan or stopped run that
  no longer fits, and nothing to undo. The command line and the logs keep the English.

### Fixed

- The window at 125 % or 150 % display scaling: spacing, text widths, table columns and the
  window size now grow with the text, the text in buttons, fields and choices is no longer
  left small beside larger labels, and the window always fits on the screen, also when the
  size remembered from a larger screen does not.
- With Windows High Contrast on, the window keeps the system's colors instead of its own
  light or dark theme, and the theme button is hidden.

## [0.1.0] - 2026-10-10

First public release. Takeout Organizer puts the dates, places and captions from a Google
Photos Takeout back into the photos themselves and files them in folders by date, offline,
without ever changing the Takeout files.

### Metadata written into each photo and video

- Capture dates from Google's sidecar, else the camera's own date, else a date in the file
  name; a photo with only a guessed date (upload date or file time) goes to `_undated/` so it
  never hides in a wrong year. `--date-policy` decides when Google's and the camera's dates
  are different moments.
- Times in the zone where each photo was taken, from its GPS position (the `[timezones]`
  extra) or the camera's offset, else `--timezone` or this computer's zone, with
  `OffsetTimeOriginal`.
- Location, description and keywords through ExifTool's MWG tags (EXIF and XMP, and IPTC only
  when the file already has it, marked UTF-8); people's names in
  `XMP-iptcExt:PersonInImage` (`--people-as-keywords` also adds them to the keywords). A
  location the camera recorded is kept unless `--overwrite-gps`.
- Every write is read back: a file whose values did not land is counted under "without new
  metadata", with the reason.
- Formats ExifTool cannot write (BMP, AVI, MKV, ...) get a `name.ext.xmp` sidecar, which
  digiKam, darktable and Immich read. `--fix-extensions` renames files whose extension does
  not match their content (a `.heic` that is a JPEG) so they get their metadata too.

### Finding each photo's sidecar

- Old and new sidecar names (`.json`, `.supplemental-metadata.json` and their truncated
  forms), each match confirmed by the sidecar's `title`, one photo per sidecar and no
  prefix matching (`IMG_7.jpg` never takes `IMG_77.jpg`'s date). A sidecar in another folder
  is found by its title. Every naming case has a test in `tests/fixtures/takeout_names.csv`.
- Edited copies share their photo's sidecar. A Live Photo's or motion photo's video shares
  its photo's sidecar, goes in its photo's folder and, with `--rename`, is named by its
  photo's date, also when neither has a sidecar.
- Shared albums downloaded as ZIPs ("Download all") are read as well as Takeout.

### The library

- Folders by date (`--layout`: `2023/06` by default, `2023/06/15`, `2023` or one folder),
  `_undated/`, `_other/` for files that are not photos or videos, and one playlist per album
  in `Albums/` with `albums.json`.
- Duplicates are found by content: exact copies, and with the default perceptual mode also
  re-compressed copies of the same photo, with guards so bursts, retakes and plain images are
  kept. Possible duplicates are kept and listed for a look.
- Optional extras: `--rename` for names that sort by date
  (`20230615_143045_IMG_1234.jpg`), `--heic-copies jpg` for a JPG beside each HEIC photo, and
  `--place-names` for the town or city on each date folder (`2023/06 Tokyo`), looked up
  offline from GeoNames data (CC BY 4.0).
- A newer Takeout of the same account adds only what is new: files already in the library
  are recognized by where they came from or by their content.

### Safe, checkable and repeatable

- Every run plans first and then applies its plan, so a preview (`plan`, or `run --dry-run`)
  shows exactly the folders and names a run uses, and nothing is written before you agree.
- A summary accounts for every input file (written, already there, duplicate, other or
  failed), and an HTML report shows the duplicate decisions with thumbnails. Each run keeps
  its log, a text report and `summary.json` in `.takeout-organizer/logs/`.
- A manifest (`.takeout-organizer/manifest.db`, SQLite) records every run and every file.
  With it, `run --resume` continues a stopped run, `run --plan FILE` applies a saved preview
  exactly, `undo` deletes exactly the files a run wrote (keeping any edited since), and
  `verify` checks later that every file is still there, unchanged and dated.
- Takeout files are only read. Finished files are moved into place from a working copy in
  the destination, never over another file and never half-written. One run at a time per
  destination; the computer is kept awake.
- Checks before a run: missing or damaged ZIP parts, free space, FAT32 drives and their 4 GB
  limit, and ZIPs that would expand to far more than their size. On a NAS or a USB disk,
  files that could not be read are read again from a local copy of their ZIP.

### Fast on big exports

- ExifTool stays open for a whole run, and big runs write metadata with four ExifTool
  processes at once, about four times as fast as one. Looking for duplicates reads four files
  at a time.

### The window

- Four steps: choose folders, preview, organize, done. Folders are checked as they are
  chosen, the preview shows the real plan, organizing lists each phase with the time left
  and can be stopped and resumed, and the last step opens the report or the log, copies
  diagnostics for a bug report and can undo the run.
- Light and dark themes, an Alt shortcut on every button, names for screen readers on
  Tk 9.1, settings remembered between launches.
- English, German, French and Spanish, following the system's language. The translations
  are not yet reviewed by native speakers; corrections are welcome.

### The command line

- `check`, `plan`, `run`, `undo`, `verify`, `diagnose` and `gui`, with `--quiet`,
  `--verbose` and `--json`, and exit codes for scripts: 0 done, 1 some files failed, 2 a
  problem found before starting, 3 stopped.
- `diagnose` prints what a bug report needs, with folder paths left out.

### Install

- Ready-made apps for Windows and macOS (Apple silicon) with ExifTool inside, built and
  tested on GitHub for each release, with `.sha256` files and every bundled license.
- With pipx on any system (Python 3.10 or newer and ExifTool), with the `[timezones]` and
  `[heic]` extras.
