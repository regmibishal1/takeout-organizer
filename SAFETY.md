# Safety

What the tool guarantees, how each guarantee is tested, and what it does not guarantee.
Every test named here runs in CI on Windows, macOS and Linux with the real ExifTool.

## Guarantees

### Your Takeout files are never changed

ZIP files are only read. An already-extracted export is copied to a temporary working
folder first, and ExifTool only ever writes to that copy. The working folder is deleted at
the end of every run, including previews and failed runs.

Tested by `test_already_extracted_source_is_never_modified` (tests/test_integration.py).

### Nothing in the destination is overwritten

A file whose name is taken gets a new name (`IMG_1234_1.jpg`). A file that is already there
with identical content is not copied again, so running the same command twice adds nothing.

Tested by `test_rerun_into_same_destination_copies_nothing_new` (tests/test_integration.py).

### Every file is accounted for

Each input file gets exactly one outcome: written, already in the destination, duplicate not
copied, other file (copied to `_other/`), or failed. The summary checks that these add up
to the number of input files and prints `MISMATCH` if they do not. Files that cannot be read
from a ZIP count as failed; they are never silently dropped. Shared albums downloaded as
ZIPs are kept. The input files are the photos, videos and other files of the export; the
`.json` sidecars are read for their metadata, and Takeout's own files
(`archive_browser.html`, album `metadata.json`) are not counted. When sidecars name photos
that are not in the export, the summary says how many, since a part is probably missing.

Tested by `test_every_input_file_is_accounted_for`, `test_shared_album_zip_photos_are_kept`
(tests/test_integration.py) and `test_unreadable_zip_entries_count_as_failed_files`
(tests/test_runs.py).

### A preview writes nothing to the destination

`plan` (or `run --dry-run`) creates nothing in the destination. Its log and reports go to a
preview folder in the temporary folder. A real run afterwards is not affected by it.

Tested by `test_dry_run_leaves_destination_and_temp_untouched` (tests/test_processor.py) and
`test_dry_run_then_real_run_writes_metadata` (tests/test_integration.py).

### A photo is never dropped because it only looks like another

Identical bytes are one photo. A visual match drops a copy only when shape, colors and
capture times also agree and at least one copy has no Google sidecar. Burst shots, retakes,
blank pages and single-color images are kept. Every dropped and flagged file is listed in
the reports.

Tested by tests/test_deduplicator.py, for example
`test_burst_frames_stay_apart_whichever_copy_comes_first` and
`test_flat_images_of_different_colors_are_all_kept`.

### Another photo's metadata is never written into a file

Sidecars are matched by exact naming rules, never by a similar name, and weaker matches
must be confirmed by the sidecar's `title`. Each sidecar is used by one photo.

Tested by every row of tests/fixtures/takeout_names.csv (tests/test_takeout_names.py),
including the case where `IMG_7.jpg` must not take `IMG_77.jpg.json`.

### A photo counted as written holds its metadata

After writing, the date, location, description, keywords, people and rating are read back
from the file. ExifTool can report success for a value it did not store; then the photo is
still copied, but it is counted under "without new metadata", with the reason in the run
log and the manifest. A value cut to fit an IPTC field's length limit still counts, since
the full text is in XMP.

Tested in tests/test_read_back.py, with real ExifTool for JPEG, PNG, GIF, WebP, TIFF and
MP4.

### Capture times do not depend on the computer's time zone

A camera date that is the same moment as Google's is kept. Otherwise Google's date is
written in the zone where the photo was taken, with its offset.

Tested by `test_camera_date_from_abroad_is_kept` (tests/test_integration.py), and CI runs
the whole suite a second time with the computer's zone set to Asia/Tokyo.

### A file is put in place whole

A finished file is moved into place with a rename when its working copy is on the same
drive, and copied under a temporary name and then renamed otherwise, so a file in the
destination is never half there. Neither ever replaces a file that is already there. Only
files in the run's own working copy are moved; your Takeout files are only read. A working
copy a killed run left in `.takeout-organizer/work` is deleted at the start of the next run.

Tested in tests/test_move_into_place.py.

### An interrupted run leaves no half-written file

Copies are written under a temporary name and renamed when complete; leftovers are removed
at the start of the next run. Ctrl+C stops after the current file. Running the same command
again continues without copying anything twice.

Tested by `test_copies_appear_complete_or_not_at_all`,
`test_only_leftover_partial_copies_are_removed` and `test_stopped_run_exits_3`
(tests/test_runs.py).

### Problems are found before anything is written

Missing ExifTool, a destination inside the Takeout folder, too little disk space, a ZIP
that would expand to more than 20 times its size or holds over a million files (a damaged
ZIP, or one built to fill the disk), and a second run into the same destination stop the
run before it writes anything, with exit code 2 and a message that says what to do.

Tested by `test_missing_exiftool_fails_before_creating_anything`,
`test_destination_inside_source_is_refused` (tests/test_processor.py),
`test_not_enough_space_stops_before_anything_is_written`,
`test_a_zip_that_expands_far_beyond_its_size_is_refused`,
`test_a_zip_with_too_many_entries_is_refused` and
`test_a_second_run_into_the_same_folder_is_refused` (tests/test_runs.py).

### Undo removes only what a run wrote

`takeout-organizer undo` deletes the files one run wrote, as recorded in the manifest, and
the JPG copies of HEIC photos it made, after showing how many and how large. A file whose size or modification time changed since the
run (for example a photo you edited) is kept and listed. Your Takeout files are not touched.

Tested by `test_undo_removes_exactly_what_the_run_wrote`,
`test_a_file_edited_since_the_run_is_kept` and `test_each_undo_removes_only_its_own_run`
(tests/test_undo.py).

## Not guaranteed

- **Dates that do not exist.** A file with no Google sidecar, no date inside it and no date
  in its name is filed by a guess (its upload date or file time) in `_undated/`. Check those.
  A date in the name is written into the file, without a time zone, since a name gives none.
- **Photos missing from the export.** A Takeout that stopped partway, or a part that was not
  downloaded, lacks those photos, and no tool can restore them. The summary counts sidecars
  that name a photo not in the folder; export again if it is not zero.
- **Later edits in Google Photos.** A description or date changed in Google Photos after a
  photo was copied is not applied to the copy already in your library.
- **Every duplicate removed.** Uncertain pairs are kept and listed under "Possible
  duplicates" in the report. Some duplicates may remain.
- **Metadata inside every format.** ExifTool cannot write BMP, AVI, MKV and some other
  formats; their metadata goes in an `.xmp` sidecar next to them instead. Files whose
  extension does not match their content are copied without new metadata unless you use
  `--fix-extensions`.
- **The local time zone of every photo.** Without the `[timezones]` extra, or without GPS,
  photos with no camera date use `--timezone` or this computer's zone.
- **Exact space estimates.** The space check uses the sizes each ZIP declares.
- **Photos others added to shared albums.** Takeout does not contain them. Download those
  albums from Google Photos and put the ZIPs in the same folder.

## Before a large run

1. Run `takeout-organizer check` and fix what it reports.
2. Run `takeout-organizer plan` and read the summary and the HTML report.
3. Keep your Takeout files until you have checked the result.
