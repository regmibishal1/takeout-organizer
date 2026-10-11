"""Everything the window needs that is not a widget: remembered settings, the
system's light or dark mode, opening folders, and checking the chosen folders.

Kept apart from gui.py so it can be tested without a display.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

from .i18n import _, ngettext, number, tr
from .main_processor import SetupError, check_folders
from .metadata_handler import MetadataHandler
from .preflight import check as preflight_check, human

APP_DIR_NAME = 'takeout-organizer'


def config_dir() -> str:
    """Where the app keeps its settings, per the platform's convention."""
    if os.name == 'nt':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, APP_DIR_NAME)


@dataclass
class Settings:
    """What the window remembers between launches (WCAG 3.3.7: no re-entry)."""
    source: str = ''
    dest: str = ''
    layout: str = 'year-month'
    dedup: str = 'perceptual'
    albums: bool = True
    time_zone: str = ''
    rename: str = ''               # a --rename template; empty means files keep their names
    rename_template: str = '{date:%Y%m%d_%H%M%S}_{name}'
    heic_copies: bool = False
    place_names: bool = False      # the town or city on date folders (2023/06 Tokyo)
    language: str = ''             # a code from i18n.languages(); empty means the system's
    theme: str = 'system'          # system, light or dark
    geometry: str = ''
    seen_guide: bool = False
    path: Optional[str] = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Optional[str] = None) -> 'Settings':
        path = path or os.path.join(config_dir(), 'settings.json')
        settings = cls(path=path)
        try:
            with open(path, encoding='utf-8') as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return settings
        for name, value in stored.items():
            if hasattr(settings, name) and name != 'path':
                setattr(settings, name, value)
        return settings

    def save(self) -> None:
        if not self.path:
            return
        data = asdict(self)
        data.pop('path')
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass  # remembering settings must never stop the app


def scale_factor(root) -> float:
    """How much larger than at 96 DPI the window's pixel sizes should be.
    Tk already scales fonts by the screen's DPI (125 % or 150 % on Windows);
    paddings, wrap widths and window sizes given in pixels are multiplied by
    this so they grow with the text. macOS reports 72 and scales the whole
    window itself, and a factor this close to 1 is not worth a change."""
    if sys.platform == 'darwin':
        return 1.0
    try:
        factor = root.winfo_fpixels('1i') / 96
    except Exception:
        return 1.0
    return factor if factor >= 1.15 else 1.0


def high_contrast() -> bool:
    """Windows High Contrast is on: the window then keeps the system's colors
    instead of its own theme."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class HighContrast(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwFlags', wintypes.DWORD),
                        ('lpszDefaultScheme', wintypes.LPWSTR)]
        info = HighContrast(ctypes.sizeof(HighContrast), 0, None)
        spi_gethighcontrast = 0x42
        if not ctypes.windll.user32.SystemParametersInfoW(
                spi_gethighcontrast, ctypes.sizeof(info), ctypes.byref(info), 0):
            return False
        return bool(info.dwFlags & 1)   # HCF_HIGHCONTRASTON
    except Exception:
        return False


def system_theme() -> str:
    """'dark' when the operating system is set to dark mode, else 'light'."""
    try:
        if os.name == 'nt':
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            value, _kind = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            return 'light' if value else 'dark'
        if sys.platform == 'darwin':
            result = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                                    capture_output=True, text=True, timeout=5)
            return 'dark' if 'Dark' in result.stdout else 'light'
    except Exception:
        pass
    return 'light'


def enable_dpi_awareness() -> None:
    """Sharp text on high-DPI Windows screens; call before creating Tk."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def open_path(path: str) -> None:
    """Open a folder or file with the system's default app."""
    if os.name == 'nt':
        os.startfile(path)  # noqa: S606 - opening the user's own folder
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


@dataclass
class FieldCheck:
    """The result of checking one field, shown under it."""
    ok: bool
    message: str = ''
    level: str = 'info'   # info (muted), warning, problem


def check_source(source: str) -> Tuple[FieldCheck, List[str]]:
    """Check the Takeout folder: (result for the field, export warnings)."""
    if not source.strip():
        return FieldCheck(False, _("Choose the folder with your Takeout files."), 'problem'), []
    if not os.path.isdir(source):
        return FieldCheck(False, _("This folder does not exist."), 'problem'), []
    result = preflight_check(source, os.path.join(source, '.unused'), source, dry_run=True)
    if result.zip_count == 0 and result.source_bytes == 0:
        return FieldCheck(False, _("No Takeout files found in this folder."), 'problem'), []
    if result.zip_count:
        found = ngettext("{count} ZIP file found, {size}.", "{count} ZIP files found, {size}.",
                         result.zip_count).format(count=number(result.zip_count),
                                                  size=human(result.source_bytes))
    else:
        found = _("An extracted Takeout, {size}.").format(size=human(result.source_bytes))
    return FieldCheck(True, found), result.warnings


def check_dest(source: str, dest: str) -> FieldCheck:
    """Check the destination folder against the Takeout folder."""
    if not dest.strip():
        return FieldCheck(False, _("Choose where to save the organized photos."), 'problem')
    if source.strip() and os.path.isdir(source):
        try:
            check_folders(source, dest)
        except SetupError as exc:
            return FieldCheck(False, "\n".join(tr(m) for m in exc.messages), 'problem')
    result = preflight_check(source, dest, dest, dry_run=False) if os.path.isdir(source) \
        else None
    free = _("{size} free.").format(size=human(result.dest_free)) \
        if result and result.dest_free is not None else ''
    if result and result.problems:
        return FieldCheck(False, tr(result.problems[0]), 'problem')
    if os.path.isdir(dest) and any(os.scandir(dest)):
        return FieldCheck(True, _("This folder already has photos. New photos will be added; "
                                  "nothing is overwritten.") + (f" {free}" if free else ''),
                          'warning')
    return FieldCheck(True, free)


def find_exiftool() -> Tuple[Optional[str], str]:
    """(path or None, a line for the window)."""
    handler = MetadataHandler(require=False)
    if not handler.exiftool_path:
        return None, _("ExifTool not found. It writes the dates into your photos.")
    return handler.exiftool_path, _("ExifTool {version} found.").format(
        version=handler.get_exiftool_version())
