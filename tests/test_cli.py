"""CLI exit codes and messages, called through main(argv)."""

from __future__ import annotations

import io
import json

from takeout_organizer.cli import Display, main
from takeout_organizer.metadata_handler import MetadataHandler


def test_missing_source_exits_2(tmp_path, capsys):
    code = main(["run", "--source", str(tmp_path / "nope"), "--dest", str(tmp_path / "out")])
    assert code == 2
    assert "does not exist" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_destination_inside_source_exits_2(takeout_dir, capsys):
    code = main(["run", "--source", str(takeout_dir), "--dest", str(takeout_dir / "out")])
    assert code == 2
    assert "Choose a different folder." in capsys.readouterr().err
    assert not (takeout_dir / "out").exists()


def test_missing_exiftool_exits_2_with_install_hint(takeout_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MetadataHandler, "_find_exiftool", lambda self: None)
    code = main(["run", "--source", str(takeout_dir), "--dest", str(tmp_path / "out")])
    assert code == 2
    err = capsys.readouterr().err
    assert "ExifTool not found" in err and "winget install OliverBetz.ExifTool" in err
    assert not (tmp_path / "out").exists()


def test_unknown_timezone_exits_2(takeout_dir, tmp_path, capsys):
    code = main(["run", "--source", str(takeout_dir), "--dest", str(tmp_path / "out"),
                 "--dry-run", "--timezone", "Mars/Olympus"])
    assert code == 2
    assert "Unknown time zone: Mars/Olympus" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_dry_run_exits_0_and_old_flag_still_accepted(takeout_dir, tmp_path, capsys):
    code = main([
        "run", "--source", str(takeout_dir), "--dest", str(tmp_path / "out"),
        "--dry-run", "--preserve-originals",
    ])
    assert code == 0
    assert "--preserve-originals no longer does anything" in capsys.readouterr().err


def test_check_reports_and_writes_nothing(takeout_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(MetadataHandler, "_find_exiftool", lambda self: None)
    code = main(["check", "--source", str(takeout_dir), "--dest", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 2  # ExifTool is missing
    assert "Source" in out and "an extracted export" in out
    assert "ExifTool  not found" in out
    assert "1 problem found. Fix it, then run:" in out
    assert "takeout-organizer plan --source" in out
    assert not (tmp_path / "out").exists()


def test_check_passes_with_exiftool(takeout_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(MetadataHandler, "_find_exiftool", lambda self: "exiftool")
    monkeypatch.setattr(MetadataHandler, "get_exiftool_version", lambda self: "13.40")
    code = main(["check", "--source", str(takeout_dir), "--dest", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ExifTool  13.40 at exiftool" in out and "OK" in out
    assert "No problems found. Next, preview the plan:" in out


def test_plan_prints_the_plan_summary(takeout_dir, tmp_path, capsys):
    code = main(["plan", "--source", str(takeout_dir), "--dest", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 0
    assert "Plan (nothing was written)" in captured.out
    assert "Photos and videos to be written" in captured.out
    assert "all files accounted for" in captured.out
    assert not (tmp_path / "out").exists()
    # Log lines no longer flood the console; the log file has them.
    assert "--- Phase" not in captured.out + captured.err


def test_json_summary_on_stdout(takeout_dir, tmp_path, capsys):
    code = main(["plan", "--source", str(takeout_dir), "--dest", str(tmp_path / "out"),
                 "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["dry_run"] is True and result["summary"]["balanced"] is True
    assert result["log"].endswith(".log")


def test_progress_is_one_redrawn_line_on_a_terminal():
    stream = _Stream(tty=True)
    display = Display(stream)
    for i in range(1, 4):
        display.progress(i, 3, "Copying", f"IMG_{i}.jpg")
    display.message("IMG_2.jpg has no date", "warning")
    display.finish()
    text = stream.getvalue()
    assert text.count("\r") == 3 and "Copying" in text
    assert "\nWarning: IMG_2.jpg has no date\n" in text  # never mixed into the progress line


def test_progress_without_a_terminal_is_plain_lines(monkeypatch):
    stream = _Stream(tty=False)
    display = Display(stream)
    for i in range(1, 101):
        display.progress(i, 100, "Writing metadata", f"IMG_{i}.jpg")
    lines = stream.getvalue().splitlines()
    assert "\r" not in stream.getvalue()
    assert 10 <= len(lines) <= 25  # every 5% or 5 s, not one per file
    assert lines[-1].startswith("Writing metadata") and "100/100" in lines[-1]


def test_quiet_hides_warnings_but_not_errors():
    stream = _Stream(tty=False)
    display = Display(stream, quiet=True)
    display.progress(1, 2, "Copying", "a.jpg")
    display.message("minor", "warning")
    display.message("broken", "error")
    assert stream.getvalue() == "Error: broken\n"


class _Stream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty
