"""Integration test: the full processor pipeline in dry-run mode.

Dry-run needs no exiftool and writes nothing, so it exercises extract ->
match -> process -> organize end-to-end against a synthetic export.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from takeout_organizer.main_processor import MainProcessor, SetupError
from takeout_organizer.metadata_handler import MetadataHandler


def test_dry_run_pipeline_on_extracted_export(takeout_dir, tmp_path):
    dest = tmp_path / "out"
    logs: list[tuple[str, str]] = []

    processor = MainProcessor(
        source_folder=str(takeout_dir),
        dest_folder=str(dest),
        dry_run=True,
        log_callback=lambda msg, lvl: logs.append((lvl, msg)),
    )
    ok = processor.run()

    assert ok is True
    # Dry run must not create output files.
    assert not any(dest.rglob("*.jpg")) if dest.exists() else True


def test_dry_run_leaves_destination_and_temp_untouched(takeout_dir, tmp_path):
    dest = tmp_path / "out"
    processor = MainProcessor(
        source_folder=str(takeout_dir), dest_folder=str(dest), dry_run=True,
    )
    assert processor.run() is True

    # No folders, log, report or session file in dest; the working folder is gone.
    assert not dest.exists()
    assert processor.temp_dir and not os.path.exists(processor.temp_dir)
    assert any((Path(processor.output_dir) / ".takeout-organizer" / "logs").glob("run-*.log"))


def test_dry_run_plans_folders_from_sidecar_dates(takeout_dir, tmp_path):
    processor = MainProcessor(
        source_folder=str(takeout_dir), dest_folder=str(tmp_path / "out"), dry_run=True,
        dedup_mode="off",  # the synthetic files share bytes
    )
    assert processor.run() is True
    photo = next(p for p in processor.planned_dates if p.endswith("photo.jpg"))
    # conftest sidecars hold 2021-01-01 00:00 UTC; local time is within a day.
    assert processor.planned_dates[photo].year in (2020, 2021)


def test_destination_inside_source_is_refused(takeout_dir, tmp_path):
    for dest in (takeout_dir / "organized", takeout_dir):
        with pytest.raises(SetupError, match="destination is inside the Takeout folder"):
            MainProcessor(source_folder=str(takeout_dir), dest_folder=str(dest))
        assert not (takeout_dir / "organized").exists()


def test_source_inside_destination_is_refused(takeout_dir):
    with pytest.raises(SetupError, match="Takeout folder is inside the destination"):
        MainProcessor(source_folder=str(takeout_dir), dest_folder=str(takeout_dir.parent))


def test_missing_exiftool_fails_before_creating_anything(takeout_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(MetadataHandler, "_find_exiftool", lambda self: None)
    dest = tmp_path / "out"
    with pytest.raises(SetupError, match="ExifTool not found"):
        MainProcessor(source_folder=str(takeout_dir), dest_folder=str(dest))
    assert not dest.exists()


def test_cancel_stops_the_run(takeout_dir, tmp_path):
    cancel = threading.Event()
    cancel.set()
    processor = MainProcessor(
        source_folder=str(takeout_dir), dest_folder=str(tmp_path / "out"),
        dry_run=True, cancel_event=cancel,
    )
    assert processor.run() is False
    assert processor.cancelled is True


def test_runs_in_one_process_do_not_stack_log_handlers(takeout_dir, tmp_path):
    for i in range(2):
        MainProcessor(
            source_folder=str(takeout_dir), dest_folder=str(tmp_path / f"out{i}"), dry_run=True,
        ).run()
    # Each run closes its handlers; a later run must not log every line twice.
    assert logging.getLogger("GooglePhotosOrganizer").handlers == []


def test_dry_run_needs_no_exiftool(takeout_dir, tmp_path, monkeypatch):
    # Even with nothing on PATH, a dry run should construct and run.
    monkeypatch.setenv("PATH", "")
    processor = MainProcessor(
        source_folder=str(takeout_dir),
        dest_folder=str(tmp_path / "out"),
        dry_run=True,
    )
    assert processor.run() is True
