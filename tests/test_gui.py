"""The desktop window: settings, folder checks, and the four steps end to end.

The window tests run only with TAKEOUT_GUI_TESTS=1, so a plain pytest never
opens windows on your desktop. CI sets it; on Linux they can also run under a
virtual display: TAKEOUT_GUI_TESTS=1 xvfb-run -a pytest
"""

from __future__ import annotations

import gc
import io
import json
import os
import shutil
import sys
import time
import zipfile

import pytest
from PIL import Image

from takeout_organizer import gui_support
from takeout_organizer.gui_support import Settings, check_dest, check_source

tk = pytest.importorskip("tkinter")


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG")
    return buf.getvalue()


def _export(tmp_path):
    src = tmp_path / "Takeout"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        for i in range(3):
            name = f"IMG_{i}.jpg"
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}", _jpeg((80 * i, 9, 9)))
            zf.writestr(f"Takeout/Google Photos/Photos from 2023/{name}.json", json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": "1686839445"}}))
        zf.writestr("Takeout/Google Photos/Photos from 2023/IMG_0(1).jpg", _jpeg((0, 9, 9)))
    return src


# --- without a display ------------------------------------------------------

def test_settings_are_remembered(tmp_path):
    path = tmp_path / "cfg" / "settings.json"
    settings = Settings.load(str(path))
    assert settings.layout == "year-month" and settings.theme == "system"
    settings.source, settings.theme = "C:/Takeout", "dark"
    settings.save()
    again = Settings.load(str(path))
    assert again.source == "C:/Takeout" and again.theme == "dark"


def test_a_damaged_settings_file_is_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert Settings.load(str(path)).layout == "year-month"


def test_system_theme_is_light_or_dark():
    assert gui_support.system_theme() in ("light", "dark")


def test_folder_checks_say_what_to_fix(tmp_path):
    check, _ = check_source("")
    assert not check.ok and "Choose the folder" in check.message
    check, _ = check_source(str(tmp_path / "missing"))
    assert not check.ok and check.message == "This folder does not exist."
    (tmp_path / "empty").mkdir()
    check, _ = check_source(str(tmp_path / "empty"))
    assert not check.ok and "No Takeout files" in check.message

    src = _export(tmp_path)
    check, warnings = check_source(str(src))
    assert check.ok and check.message.startswith("1 ZIP file found,") and not warnings
    inside = check_dest(str(src), str(src / "Organized"))
    assert not inside.ok and "inside the Takeout folder" in inside.message
    fine = check_dest(str(src), str(tmp_path / "Organized"))
    assert fine.ok and fine.message.endswith("free.")


# --- the window -------------------------------------------------------------

def _clear(root) -> None:
    """Take the last test's window out of the shared root: its timers and
    widgets go, and the root is hidden again."""
    for after_id in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(after_id)
    for child in root.winfo_children():
        child.destroy()
    root.withdraw()


@pytest.fixture(scope="session")
def tk_root():
    """One Tk root for the whole test run, as the app has one for its life.

    Creating and deleting a Tk interpreter for every test made Tk 8.6 hang on
    macOS and sometimes fail to read sv-ttk's theme on Windows; one window run
    on its own works on both (checked on a macOS runner with Tk 8.6.16).

    Window tests run only when asked for (TAKEOUT_GUI_TESTS=1), so a plain
    pytest never opens windows on the desktop of whoever runs it. CI asks for
    them, and locally they run under a virtual display (xvfb-run).
    """
    if os.environ.get("TAKEOUT_GUI_TESTS") != "1":
        pytest.skip("window tests open windows; set TAKEOUT_GUI_TESTS=1 to run them")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    root = tk.Tk()
    root.withdraw()
    # Some tests put the window on screen (focus, shortcuts). It must never
    # appear on the desktop of whoever runs the tests: fully transparent, no
    # title bar or taskbar button.
    root.attributes("-alpha", 0.0)
    root.overrideredirect(True)
    yield root
    _clear(root)
    root.destroy()
    gc.collect()


@pytest.fixture
def app(tk_root, tmp_path):
    from takeout_organizer.gui import GooglePhotosOrganizerGUI
    # Tk objects may only be freed on the main thread. The worker thread
    # allocates the most, so the cycle collector would often run there and
    # free Tk objects left from an earlier test: Tcl then aborts (Windows) or
    # blocks for good (macOS). So it is off while a window exists, and the
    # window is collected here, on the main thread.
    gc.collect()
    gc.disable()
    try:
        window = GooglePhotosOrganizerGUI(tk_root, settings_path=str(tmp_path / "settings.json"))
        yield window
        if window.worker is not None:
            window.worker.join(timeout=60)
        _clear(tk_root)
        del window
        gc.collect()
    finally:
        gc.enable()


def _pump(app, until, seconds=60):
    end = time.time() + seconds
    while time.time() < end and not until():
        app.root.update()
        time.sleep(0.02)
    assert until(), "timed out"


def test_problems_show_under_the_field_and_focus_moves_there(app, tmp_path):
    app.root.deiconify()  # Tk records focus only for a window on screen
    app.root.update()
    app.source.set(str(tmp_path / "missing"))
    app.dest.set(str(tmp_path / "out"))
    app._primary()
    app.root.update()
    assert app.step == 1
    assert app.source_status.cget("text") == "Problem: This folder does not exist."
    # The field the window gives focus to. focus_get() would also need the
    # window to be in front, which a busy desktop or a test run in the
    # background does not allow.
    assert app.root.focus_lastfor() == app.source_entry


def test_steps_are_labelled_in_words(app):
    texts = [label.cget("text") for label in app.step_labels]
    assert texts == ["1  Choose folders", "2  Preview", "3  Organize", "4  Done"]
    app.show_step(3)
    texts = [label.cget("text") for label in app.step_labels]
    assert texts[:2] == ["1  Choose folders (done)", "2  Preview (done)"]
    assert app.primary.cget("text") == "Stop"


def test_theme_switch_keeps_text_readable(app):
    before = app.theme
    app._toggle_theme()
    assert app.theme != before and app.settings.theme == app.theme
    assert app.theme_button.cget("text") == ("Dark mode" if app.theme == "light" else "Light mode")
    background = str(app.done_summary.cget("background"))
    foreground = str(app.done_summary.cget("foreground"))
    assert background != foreground


def test_preview_then_organize(app, tmp_path):
    if shutil.which("exiftool") is None:
        pytest.skip("ExifTool not installed")
    src, dest = _export(tmp_path), tmp_path / "Organized"
    app.source.set(str(src))
    app.dest.set(str(dest))

    app._primary()  # Preview plan
    _pump(app, lambda: app.preview is not None)
    assert app.step == 2 and not dest.exists()  # a preview writes nothing
    assert app.tile_values["Photos and videos"].cget("text") == "4"
    assert app.tile_values["Duplicates to skip"].cget("text") == "1"
    assert app.primary.cget("text") == "Organize 3 files"
    assert [app.years.item(i, "text") for i in app.years.get_children()] == ["2023"]

    app._primary()  # Organize 3 files
    _pump(app, lambda: app.step == 4)
    assert app.done_banner.cget("text") == "Done. All 4 files are accounted for."
    assert len(list((dest / "2023" / "06").glob("*.jpg"))) == 3
    assert app.processor.previous_run == app.preview.run_id  # the previewed plan was applied

    remembered = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert remembered["source"] == str(src) and remembered["dest"] == str(dest)


def _organize(app, src, dest):
    app.source.set(str(src))
    app.dest.set(str(dest))
    app._primary()
    _pump(app, lambda: app.preview is not None)
    app._primary()
    _pump(app, lambda: app.step == 4)


needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None,
                                    reason="ExifTool not installed")


@needs_exiftool
def test_stop_asks_then_resume_finishes(app, tmp_path):
    src, dest = _export(tmp_path), tmp_path / "Organized"
    app.source.set(str(src))
    app.dest.set(str(dest))
    app._primary()
    _pump(app, lambda: app.preview is not None)

    app._ask = lambda *args: False
    app._primary()        # Organize
    app._stop()           # declined: keeps going
    assert not app.cancel_event.is_set()
    app._ask = lambda *args: True
    app._stop()
    _pump(app, lambda: app.step == 4)
    assert app.done_banner.cget("text").startswith("Stopped.")
    assert app.primary.cget("text") == "Resume"

    app._primary()        # Resume
    _pump(app, lambda: app.step == 4 and not app.processor.cancelled)
    assert app.done_banner.cget("text") == "Done. All 4 files are accounted for."
    assert len(list((dest / "2023" / "06").glob("*.jpg"))) == 3


@needs_exiftool
def test_undo_from_the_done_step(app, tmp_path):
    src, dest = _export(tmp_path), tmp_path / "Organized"
    _organize(app, src, dest)
    asked = []
    app._ask = lambda title, message, yes, no: asked.append(message) or True
    app._undo()
    assert asked and asked[0].startswith("Delete 3 files")
    assert "Your Takeout files are not affected." in asked[0]
    assert app.done_banner.cget("text") == "Undone. 3 files were deleted from the destination."
    assert not list(dest.rglob("IMG_*.jpg"))
    assert app.undo_button.instate(["disabled"])


@needs_exiftool
def test_nothing_new_to_add(app, tmp_path):
    src, dest = _export(tmp_path), tmp_path / "Organized"
    _organize(app, src, dest)
    app._start_over()
    app._primary()
    _pump(app, lambda: app.preview is not None)
    assert app.preview_status.cget("text") == ("Everything in this Takeout is already in "
                                               "your library. Nothing to add.")
    assert app.primary.instate(["disabled"])


def test_no_photos_found(app, tmp_path):
    src = tmp_path / "Takeout"
    src.mkdir()
    with zipfile.ZipFile(src / "takeout-001.zip", "w") as zf:
        zf.writestr("Takeout/Google Photos/Trip/metadata.json", json.dumps({"title": "Trip"}))
    app.source.set(str(src))
    app.dest.set(str(tmp_path / "Organized"))
    app._primary()
    _pump(app, lambda: app.preview is not None)
    assert app.preview_status.cget("text").startswith("No photos were found in this folder.")
    assert app.primary.instate(["disabled"])


def test_alt_shortcuts_press_visible_buttons(app):
    app.root.deiconify()  # only buttons on screen respond
    app.show_step(2)
    app.root.update()

    class Key:
        char, keysym = "b", "b"

    assert app._mnemonic(Key()) == "break"
    assert app.step == 1
    assert app._mnemonic(Key()) is None  # Back is hidden on step 1


def test_details_log_says_the_level_in_words(app):
    app.show_step(3)
    app._log("IMG_7.jpg has no date", "warning")
    app._log("Copied IMG_8.jpg", "info")
    text = app.details.get("1.0", "end").splitlines()
    assert text[:2] == ["Warning: IMG_7.jpg has no date", "Info: Copied IMG_8.jpg"]
    assert app.details.cget("state") == "disabled"


def test_screen_reader_calls_are_safe_on_this_tk(app):
    from takeout_organizer import gui_a11y
    gui_a11y.name(app.source_entry, "Takeout folder")
    gui_a11y.describe(app.source_entry, "Folder with your ZIP files")
    gui_a11y.changed(app.run_bar)
    assert gui_a11y.AVAILABLE == (tk.TkVersion >= 9.1)


def test_copy_diagnostics_puts_a_bug_report_on_the_clipboard(app, tmp_path):
    app.dest.set(str(tmp_path / "Organized"))
    app.show_step(4)
    app._copy_diagnostics()
    copied = app.root.clipboard_get()
    assert copied.startswith("Takeout Organizer diagnostics") and "Python" in copied
    assert str(tmp_path) not in copied
    assert app.copied.cget("text").startswith("Diagnostics copied.")


def test_the_self_test_flag_starts_and_quits_without_showing_a_window(tk_root, monkeypatch):
    # What the release workflow runs against the built window app.
    from takeout_organizer import cli, gui
    shown = []
    monkeypatch.setenv("TAKEOUT_ORGANIZER_SELFTEST", "1")
    monkeypatch.setattr(gui.GooglePhotosOrganizerGUI, "__init__",
                        lambda self, *a, **k: setattr(self, "root", tk_root))
    monkeypatch.setattr(gui.GooglePhotosOrganizerGUI, "run",
                        lambda self: (shown.append(self.root.state()), self.root.mainloop()))
    assert cli.main(["gui"]) == 0
    assert shown == ["withdrawn"]


def test_the_extras_are_offered_and_remembered(app, tmp_path):
    from takeout_organizer.deduplicator import HEIC_SUPPORT
    assert app._options()["rename"] is None and app._options()["heic_copies"] is None
    app.rename_on.set(True)
    app.heic_copies.set(True)
    options = app._options()
    assert options["rename"] == "{date:%Y%m%d_%H%M%S}_{name}"
    if HEIC_SUPPORT is not None:   # CI installs the heic extra
        assert options["heic_copies"] == "jpg"
    app.source.set(str(tmp_path))
    app.dest.set(str(tmp_path / "out"))
    app._remember()
    remembered = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert remembered["rename"] == "{date:%Y%m%d_%H%M%S}_{name}"


def test_the_language_changes_in_place_and_is_remembered(app, tmp_path):
    from takeout_organizer import i18n
    from takeout_organizer.gui import LAYOUT_CHOICES
    app.source.set(str(tmp_path))
    app.layout.set(app._label_of(LAYOUT_CHOICES, "year-month-day"))
    app.language.set("Deutsch")
    app._change_language()
    assert i18n.active() == "de"
    assert app.primary.cget("text") == "Plan anzeigen"
    assert app.step_labels[0].cget("text") == "1  Ordner w\u00e4hlen"
    assert app.source.get() == str(tmp_path)
    assert app.layout.get() == "Jahr / Monat / Tag"
    assert app._options()["layout"] == "year-month-day"
    remembered = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert remembered["language"] == "de"
    app.language.set("English")
    app._change_language()
    assert app.primary.cget("text") == "Preview plan"
    assert app._options()["layout"] == "year-month-day"


@pytest.mark.parametrize("name", ["Deutsch", "Espa\u00f1ol", "Fran\u00e7ais"])
def test_every_language_builds_the_window_and_maps_choices_back(app, name):
    from takeout_organizer import i18n
    from takeout_organizer.gui import LAYOUT_CHOICES
    app.language.set(name)
    app._change_language()
    assert i18n.active() != "en"
    for _label, value in LAYOUT_CHOICES:
        app.layout.set(app._label_of(LAYOUT_CHOICES, value))
        assert app._options()["layout"] == value
    assert app.primary.cget("text") == i18n._("Preview plan") != "Preview plan"
    assert app.language_box.instate(["!disabled"])
    app.show_step(2)
    assert app.language_box.instate(["disabled"])


def test_place_names_are_offered_except_with_the_flat_layout(app, tmp_path):
    from takeout_organizer.gui import LAYOUT_CHOICES
    assert app._options()["place_names"] is False
    assert "GeoNames (geonames.org), CC BY 4.0" in app.place_note.cget("text")
    app.place_names.set(True)
    assert app._options()["place_names"] is True
    app.layout.set(app._label_of(LAYOUT_CHOICES, "flat"))
    assert app._options()["place_names"] is False and app.place_check.instate(["disabled"])
    assert "choose another layout" in app.place_note.cget("text")
    app.layout.set(app._label_of(LAYOUT_CHOICES, "year-month"))
    app.place_names.set(True)
    assert not app.place_check.instate(["disabled"])
    app.source.set(str(tmp_path))
    app.dest.set(str(tmp_path / "out"))
    app._remember()
    remembered = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert remembered["place_names"] is True


def test_a_bad_name_template_is_shown_under_its_field(app, tmp_path):
    app.root.deiconify()  # Tk records focus only for a window on screen
    app.root.update()
    app.rename_on.set(True)
    app.rename_template.set("{place}_{name}")
    app._start_preview()
    app.root.update()
    assert app.step == 1 and app.worker is None
    assert app.rename_status.cget("text").startswith("Problem: The name template uses {place}")
    assert app.root.focus_lastfor() == app.rename_entry
    app.rename_template.set("{date}_{name}")
    assert app._template_ok() and app.rename_status.cget("text").startswith("for example")


def test_without_a_heic_decoder_the_copies_option_is_off_and_disabled(tk_root, tmp_path,
                                                                       monkeypatch):
    import gc
    from takeout_organizer import deduplicator
    from takeout_organizer.gui import GooglePhotosOrganizerGUI
    monkeypatch.setattr(deduplicator, "HEIC_SUPPORT", None)
    (tmp_path / "settings.json").write_text('{"heic_copies": true}', encoding="utf-8")
    gc.disable()
    try:
        window = GooglePhotosOrganizerGUI(tk_root, settings_path=str(tmp_path / "settings.json"))
        assert window._options()["heic_copies"] is None
        texts = []

        def walk(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == "TCheckbutton":
                    texts.append((str(child.cget("text")), child.instate(["disabled"])))
                walk(child)
        walk(tk_root)
        assert ("Also make a JPG copy of each HEIC photo (the HEIC is kept)", True) in texts
    finally:
        for after_id in tk_root.tk.splitlist(tk_root.tk.call("after", "info")):
            tk_root.after_cancel(after_id)
        for child in tk_root.winfo_children():
            child.destroy()
        gc.collect()
        gc.enable()


def _own_window(tk_root, tmp_path):
    from takeout_organizer.gui import GooglePhotosOrganizerGUI
    return GooglePhotosOrganizerGUI(tk_root, settings_path=str(tmp_path / "settings.json"))


def _close_own_window(tk_root):
    import gc
    _clear(tk_root)
    gc.collect()
    gc.enable()


def test_pixel_sizes_grow_with_the_screen_scale(tk_root, tmp_path, monkeypatch):
    import gc
    from takeout_organizer import gui
    monkeypatch.setattr(gui, "scale_factor", lambda root: 1.5)
    gc.disable()
    try:
        window = _own_window(tk_root, tmp_path)
        assert gui.PAD == 36 and gui.GAP == 12 and gui.px(4) == 6
        assert int(str(window.preview_status.cget("wraplength"))) == 1290
        width, height = tk_root.minsize()
        screen_w, screen_h = tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()
        # 720 x 560 at 150 %, but never more than the screen holds.
        assert width == min(1080, screen_w - 60) and height == min(840, screen_h - 120)
    finally:
        _close_own_window(tk_root)


def test_a_remembered_size_too_big_for_the_screen_is_not_used(tk_root, tmp_path):
    import gc
    (tmp_path / "settings.json").write_text('{"geometry": "9000x9000+0+0"}', encoding="utf-8")
    gc.disable()
    try:
        _own_window(tk_root, tmp_path)
        tk_root.update_idletasks()
        assert tk_root.winfo_width() <= tk_root.winfo_screenwidth()
        assert tk_root.winfo_height() <= tk_root.winfo_screenheight()
    finally:
        _close_own_window(tk_root)


def test_high_contrast_keeps_the_system_colors(tk_root, tmp_path, monkeypatch):
    import gc
    from takeout_organizer import gui
    monkeypatch.setattr(gui, "high_contrast", lambda: True)
    # Windows names its contrast colors; Tk elsewhere does not know them.
    monkeypatch.setattr(gui, "CONTRAST_COLORS",
                        {"text": "#000000", "background": "#ffffff", "bar": "#0000ff"})
    (tmp_path / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
    gc.disable()
    try:
        window = _own_window(tk_root, tmp_path)
        assert window.theme == "contrast"
        assert window.theme_button.grid_info() == {}          # no theme switch
        window._toggle_theme()
        assert window.settings.theme == "dark" and window.theme == "contrast"
        window._set_status(window.source_status, "problem")
        assert str(window.source_status.cget("foreground")) == "#000000"
        assert str(window.done_summary.cget("background")) == "#ffffff"
    finally:
        _close_own_window(tk_root)
