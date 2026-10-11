"""The standalone builds: the bundled ExifTool is found and preferred, the
license notices cover what is shipped, and no HEIC encoder is pulled in."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from takeout_organizer import metadata_handler
from takeout_organizer.metadata_handler import MetadataHandler, bundled_exiftool

ROOT = Path(__file__).resolve().parent.parent


def _fake_build(tmp_path, monkeypatch, name: str) -> Path:
    """A frozen app whose _internal folder holds exiftool/<name>."""
    internal = tmp_path / "Takeout Organizer" / "_internal"
    exiftool = internal / "exiftool" / name
    exiftool.parent.mkdir(parents=True)
    exiftool.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Takeout Organizer" / "app.exe"))
    return exiftool


def test_no_bundled_exiftool_outside_a_build():
    assert not getattr(sys, "frozen", False)
    assert bundled_exiftool() is None


def test_a_build_uses_the_exiftool_it_ships(tmp_path, monkeypatch):
    for name in ("exiftool.exe", "exiftool"):   # Windows, macOS
        exiftool = _fake_build(tmp_path / name, monkeypatch, name)
        assert bundled_exiftool() == str(exiftool)
        # Preferred over one on the PATH.
        monkeypatch.setattr(metadata_handler.shutil, "which", lambda n: "/usr/bin/exiftool")
        assert MetadataHandler(require=False).exiftool_path == str(exiftool)


def test_the_notices_cover_what_the_builds_ship():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for component in ("ExifTool", "Strawberry Perl", "Python", "Tcl/Tk", "Pillow", "sv-ttk",
                      "pi-heif", "libheif", "tzfpy", "OpenStreetMap", "tzdata", "PyInstaller",
                      "GeoNames", "CC BY 4.0"):
        assert component in notices, component
    spec = (ROOT / "packaging" / "takeout-organizer.spec").read_text(encoding="utf-8")
    for package in ("sv_ttk", "tzfpy", "tzdata", "pi_heif"):
        assert f"'{package}'" in spec, package


def test_heic_support_never_pulls_in_an_encoder():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    heic_line = next(line for line in pyproject.splitlines() if line.startswith("heic = "))
    assert "pi-heif" in heic_line and "pillow-heif" not in heic_line
    spec = (ROOT / "packaging" / "takeout-organizer.spec").read_text(encoding="utf-8")
    assert "excludes=['pillow_heif'" in spec


def test_the_release_workflow_checks_what_it_downloads():
    workflow = (ROOT / ".github" / "workflows" / "release-builds.yml").read_text(encoding="utf-8")
    assert "sha256sum -c -" in workflow and "shasum -a 256 -c -" in workflow
    assert workflow.count("EXIFTOOL_WINDOWS_SHA256") >= 2
    assert "smoke_test.py" in workflow and "TAKEOUT_ORGANIZER_SELFTEST" in workflow
    assert os.path.isfile(ROOT / "packaging" / "smoke_test.py")


def test_without_perl_the_macos_build_falls_back_to_another_exiftool(tmp_path, monkeypatch):
    _fake_build(tmp_path, monkeypatch, "exiftool")   # the Perl script, as on macOS
    monkeypatch.setattr(metadata_handler.os.path, "exists",
                        lambda path: False if path == "/usr/bin/perl" else os.path.isfile(path))
    monkeypatch.setattr(metadata_handler.shutil, "which",
                        lambda name: None if name == "perl" else "/opt/homebrew/bin/exiftool")
    assert bundled_exiftool() is None
    assert MetadataHandler(require=False).exiftool_path == "exiftool"


def test_the_window_app_survives_having_no_console():
    # A windowed build has sys.stdout and sys.stderr set to None; they must be
    # replaced before the tool is imported (its CLI keeps a reference to stderr).
    text = (ROOT / "packaging" / "gui_main.py").read_text(encoding="utf-8")
    assert text.index("open(os.devnull") < text.index("from takeout_organizer")


def test_screen_scale_and_contrast_checks_are_safe_anywhere():
    from takeout_organizer.gui_support import high_contrast, scale_factor

    class Root:
        def __init__(self, dpi):
            self.dpi = dpi

        def winfo_fpixels(self, distance):
            return self.dpi
    assert isinstance(high_contrast(), bool)
    if sys.platform == "darwin":
        assert scale_factor(Root(72)) == 1.0
    else:
        assert scale_factor(Root(96)) == 1.0 and scale_factor(Root(100)) == 1.0
        assert scale_factor(Root(144)) == 1.5 and scale_factor(Root(72)) == 1.0
