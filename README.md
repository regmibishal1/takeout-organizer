# Takeout Organizer

Fix the dates and locations in your Google Photos Takeout. Free, offline, and your original
files are never changed.

When you export your library with Google Takeout, the photos come stripped of their
metadata: the real capture date and GPS coordinates are dumped into separate `.json`
sidecar files. Takeout Organizer matches each photo to its sidecar, writes the metadata
back into the file with [ExifTool](https://exiftool.org/), and organizes everything into
a clean structure, without ever modifying or deleting your originals.

## Why another one?

Other good tools exist. [GPTH Neo](https://github.com/Xentraxx/GooglePhotosTakeoutHelper_Neo),
the maintained fork of GooglePhotosTakeoutHelper, also writes EXIF with ExifTool, and if you
are moving only into Immich, [immich-go](https://github.com/simulot/immich-go) can import
your Takeout directly. Takeout Organizer aims at a portable library: plain folders where
every file carries its own metadata, readable by any app, with copy-only safety and an
account of every file. It:

- **Writes real EXIF** (DateTimeOriginal, GPS) with ExifTool, not just the file's timestamp
- **Never overwrites or deletes** source files; every operation is copy-based and collision-safe
- **Handles the awkward Takeout filename patterns**: edited variants (`photo-edited.jpg`),
  duplicate counters (`IMG_1(1).jpg`), the newer `.supplemental-metadata.json` names (also
  cut short), truncated sidecar names, live-photo pairs, and Unicode/case differences.
  Sidecars are matched by exact rules only, never by a similar name, and a weak match must
  be confirmed by the sidecar's `title`. Each sidecar is used by one photo (edited copies
  and Live Photo videos share). The rules are listed with a test for each case in
  [tests/fixtures/takeout_names.csv](tests/fixtures/takeout_names.csv)
- **De-duplicates by content, not just bytes**: a perceptual hash catches the same photo
  even when it was re-compressed (e.g. a Takeout copy and a downloaded shared-album copy),
  and keeps the best version (the one with a sidecar, then higher resolution)
- **Keeps photos with no sidecar** (like downloaded shared albums), dating them from the
  date already in the file or a date in the file name (which is then written into the file).
  A file with neither goes to `_undated/`
- **Keeps correct camera dates** and writes each photo's time in the zone where it was taken
- **Runs headless** (CLI, works over SSH / in containers) or with a desktop GUI
- **Safe to stop and run again**: files already copied are skipped, nothing is copied twice

## Before you export

1. Go to [takeout.google.com](https://takeout.google.com), click **Deselect all**, then select
   only **Google Photos**.
2. Choose **Send download link via email**, **Export once**, file type `.zip` and size
   **50 GB** (fewer parts, and large videos are not split).
3. Download every part within 7 days into one folder. Do not unzip them.
4. If you use shared albums, open each one in Google Photos, use **Download all**, and put
   those ZIPs in the same folder (a Takeout holds only the photos you added yourself).
5. Run `takeout-organizer check` on that folder, or open the app and choose it.

## Install

### Download (Windows and macOS)

The [Releases](https://github.com/regmibishal1/takeout-organizer/releases) page has ready-made
apps with ExifTool inside, so nothing else needs installing:

- **Windows**: download `Takeout-Organizer-...-windows-x64.zip`, unzip it anywhere, and open
  `Takeout Organizer.exe` in the folder. The app is not signed, so Windows SmartScreen may
  say it does not recognize it: choose **More info**, then **Run anyway**. The same folder
  has `takeout-organizer.exe` for the command line.
- **macOS** (Apple silicon): download `Takeout-Organizer-...-macos-arm64.zip`, unzip it, and
  move `Takeout Organizer.app` to Applications. The app is not notarized, so the first time
  right-click it, choose **Open**, then **Open** again. The command-line tool is
  `Takeout Organizer.app/Contents/MacOS/takeout-organizer`. The bundled ExifTool runs with
  the Perl that macOS includes; if a future macOS drops it, `brew install exiftool` is used
  instead.

Each download has a `.sha256` file to check it against, for example
`certutil -hashfile FILE SHA256` on Windows or `shasum -a 256 FILE` on macOS.

### With pipx (any system, including Linux)

Needs Python 3.10 or newer. The package is not on PyPI yet. Install it from GitHub with
[pipx](https://pipx.pypa.io/), so it gets its own environment and the command is on your PATH:

```bash
pipx install "git+https://github.com/regmibishal1/takeout-organizer"
```

Or download the wheel from the Releases page and run
`pipx install takeout_organizer-0.1.0-py3-none-any.whl`.

With the optional extras for time zones from GPS and iPhone (HEIC) duplicates:

```bash
pipx install "takeout-organizer[timezones,heic] @ git+https://github.com/regmibishal1/takeout-organizer"
```

Installed this way, ExifTool must be installed separately:

| OS | Command |
|----|---------|
| Windows | `winget install OliverBetz.ExifTool`, or download from [exiftool.org](https://exiftool.org/) and put `exiftool.exe` on your PATH |
| macOS | `brew install exiftool` |
| Debian/Ubuntu | `sudo apt install libimage-exiftool-perl` |

## Usage

```bash
# 1. Check the export, ExifTool and disk space (writes nothing)
takeout-organizer check --source ./Takeout --dest ./organized

# 2. Preview everything a run would do (writes nothing to the destination)
takeout-organizer plan --source ./Takeout --dest ./organized

# 3. Do it for real (on a terminal it shows the plan and asks first; --yes skips that)
takeout-organizer run --source ./Takeout --dest ./organized

# Changed your mind? Delete exactly the files the last run wrote (asks first)
takeout-organizer undo --dest ./organized

# Later: check the library is still as the runs left it (changes nothing)
takeout-organizer verify --dest ./organized

# Or launch the desktop GUI instead
takeout-organizer gui
```

Progress shows one line per phase with counts and the time left. Warnings and errors go to
stderr; the summary goes to stdout at the end, and the full log is always saved to a file.
`--quiet` shows only errors and the summary, `--verbose` also shows every step, and `--json`
prints the summary as JSON for scripts.

The desktop window walks through the same four steps: choose the two folders (each is
checked as you pick it, and they are remembered next time), preview the plan, organize, and
done. Organizing applies exactly the plan the preview showed. Stop asks first, and a stopped
run can be resumed from the done step, which also opens the report and the log and can undo
the run. Every button has an Alt shortcut (the underlined letter). It follows your system's
light or dark mode, with a switch in the header, and its display scaling (125 %, 150 %). With
Windows High Contrast on, it keeps the system's colors.

`--source` accepts either a folder of Takeout `.zip` files or an already-extracted export.

| Flag | Effect |
|------|--------|
| `--dry-run` | Preview every action; write nothing to the destination (same as `plan`) |
| `--keep-json` | Put each Takeout JSON sidecar next to its photo in the output |
| `--dedup MODE` | `off`, `exact` (byte-identical only), or `perceptual` (also catches re-compressed copies). Default: `perceptual` |
| `--exiftool PATH` | Point at a specific ExifTool binary |
| `--resume` | Continue the last stopped run into this destination, reusing its decisions and skipping the files it finished |
| `--plan FILE` | Apply a plan made by `plan` (the `manifest.db` it prints) exactly, as long as the export and settings are the same |
| `--yes` | Do not ask before writing |
| `--timezone ZONE` | Zone for photos whose location and camera give none, e.g. `+09:00` or `Asia/Tokyo`. Default: this computer's |
| `--date-policy POLICY` | `google-first` (default) or `camera-first`, for when the two dates are different moments |
| `--overwrite-gps` | Replace a location the camera recorded with Google's |
| `--people-as-keywords` | Also add people's names to the keywords |
| `--fix-extensions` | Rename files whose extension does not match their content (e.g. a `.heic` that is a JPEG) so they get metadata |
| `--work-dir PATH` | Folder for the temporary working copy (default: `.takeout-organizer/work` in the destination; a preview uses the system temp folder) |
| `--layout LAYOUT` | Folders by date: `year-month` (`2023/06`, default), `year-month-day` (`2023/06/15`), `year`, or `flat` |
| `--albums MODE` | `playlists` (default) writes one playlist per album to `Albums/`; `off` writes none |
| `--rename TEMPLATE` | Names that sort by date, e.g. `"{date:%Y%m%d_%H%M%S}_{name}"` (see [Optional extras](#optional-extras)) |
| `--heic-copies jpg` | Also a JPG copy beside each HEIC photo; the HEIC is kept (see [Optional extras](#optional-extras)) |
| `--place-names` | Add the town or city to each date folder, e.g. `2023/06 Tokyo` (see [Optional extras](#optional-extras)) |

## What the output looks like

```
organized/
  2023/06/IMG_1234.jpg          photos and videos, by the month they were taken
  2023/06/SCAN.bmp              a format ExifTool cannot write ...
  2023/06/SCAN.bmp.xmp          ... with its date and place in an XMP sidecar
  _undated/                     files whose date is only a guess (see below)
  _other/                       files from the export that are not photos or videos
  Albums/Paris trip.m3u8        one playlist per album, with relative paths
  Albums/albums.json            the same, for scripts
  takeout-organizer-report.html what the last run did, with the duplicate decisions
  .takeout-organizer/           the tool's own files:
    manifest.db                   every run's record of every file (SQLite)
    logs/run-...log, .md, .json   each run's full log, text report and numbers
    duplicates.md                 every duplicate decision of the last run
    work/                         the working copy, only while a run is going
```

A photo whose only date is a guess (the date it was uploaded to Google Photos, or the file's
own time) goes to `_undated/` instead of a date folder, so a wrong guess never hides it in
the wrong year. The summary says how many there are.

Albums are kept as playlists rather than copies, so a photo in three albums is stored once.
Album folders from Takeout and shared albums downloaded as ZIPs both count. XMP sidecars
(`name.ext.xmp`) are read by digiKam, darktable and Immich.

## Every file is accounted for

Nothing from the export is dropped. Photos and videos are recognized by their content as
well as their name, so formats with unusual extensions are kept too. Files that are not
photos or videos are copied to `_other/`, in the folders they came from. The `.json`
sidecars are read for their metadata rather than copied (use `--keep-json` to keep them),
and Takeout's own files (`archive_browser.html`, album `metadata.json`) are not counted.

Each run ends with a summary that accounts for every input file:

```
Input files                              8,311
  Photos and videos written              8,102
    date from Google sidecar             7,902
    date from photo EXIF                   151
    date from file name                     28
    date from upload date                    0
    date from file time                     21
    in _undated/ (date is a guess)          21
    metadata in an .xmp sidecar              3
    without new metadata                     5
  Already in the destination                 0
  Duplicates not copied                    176
  Other files (in _other/)                   9
  Failed to copy                            24
Total accounted                          8,311   all files accounted for
```

It is also saved in the run report. If the numbers ever fail to add up, the last line says
`MISMATCH`. When sidecars name photos that are not in the folder, a note says how many: a
part of the export is probably missing.

Each run also writes `takeout-organizer-report.html` next to your photos, replacing the
previous one (a preview run puts it in the preview folder). It works offline and shows the summary, what needs a look (files
that failed, possible duplicates side by side with the reason both were kept, files with a
guessed date), every duplicate that was removed next to the copy that was kept, photos per
year, albums, and the settings used. It is built to be usable with a screen reader and a
keyboard alone.

## Long runs

- **Before starting**, the tool checks that there is enough free space, that no part of a
  split Takeout (`-001.zip`, `-002.zip`, ...) is missing, that no ZIP is damaged, and that a
  FAT32 drive will not be asked to hold files over 4 GB.
- **Space.** The working copy is made next to the organized photos, in
  `.takeout-organizer/work` in the destination, and each finished file is moved from it into
  place. So a run needs free space on the destination drive for about one copy of the
  library, and none on the system drive. With `--work-dir` on another drive, that drive needs
  room for the working copy and the destination for the photos. If the destination is in a
  synced folder (OneDrive, Google Drive, Dropbox), the working copy would be synced too: use
  `--work-dir` on a folder outside it.
- **Ctrl+C** stops after the current file. Press it again to quit at once. Running the same
  command again continues: files already in the destination are not copied twice.
- Copies are written under a temporary name and renamed when complete, so an interrupted
  run never leaves a cut-off photo under its real name.
- **Takeout on a NAS or a USB disk.** If reading part of a ZIP fails (the network drops
  out, the disk goes to sleep), the tool copies that ZIP to the work folder once and reads
  the failed files from the copy, and the log says so. A file still unreadable, or damaged
  in the ZIP itself, is counted as failed in the summary.
- **Speed.** Writing metadata takes most of a run, so a run with many files writes it with
  four ExifTool processes at once, about four times as fast as one. ExifTool stays open for
  the whole run instead of starting for each file.
- Only one run at a time can write to a destination.
- The computer is kept from going to sleep while a run is going.

Exit codes: `0` done, `1` done but some files failed to copy, `2` a problem found before
starting (nothing was written), `3` stopped with Ctrl+C or by answering no.

Every run plans first and then carries out its plan, so `plan` shows exactly the folders and
names a run will use (it reads each file's own dates when ExifTool is installed). Each
file's progress is recorded in the manifest as it is copied.

Some Takeout files have an extension that does not match their content, such as `.heic`
files that are really JPEGs. ExifTool cannot write to those, so they are copied without new
metadata and counted. Run with `--fix-extensions` to give them the right extension in the
output, so they get their metadata too.

## Dates and time zones

Google stores each photo's capture time as a moment in UTC. A photo needs it as local time,
in the zone where it was taken:

- If the camera already recorded the same moment, its date is kept as it is, with its
  sub-seconds and offset. Only a missing offset is added.
- Otherwise Google's date is written (it includes date fixes you made in Google Photos), in
  the zone of the photo's GPS position, else the camera's offset, else `--timezone`, else
  this computer's zone. The offset is always written next to it (`OffsetTimeOriginal`).
- Videos get `Keys:CreationDate` with the zone, which Apple Photos reads first.
- A location the camera recorded is kept unless you pass `--overwrite-gps`.
- People's names go in the IPTC `PersonInImage` field, not the keywords, so photo apps
  do not show them as tags.
- Dates, captions and keywords are written the way the Metadata Working Group recommends,
  so every app reads the same values: in EXIF and in XMP (where the date carries its zone),
  and in IPTC only when the file already has an IPTC block, marked as UTF-8 so non-Latin
  text survives. No IPTC block is added to a file that has none.

Looking up the zone of a GPS position needs the optional `timezones` extra; see
[Install](#install).

## Merging shared albums (group photos)

A normal Takeout only exports *your* photos, not the ones other people added to a shared
album. To include those, add each shared album's photos to your library before exporting,
or download the album directly (its **Download all** button gives a `.zip`). Drop those
`.zip`s (or extracted folders) into the same `--source` folder alongside your Takeout. A ZIP
without Takeout folders is treated as an album download and kept under the ZIP's name.

Photos you contributed will then appear twice: once from Takeout (with a sidecar) and once
from the album (usually without one, and re-compressed so the bytes differ). Perceptual
de-duplication collapses each such pair to a single best copy, while genuinely different
group photos are all kept.

Looking alike is never enough on its own to drop a photo, because burst shots, exposure
brackets and retakes look alike too. A copy is only dropped when the shape, the colors and
the capture times also agree, and when at least one of the two has no Google sidecar (two
photos that both have sidecars are separate items in your Google Photos library). Blank or
single-color images are only matched when their bytes are identical. Uncertain matches are
kept and listed in the report with the reason, so you can review them.

iPhone photos (HEIC) are compared visually only with the optional `heic` extra installed
(see [Install](#install)). Without it they are still de-duplicated when their bytes are
identical.

After every run, `.takeout-organizer/duplicates.md` in the destination lists exactly what
was merged and what was flagged for review. A preview writes it, with the log and the
reports, to a preview folder in your temp directory instead and prints that path.

Running again into the same destination is safe: files already there with identical
content are skipped, not copied a second time. This also works with a newer Takeout of
the same account: files an earlier run already put there (and that are unchanged since)
are recognized from the manifest by their content, so only what is new is processed, and a
preview counts the rest as already there. A description or date changed in Google Photos
after a photo was copied is not applied to the copy already there.

## Optional extras

These are off unless you turn them on, with the flags below or under **advanced options**
in the window.

- **Names that sort by date.** `--rename "{date:%Y%m%d_%H%M%S}_{name}"` names
  `IMG_1234.jpg` taken on 15 June 2023 at 14:30:45 `20230615_143045_IMG_1234.jpg`, so any
  file browser lists photos in the order they were taken. `{date}` takes any
  [strftime](https://strftime.org/) format, and `{name}` is the original name; the extension
  is kept. A Live Photo's video and an edited copy are named by their photo's date, so they
  stay together. A file whose date is only a guess keeps its own name in `_undated/`.
- **JPG copies of HEIC photos.** `--heic-copies jpg` puts a JPG beside each iPhone HEIC
  photo, for devices and apps that cannot show HEIC. The HEIC is always kept. The copy is
  upright, has the same date, place and other metadata, and keeps the color profile; it is
  checked by `verify` and removed by `undo` like the files a run writes. Copies can be added
  to an existing library by running again with the flag. Installed with pipx, this needs the
  `heic` extra; the downloadable apps have it.
- **Place names in folder names.** `--place-names` adds the town or city each photo was
  taken in to its date folder: `2023/06 Tokyo`, `2023/06 Kyoto`. A month spent in two cities
  becomes two folders, and photos without a location stay in `2023/06`. The place is the
  nearest town or city with over 15,000 people within 30 km of where the photo was taken
  (the camera's location if it recorded one, else Google's), preferring a city over the
  district it contains, so a photo by Tokyo Tower goes in `Tokyo`, not `Minato City`. It is
  looked up offline, from data that ships with the tool; nothing is sent anywhere. Files
  already in the library stay where they are when you turn it on later. It needs date
  folders, so it does not work with `--layout flat`. Place names from
  [GeoNames](https://www.geonames.org/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Checking a library later

`takeout-organizer verify --dest ./organized` reads the manifest and checks every file the
runs put there: is it still there, unchanged since it was written (same size and
modification time), and does a photo or video that was given a date still hold one? It
changes nothing and exits with `1` when something is off, so it can run on a schedule, for
example on a NAS, or before you delete your Takeout files. `--json` lists every file for
scripts.

## Languages

The window is in English, German, French and Spanish. It follows your system's language;
choose another in the list at the top of the window (before you start a preview), and the
window remembers it. Logs, reports, the command line, `diagnose` and some error messages
stay in English on purpose, so a bug report reads the same for everyone.

The German, French and Spanish texts have not been reviewed by native speakers yet.
Corrections are welcome: the texts are in
[`src/takeout_organizer/locale`](src/takeout_organizer/locale), one standard gettext `.po`
file per language, which any text editor or [Poedit](https://poedit.net/) can open. To add a
language, copy `messages.pot` to `<code>.po` (for example `it.po`), fill in each `msgstr`,
and set the `X-Language-Name` header to the language's own name. Keep the `{placeholders}`
as they are, and in button labels put `&` before the letter for the Alt shortcut. After
changing text in the window, `python packaging/update_translations.py` brings every file up
to date, and the tests check that each one is complete.

## Reporting a problem

Open an issue with the output of:

```bash
takeout-organizer diagnose --dest ./organized
```

or use **Copy diagnostics** on the last step of the window. It lists the versions of the
tool, Python, Tk and ExifTool, your operating system, and the last run's summary and
warnings, with your folder paths left out. Check it before you post it. Each run also saves
its numbers in `.takeout-organizer/logs/run-....json`; nothing is ever sent anywhere.

## Privacy

Nothing leaves your computer: the tool makes no network connections (the Help button in
the window opens this page in your browser). The report and the logs
contain file names, folder paths and the names of people tagged in your photos, so look
before you share them, for example in a bug report.

## Safety

The tool is designed so a failed or interrupted run can never cost you data. Sources are
read-only, existing files are never overwritten, and `--dry-run` shows exactly what will
happen first. See [SAFETY.md](SAFETY.md) for the full guarantees.

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

The window tests are skipped unless `TAKEOUT_GUI_TESTS=1` is set, so a plain `pytest` never
opens windows on your desktop. CI runs them. On Linux they can run under a virtual display
without touching yours: `TAKEOUT_GUI_TESTS=1 xvfb-run -a pytest`.

## License

MIT, see [LICENSE](LICENSE). The standalone apps also include ExifTool, Python, Tcl/Tk and
the libraries listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), each under its own
license.
