# Security

## Reporting a problem

Please report security problems privately, not in a public issue: open the repository's
**Security** tab on GitHub and choose **Report a vulnerability**.

Useful to include: what you did, what happened, the version (`takeout-organizer --version`),
your operating system and ExifTool version, and, if you can, a small Takeout file that shows
it. Remove anything personal from shared files first.

## What the tool does with your data

- It reads your Takeout files and never changes or deletes them.
- It makes no network connections. Nothing leaves your computer.
- It writes only to the destination folder you choose (its working copy is in
  `.takeout-organizer/work` there and is removed at the end of each run, or in the folder
  you give with `--work-dir`), a preview folder in the temporary folder for a preview, and,
  for the desktop window, a settings file with the last folders and choices
  (`takeout-organizer/settings.json` in `%APPDATA%`, `~/Library/Application Support` or
  `~/.config`).
- Logs and reports contain file names, folder paths and the names of people tagged in your
  photos. Check them before you share them. `takeout-organizer diagnose` prints what a bug
  report needs with folder paths left out; it still shows file names.

## The standalone apps

The Windows and macOS apps on the Releases page are built from the tagged source by the
`Release builds` workflow in this repository, on GitHub's runners. They are not signed or
notarized, which costs money this free project does not spend; each download has a
`.sha256` file to check it against. The ExifTool inside is a pinned version, downloaded by
the workflow and checked against the SHA-256 that exiftool.org publishes before it is
bundled. HEIC photos are read with pi-heif, which only decodes.

## ZIP files from other people

Shared albums and other people's ZIP files are read the same way as your own Takeout.
Python's ZIP reader removes `..` and drive or root prefixes from entry names, so nothing is
extracted outside the working folder. Before a run, a ZIP that would expand to more than
20 times its size (above 1 GB) or holds over a million files stops the run, named, and the
space check uses the sizes each ZIP declares. Python's ZIP reader never writes more than an
entry's declared size, so a ZIP cannot fill the disk past what the space check allowed.
