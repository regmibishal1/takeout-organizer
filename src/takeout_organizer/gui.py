"""
The Takeout Organizer window.

One window, four steps: choose folders, preview, organize, done. Steps follow
each other and cannot be skipped, the main button always says what it does,
and every state is said in words, never by color alone. Preview runs the same
plan the run then applies, so what step 2 shows is what step 3 does.

Theme: Sun Valley (sv-ttk), light or dark, following the system by default.
"""

import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, ttk
from typing import Any, Dict, List, Optional

import sv_ttk

from . import __version__, gui_a11y, i18n, places
from .gui_support import (FieldCheck, Settings, check_dest, check_source,
                          enable_dpi_awareness, find_exiftool, high_contrast, open_path,
                          scale_factor, system_theme)
from .i18n import N_, _, decimal, mnemonic, ngettext, number, tr
from .preflight import human

# Text defined here is marked with N_() and translated where it is shown.
STEPS = (N_("Choose folders"), N_("Preview"), N_("Organize"), N_("Done"))

LAYOUT_CHOICES = ((N_("Year / Month"), "year-month"), (N_("Year / Month / Day"), "year-month-day"),
                  (N_("Year"), "year"), (N_("One folder"), "flat"))
DEDUP_CHOICES = ((N_("Smart (recommended)"), "perceptual"), (N_("Exact copies only"), "exact"),
                 (N_("Off"), "off"))

EXPORT_GUIDE = N_(
    "1. Go to takeout.google.com, choose Deselect all, then select only Google Photos.\n"
    "2. Choose Send download link via email, Export once, file type .zip, size 50 GB.\n"
    "3. Download every part within 7 days into one folder. Do not unzip them.\n"
    "4. If you use shared albums, download those albums too, into the same folder.\n"
    "5. Choose that folder below."
)

README_URL = "https://github.com/regmibishal1/takeout-organizer#readme"
EXIFTOOL_URL = "https://exiftool.org/"

# Status colors with at least 4.5:1 contrast on Sun Valley's backgrounds.
RENAME_HELP = N_("for example 20230615_143045_IMG_1234.jpg; {date} and {name} are filled "
                 "in, and files with only a guessed date keep their names")

STATUS_COLORS = {
    'light': {'problem': '#B42318', 'warning': '#8A5300', 'info': '#57606A'},
    'dark': {'problem': '#FF8A80', 'warning': '#E3A008', 'info': '#A9B1BA'},
}

# The run's phases as the window lists them, with the processor phases in each.
RUN_PHASES = (
    (N_("Reading Takeout"), ("Reading Takeout",)),
    (N_("Finding duplicates"), ("Finding duplicates",)),
    (N_("Planning"), ("Planning",)),
    (N_("Writing metadata and copying"), ("Copying", "Writing metadata")),
)
# The processor's phase names, as the preview shows them.
PROGRESS_PHASES = (N_("Reading Takeout"), N_("Finding duplicates"), N_("Planning"),
                   N_("Copying"), N_("Writing metadata"))

# The preview's four numbers.
PREVIEW_TILES = (N_("Photos and videos"), N_("Dated from Google"), N_("Duplicates to skip"),
                 N_("Need a look"))

# The result banner's side bar; the banner text always says the result too.
BANNER_COLORS = {
    'light': {'ok': '#116329', 'warning': '#8A5300', 'problem': '#B42318'},
    'dark': {'ok': '#4AC26B', 'warning': '#E3A008', 'problem': '#FF8A80'},
}


def _duration(seconds: float) -> str:
    if seconds < 90:
        n = max(1, round(seconds))
        return ngettext("{count} second", "{count} seconds", n).format(count=n)
    if seconds < 90 * 60:
        n = round(seconds / 60)
        return ngettext("{count} minute", "{count} minutes", n).format(count=n)
    return _("{count} hours").format(count=decimal(seconds / 3600))


# Sizes in pixels are given as at 96 DPI and grow with the screen's scale
# (125 %, 150 % on Windows) through px(), as the text does; set per window.
_scale = 1.0
PAD = 24   # window padding
GAP = 8    # between related controls


def px(n: float) -> int:
    """n pixels at 96 DPI, at this screen's scale."""
    return round(n * _scale)


def _use_scale(factor: float) -> None:
    global _scale, PAD, GAP
    _scale = factor
    PAD, GAP = px(24), px(8)


_THEME_FONTS: Dict[str, int] = {}   # sv-ttk font -> its size in pixels at 96 DPI


def _scale_theme_fonts(root) -> None:
    """sv-ttk sizes its fonts in pixels, which Tk does not grow with the
    screen as it does sizes in points: at 150 % buttons and fields kept small
    text beside larger labels. They are scaled from their own sizes, so this
    can run again."""
    for name in root.tk.splitlist(root.tk.call('font', 'names')):
        if not name.startswith('SunValley'):
            continue
        font = tkfont.Font(root=root, name=name, exists=True)
        size = int(font.cget('size'))
        if size < 0:
            font.configure(size=-px(_THEME_FONTS.setdefault(name, -size)))


# Windows High Contrast: the user's own colors, by their Windows names (only
# Tk on Windows knows them). The words already say each status; color adds
# nothing the user did not choose.
CONTRAST_COLORS = {'text': 'SystemWindowText', 'background': 'SystemWindow',
                   'bar': 'SystemHighlight'}

# Monospace fonts for the summary table, first available wins.
MONO_FONTS = ("Cascadia Mono", "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono")


def theme_color(option: str, element: str = "TFrame") -> str:
    """A color of the current ttk theme, from the element or the root style."""
    style = ttk.Style()
    return (style.lookup(element, option) or style.lookup(".", option)
            or ("#ffffff" if option == "background" else "#000000"))


class ScrollFrame(ttk.Frame):
    """A frame whose content scrolls when it is taller than the window, so
    nothing is cut off on a small screen or with large system text."""

    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = ttk.Frame(self.canvas)
        self.inner.columnconfigure(0, weight=1)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._resized)
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        self.match_theme()

    def match_theme(self) -> None:
        self.canvas.configure(background=theme_color("background"))

    def _resized(self, event=None) -> None:
        self.canvas.itemconfigure(self._window, width=self.canvas.winfo_width())
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        overflow = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        if overflow:
            self.bar.grid()
        else:
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)

    def _wheel(self, event) -> None:
        if self.winfo_ismapped() and self.bar.winfo_ismapped():
            self.canvas.yview_scroll(-1 * (event.delta // 120 or (1 if event.delta < 0 else -1)),
                                     "units")


class GooglePhotosOrganizerGUI:
    """The desktop app. `run()` shows the window until it is closed."""

    def __init__(self, root: Optional[tk.Tk] = None, settings_path: Optional[str] = None):
        if root is None:
            enable_dpi_awareness()
            root = tk.Tk()
        self.root = root
        self.settings = Settings.load(settings_path)
        i18n.activate(self.settings.language)
        _use_scale(scale_factor(root))
        # High Contrast keeps the system's colors: Sun Valley's would replace them.
        self.contrast = high_contrast()
        if self.contrast:
            self.theme = 'contrast'
        else:
            self.theme = system_theme() if self.settings.theme == 'system' \
                else self.settings.theme
            sv_ttk.set_theme(self.theme, root)
            _scale_theme_fonts(root)

        self.root.title("Takeout Organizer")
        self._size_window()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.messages: "queue.Queue" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.preview = None      # the preview's MainProcessor, once done
        self.processor = None    # the run's MainProcessor, once done
        self.step = 0
        self._status_levels: Dict[ttk.Label, str] = {}
        self._done_level = 'ok'
        self._phase_started: Dict[str, float] = {}
        self._layout_trace: Optional[str] = None

        self.source = tk.StringVar(value=self.settings.source)
        self.dest = tk.StringVar(value=self.settings.dest)
        self.layout = tk.StringVar(value=self._label_of(LAYOUT_CHOICES, self.settings.layout))
        self.dedup = tk.StringVar(value=self.settings.dedup)
        self.albums = tk.BooleanVar(value=self.settings.albums)
        self.time_zone = tk.StringVar(value=self.settings.time_zone)
        self.rename_on = tk.BooleanVar(value=bool(self.settings.rename))
        self.rename_template = tk.StringVar(
            value=self.settings.rename or self.settings.rename_template)
        self.heic_copies = tk.BooleanVar(value=self.settings.heic_copies)
        self.place_names = tk.BooleanVar(value=self.settings.place_names)

        self._fonts()
        self._build()
        self._match_theme()
        self.show_step(1)
        self.root.after(100, self._poll)

    # ------------------------------------------------------------------ shell

    def _fonts(self) -> None:
        base = tkfont.nametofont("TkDefaultFont")
        size = abs(base.cget("size") or 10)
        family = base.cget("family")
        self.font_heading = tkfont.Font(family=family, size=size + 5, weight="bold")
        self.font_label = tkfont.Font(family=family, size=size, weight="bold")
        self.font_small = tkfont.Font(family=family, size=max(9, size - 1))
        self.font_value = tkfont.Font(family=family, size=size + 10, weight="bold")
        available = set(tkfont.families(self.root))
        mono = next((name for name in MONO_FONTS if name in available), None)
        self.font_mono = tkfont.Font(family=mono, size=size) if mono \
            else tkfont.nametofont("TkFixedFont")

    def _size_window(self) -> None:
        """Minimum and first size, scaled and kept within the screen (with room
        for the taskbar): a laptop at 150 % has little of it. A remembered
        size is kept unless it no longer fits, say on a smaller screen."""
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        most_w, most_h = screen_w - px(40), screen_h - px(80)
        self.root.minsize(min(px(720), most_w), min(px(560), most_h))
        saved = re.match(r'(\d+)x(\d+)', self.settings.geometry or '')
        if saved and int(saved.group(1)) <= screen_w and int(saved.group(2)) <= screen_h:
            self.root.geometry(self.settings.geometry)
        else:
            self.root.geometry(self._centered(min(px(960), most_w), min(px(680), most_h)))

    def _centered(self, width: int, height: int) -> str:
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        return f"{width}x{height}+{x}+{y}"

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(PAD, px(12)))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Takeout Organizer", font=self.font_heading).grid(
            row=0, column=0, sticky="w")
        self.language_names = i18n.languages()
        self.language = tk.StringVar(value=self.language_names.get(i18n.active(), "English"))
        self.language_box = ttk.Combobox(header, textvariable=self.language, state="readonly",
                                         width=12, values=list(self.language_names.values()))
        self.language_box.grid(row=0, column=1, padx=(0, GAP))
        gui_a11y.name(self.language_box, _("Language"))
        self.language_box.bind("<<ComboboxSelected>>", lambda event: self._change_language())
        self._button(header, N_("&Help"), lambda: webbrowser.open(README_URL)).grid(
            row=0, column=2, padx=(0, GAP))
        self.theme_button = ttk.Button(header, command=self._toggle_theme)
        self.theme_button.grid(row=0, column=3, padx=(0, GAP))
        if self.contrast:
            self.theme_button.grid_remove()   # the system's colors are in use
        self._label_theme_button()
        ttk.Label(header, text=f"v{__version__}", font=self.font_small).grid(row=0, column=4)

        stepper = ttk.Frame(self.root, padding=(PAD, px(4), PAD, px(12)))
        stepper.grid(row=1, column=0, sticky="ew")
        self.step_labels: List[ttk.Label] = []
        for index, name in enumerate(STEPS):
            label = ttk.Label(stepper, text=f"{index + 1}  {_(name)}")
            label.grid(row=0, column=index * 2, sticky="w")
            self.step_labels.append(label)
            if index < len(STEPS) - 1:
                ttk.Separator(stepper, orient="horizontal").grid(
                    row=0, column=index * 2 + 1, sticky="ew", padx=GAP)
                stepper.columnconfigure(index * 2 + 1, weight=1)

        self.content = ttk.Frame(self.root, padding=(PAD, px(8), PAD, px(8)))
        self.content.grid(row=2, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
        self.frames = {1: self._build_folders(), 2: self._build_preview(),
                       3: self._build_organize(), 4: self._build_done()}

        footer = ttk.Frame(self.root, padding=(PAD, px(12)))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        self.back_button = self._button(footer, N_("&Back"), self._back)
        self.back_button.grid(row=0, column=0, sticky="w")
        self.primary = ttk.Button(footer, text=_("Preview plan"), style="Accent.TButton",
                                  command=self._primary)
        self.primary.grid(row=0, column=2, sticky="e")
        self.root.bind("<Return>", lambda event: self._primary())
        self.root.bind("<Alt-KeyPress>", self._mnemonic)

    def show_step(self, step: int) -> None:
        """Show one step; the stepper says which in words, not color only."""
        self.step = step
        for index, frame in self.frames.items():
            if index == step:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        for index, label in enumerate(self.step_labels, 1):
            name = _(STEPS[index - 1])
            text = _("{step} (done)").format(step=name) if index < step else name
            label.configure(text=f"{index}  {text}",
                            font=self.font_label if index == step else self.font_small)
        self.root.title(f"{_(STEPS[step - 1])} - Takeout Organizer")
        if step in (2, 4):
            self.back_button.grid()
        else:
            self.back_button.grid_remove()
        # The language can change only before anything has started.
        self.language_box.state(["!disabled"] if step == 1 else ["disabled"])
        self.primary.configure(text={1: _("Preview plan"), 2: _("Organize"), 3: _("Stop"),
                                     4: _("Open organized folder")}[step],
                               style="TButton" if step == 3 else "Accent.TButton")
        if step == 2 and self.preview is not None:
            self._update_organize_label()

    def _mnemonic(self, event) -> Optional[str]:
        """Alt plus a button's underlined letter presses it (no single-letter
        shortcuts, WCAG 2.1.4)."""
        key = (event.char or event.keysym or "").lower()[:1]
        if not key:
            return None
        for button in self._buttons(self.root):
            underline = int(str(button.cget("underline")) or -1)
            text = str(button.cget("text"))
            if 0 <= underline < len(text) and text[underline].lower() == key \
                    and button.winfo_ismapped() and not button.instate(["disabled"]):
                button.invoke()
                return "break"
        return None

    def _buttons(self, widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                yield child
            yield from self._buttons(child)

    def _primary(self) -> None:
        if self.worker is not None and self.worker.is_alive() and self.step != 3:
            return
        actions = {1: self._start_preview, 2: self._start_run, 3: self._stop,
                   4: self._done_primary}
        actions[self.step]()

    def _done_primary(self) -> None:
        if self.processor is not None and self.processor.cancelled:
            self._start_run(resume=True)
        else:
            open_path(self.dest.get())

    def _back(self) -> None:
        if self.step == 2:
            self.cancel_event.set()
            self.show_step(1)
        elif self.step == 4:
            self._start_over()

    def _button(self, parent, text: str, command, **options) -> ttk.Button:
        """A button whose label marks its Alt shortcut with &, in any language."""
        label, underline = mnemonic(_(text))
        return ttk.Button(parent, text=label, underline=underline, command=command, **options)

    @staticmethod
    def _relabel(button: ttk.Button, text: str) -> None:
        label, underline = mnemonic(_(text))
        button.configure(text=label, underline=underline)

    def _change_language(self) -> None:
        """Show the window in the chosen language at once: it is built again,
        with every choice and entry kept."""
        code = next((c for c, name in self.language_names.items()
                     if name == self.language.get()), None)
        if code is None or code == i18n.active() or self.step != 1:
            return
        layout = self._value_of(LAYOUT_CHOICES, self.layout.get())
        self.settings.language = code
        self._remember()
        i18n.activate(code)
        if self._layout_trace:
            self.layout.trace_remove("write", self._layout_trace)
            self._layout_trace = None
        self.root.unbind_all("<MouseWheel>")
        for child in self.root.winfo_children():
            child.destroy()
        self._status_levels = {}
        self.layout.set(self._label_of(LAYOUT_CHOICES, layout))
        self._build()
        self._match_theme()
        self.show_step(1)
        self.language_box.focus_set()

    def _label_theme_button(self) -> None:
        self.theme_button.configure(
            text=_("Dark mode") if self.theme == 'light' else _("Light mode"))

    def _toggle_theme(self) -> None:
        if self.contrast:
            return
        self.theme = 'dark' if self.theme == 'light' else 'light'
        sv_ttk.set_theme(self.theme, self.root)
        _scale_theme_fonts(self.root)
        self.settings.theme = self.theme
        self._label_theme_button()
        for label, level in self._status_levels.items():
            label.configure(foreground=self._status_color(level))
        self._match_theme()

    def _status_color(self, level: str) -> str:
        if self.contrast:
            return CONTRAST_COLORS['text']
        return STATUS_COLORS[self.theme][level]

    def _banner_color(self, level: str) -> str:
        if self.contrast:
            return CONTRAST_COLORS['bar']
        return BANNER_COLORS[self.theme][level]

    def _match_theme(self) -> None:
        """Plain Tk widgets are not themed by sv-ttk; give them its colors."""
        if self.contrast:
            background, foreground = CONTRAST_COLORS['background'], CONTRAST_COLORS['text']
        else:
            background = theme_color("background")
            foreground = theme_color("foreground", "TLabel")
        for text in (self.done_summary, self.details):
            text.configure(background=background, foreground=foreground,
                           insertbackground=foreground)
        for level in ('warning', 'error', 'critical'):
            self.details.tag_configure(
                level, foreground=self._status_color('problem' if level != 'warning'
                                                     else 'warning'))
        self.done_bar.configure(background=self._banner_color(self._done_level))
        self.folders_scroll.match_theme()

    def _close(self) -> None:
        self.cancel_event.set()
        self.settings.geometry = self.root.geometry()
        self._remember()
        self.root.destroy()

    # ---------------------------------------------------------- step 1: folders

    def _build_folders(self) -> ttk.Frame:
        self.folders_scroll = ScrollFrame(self.content)
        frame = self.folders_scroll.inner
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text=_("Fix the dates and locations in your Google Photos Takeout. "
                                "Free, offline, and your original files are never changed."),
                  font=self.font_small, wraplength=px(760)).grid(row=0, column=0, columnspan=2, sticky="w",
                                             pady=(0, px(16)))

        self.source_entry, self.source_status = self._folder_field(
            frame, 1, _("Takeout folder"),
            _("Folder with your Takeout ZIP files, or an already extracted Takeout."),
            self.source, self._browse_source)
        self.dest_entry, self.dest_status = self._folder_field(
            frame, 4, _("Save organized photos to"),
            _("A new or empty folder. Your Takeout files are never changed."),
            self.dest, self._browse_dest)

        exiftool_row = ttk.Frame(frame)
        exiftool_row.grid(row=7, column=0, columnspan=2, sticky="w", pady=(px(4), px(12)))
        path, line = find_exiftool()
        self.exiftool_status = ttk.Label(exiftool_row, text=line, font=self.font_small)
        self.exiftool_status.grid(row=0, column=0, sticky="w")
        self._set_status(self.exiftool_status, 'info' if path else 'problem')
        if not path:
            ttk.Button(exiftool_row, text=_("Install ExifTool"),
                       command=lambda: webbrowser.open(EXIFTOOL_URL)).grid(
                row=0, column=1, padx=GAP)

        self.guide_button = ttk.Button(frame, command=self._toggle_guide)
        self.guide_button.grid(row=8, column=0, sticky="w", pady=(px(4), 0))
        self.guide = ttk.Label(frame, text=_(EXPORT_GUIDE), justify="left", wraplength=px(640))
        self.guide.grid(row=9, column=0, columnspan=2, sticky="w", padx=(px(16), 0), pady=px(4))
        self._toggle_guide(show=not self.settings.seen_guide)

        self.advanced_button = ttk.Button(frame, command=self._toggle_advanced)
        self.advanced_button.grid(row=10, column=0, sticky="w", pady=(px(8), 0))
        self.advanced = self._build_advanced(frame)
        self.advanced.grid(row=11, column=0, columnspan=2, sticky="w", padx=(px(16), 0), pady=px(4))
        self._toggle_advanced(show=False)
        return self.folders_scroll

    def _folder_field(self, frame, row, title, help_text, variable, browse):
        ttk.Label(frame, text=title, font=self.font_label).grid(row=row, column=0, sticky="w")
        ttk.Label(frame, text=help_text, font=self.font_small).grid(
            row=row + 1, column=0, columnspan=2, sticky="w", pady=(0, px(4)))
        line = ttk.Frame(frame)
        line.grid(row=row + 2, column=0, columnspan=2, sticky="ew")
        line.columnconfigure(0, weight=1)
        entry = ttk.Entry(line, textvariable=variable)
        entry.grid(row=0, column=0, sticky="ew")
        gui_a11y.name(entry, title)
        gui_a11y.describe(entry, help_text)
        # Checked when leaving the field, never while typing; focus stays put.
        entry.bind("<FocusOut>", lambda event: self.validate())
        ttk.Button(line, text=_("Browse..."), width=12, command=browse).grid(
            row=0, column=1, padx=(GAP, 0))
        status = ttk.Label(line, text="", font=self.font_small, wraplength=px(640), justify="left")
        status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(px(4), px(16)))
        return entry, status

    def _build_advanced(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent)
        ttk.Label(frame, text=_("Folders")).grid(row=0, column=0, sticky="w", pady=px(2))
        ttk.Combobox(frame, textvariable=self.layout, state="readonly", width=22,
                     values=[_(label) for label, _value in LAYOUT_CHOICES]).grid(
            row=0, column=1, sticky="w", padx=GAP)
        ttk.Label(frame, text=_("Duplicates")).grid(row=1, column=0, sticky="w", pady=px(2))
        choices = ttk.Frame(frame)
        choices.grid(row=1, column=1, sticky="w", padx=GAP)
        for column, (label, value) in enumerate(DEDUP_CHOICES):
            ttk.Radiobutton(choices, text=_(label), value=value, variable=self.dedup).grid(
                row=0, column=column, padx=(0, px(12)))
        ttk.Checkbutton(frame, text=_("Create album playlists"), variable=self.albums).grid(
            row=2, column=1, sticky="w", padx=GAP, pady=px(2))
        ttk.Label(frame, text=_("Time zone")).grid(row=3, column=0, sticky="w", pady=px(2))
        zone = ttk.Frame(frame)
        zone.grid(row=3, column=1, sticky="w", padx=GAP)
        ttk.Entry(zone, textvariable=self.time_zone, width=18).grid(row=0, column=0)
        ttk.Label(zone, text=_("for photos without location or camera zone; empty means "
                               "this computer's zone, for example +09:00 or Asia/Tokyo"),
                  font=self.font_small, wraplength=px(420)).grid(row=0, column=1, padx=GAP)

        ttk.Label(frame, text=_("File names")).grid(row=4, column=0, sticky="nw", pady=px(2))
        names = ttk.Frame(frame)
        names.grid(row=4, column=1, sticky="w", padx=GAP)
        ttk.Checkbutton(names, text=_("Name files by date:"), variable=self.rename_on).grid(
            row=0, column=0, sticky="w")
        self.rename_entry = ttk.Entry(names, textvariable=self.rename_template, width=32)
        self.rename_entry.grid(row=0, column=1, padx=(GAP, 0))
        gui_a11y.name(self.rename_entry, _("Name template"))
        self.rename_status = ttk.Label(names, text=_(RENAME_HELP), font=self.font_small,
                                       wraplength=px(600))
        self.rename_status.grid(row=1, column=0, columnspan=2, sticky="w")

        from .deduplicator import HEIC_SUPPORT
        heic = ttk.Checkbutton(frame, variable=self.heic_copies,
                               text=_("Also make a JPG copy of each HEIC photo (the HEIC is "
                                      "kept)"))
        heic.grid(row=5, column=1, sticky="w", padx=GAP, pady=px(2))
        if HEIC_SUPPORT is None:
            self.heic_copies.set(False)
            heic.state(["disabled"])
            ttk.Label(frame, text=_("needs takeout-organizer[heic]"),
                      font=self.font_small).grid(
                row=6, column=1, sticky="w", padx=GAP)

        self.place_check = ttk.Checkbutton(
            frame, variable=self.place_names,
            text=_("Add the town or city to folder names, for example 2023/06 Tokyo"))
        self.place_check.grid(row=7, column=1, sticky="w", padx=GAP, pady=(px(2), 0))
        self.place_note = ttk.Label(frame, font=self.font_small, wraplength=px(600))
        self.place_note.grid(row=8, column=1, sticky="w", padx=GAP)
        self._layout_trace = self.layout.trace_add(
            "write", lambda *args: self._match_place_names())
        self._match_place_names()
        return frame

    def _match_place_names(self) -> None:
        """Place names go on date folders, so the flat layout cannot have them."""
        if self._value_of(LAYOUT_CHOICES, self.layout.get()) == 'flat':
            self.place_names.set(False)
            self.place_check.state(["disabled"])
            self.place_note.configure(text=_("needs date folders: choose another layout"))
        else:
            self.place_check.state(["!disabled"])
            self.place_note.configure(text=_("Offline, from where each photo was taken.")
                                      + f" {places.ATTRIBUTION}.")

    def _toggle_guide(self, show: Optional[bool] = None) -> None:
        hide = self.guide.winfo_ismapped() if show is None else not show
        if hide:
            self.guide.grid_remove()
            self.guide_button.configure(text=_("Show: before you start"))
        else:
            self.guide.grid()
            self.guide_button.configure(text=_("Hide: before you start"))

    def _toggle_advanced(self, show: Optional[bool] = None) -> None:
        hide = self.advanced.winfo_ismapped() if show is None else not show
        if hide:
            self.advanced.grid_remove()
            self.advanced_button.configure(text=_("Show: advanced options"))
        else:
            self.advanced.grid()
            self.advanced_button.configure(text=_("Hide: advanced options"))

    def _browse_source(self) -> None:
        folder = filedialog.askdirectory(title=_("Choose your Takeout folder"),
                                         initialdir=self.source.get() or None)
        if folder:
            self.source.set(folder)
            self.validate()

    def _browse_dest(self) -> None:
        folder = filedialog.askdirectory(title=_("Choose where to save the organized photos"),
                                         initialdir=self.dest.get() or None)
        if folder:
            self.dest.set(folder)
            self.validate()

    def _set_status(self, label: ttk.Label, level: str) -> None:
        label.configure(foreground=self._status_color(level))
        self._status_levels[label] = level

    def _show_check(self, label: ttk.Label, check: FieldCheck) -> None:
        level = 'info' if check.ok and check.level != 'warning' else check.level
        prefix = {'problem': _("Problem: {message}"),
                  'warning': _("Note: {message}")}.get(level, "{message}")
        label.configure(text=prefix.format(message=check.message) if check.message else "")
        self._set_status(label, level)

    def validate(self) -> bool:
        """Check both folders and say what is wrong under each field."""
        source_check, warnings = check_source(self.source.get())
        if source_check.ok and warnings:
            source_check = FieldCheck(True, source_check.message + " "
                                      + " ".join(tr(w) for w in warnings),
                                      'warning')
        self._show_check(self.source_status, source_check)
        dest_check = check_dest(self.source.get(), self.dest.get())
        self._show_check(self.dest_status, dest_check)
        return source_check.ok and dest_check.ok

    # ---------------------------------------------------------- step 2: preview

    def _build_preview(self) -> ttk.Frame:
        frame = ttk.Frame(self.content)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        self.preview_status = ttk.Label(frame, text="", wraplength=px(860), justify="left")
        self.preview_status.grid(row=0, column=0, sticky="w", pady=(0, GAP))
        self.preview_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.preview_bar.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))

        tiles = ttk.Frame(frame)
        tiles.grid(row=2, column=0, sticky="ew", pady=(0, px(16)))
        self.tile_values: Dict[str, ttk.Label] = {}
        for column, name in enumerate(PREVIEW_TILES):
            tile = ttk.Frame(tiles, padding=px(12), relief="solid", borderwidth=1)
            tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else GAP, 0))
            tiles.columnconfigure(column, weight=1, uniform="tiles")
            value = ttk.Label(tile, text="-", font=self.font_value)
            value.grid(row=0, column=0, sticky="w")
            ttk.Label(tile, text=_(name), font=self.font_small, wraplength=px(180)).grid(
                row=1, column=0, sticky="w")
            self.tile_values[name] = value

        table = ttk.Frame(frame)
        table.grid(row=3, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        ttk.Label(table, text=_("Photos by year"), font=self.font_label).grid(
            row=0, column=0, sticky="w", pady=(0, px(4)))
        self.years = ttk.Treeview(table, columns=("files", "folder"), height=6)
        self.years.heading("#0", text=_("Year"))
        self.years.heading("files", text=_("Files"))
        self.years.heading("folder", text=_("Folder"))
        self.years.column("#0", width=px(100), stretch=False)
        self.years.column("files", width=px(100), anchor="e", stretch=False)
        self.years.column("folder", width=px(300))
        self.years.grid(row=1, column=0, sticky="nsew")

        self.report_button = ttk.Button(frame, text=_("Open full preview report"),
                                        command=self._open_preview_report)
        self.report_button.grid(row=4, column=0, sticky="w", pady=(px(12), 0))
        return frame

    def _options(self) -> Dict[str, Any]:
        return dict(
            layout=self._value_of(LAYOUT_CHOICES, self.layout.get()),
            dedup_mode=self.dedup.get(),
            albums='playlists' if self.albums.get() else 'off',
            time_zone=self.time_zone.get().strip() or None,
            rename=self.rename_template.get().strip() if self.rename_on.get() else None,
            heic_copies='jpg' if self.heic_copies.get() else None,
            place_names=self.place_names.get(),
        )

    def _start_preview(self) -> None:
        if not self._template_ok():
            return
        if not self.validate():
            # Focus the first field that needs fixing (WCAG 3.3.1).
            source_bad = self._status_levels.get(self.source_status) == 'problem'
            (self.source_entry if source_bad else self.dest_entry).focus_set()
            return
        self._remember()
        self.preview = None
        self.show_step(2)
        self.primary.state(["disabled"])
        self.preview_status.configure(text=_("Reading your Takeout. Nothing is written yet."))
        self.preview_bar.configure(value=0)
        self.report_button.state(["disabled"])
        for value in self.tile_values.values():
            value.configure(text="-")
        self.years.delete(*self.years.get_children())
        self._start_worker(dry_run=True)

    def _show_preview(self, processor) -> None:
        self.preview = processor
        summary = processor.summary or {}
        if not summary:
            self.preview_status.configure(
                text=tr(processor.problem) if processor.problem
                else _("The preview did not finish. The log says why."))
            return
        if summary.get('input', 0) == 0:
            self.preview_status.configure(
                text=_("No photos were found in this folder. Go back and choose the folder "
                       "that holds your takeout-*.zip files."))
            return
        to_write = summary.get('written', 0) + summary.get('other', 0)
        if to_write == 0 and summary.get('already', 0):
            self.preview_status.configure(
                text=_("Everything in this Takeout is already in your library. Nothing to "
                       "add."))
            self.preview_bar.configure(value=100)
            return
        self.primary.state(["!disabled"])
        sources = summary.get('date_sources', {})
        flagged = len(processor.dedup_result.flagged) if processor.dedup_result else 0
        look = (summary.get('failed', 0) + summary.get('without_metadata', 0) + flagged
                + processor.undated)
        values = {
            PREVIEW_TILES[0]: summary.get('written', 0) + summary.get('duplicates', 0),
            PREVIEW_TILES[1]: sources.get('sidecar', 0),
            PREVIEW_TILES[2]: summary.get('duplicates', 0),
            PREVIEW_TILES[3]: look,
        }
        for name, value in values.items():
            self.tile_values[name].configure(text=number(value))
        for year, count in sorted(processor.by_year.items()):
            self.years.insert("", "end", text=year, values=(number(count), f"{year}/"))
        if processor.undated:
            self.years.insert("", "end", text=_("No date"),
                              values=(number(processor.undated), "_undated/"))
        self.preview_bar.configure(value=100)
        self.preview_status.configure(
            text=_("This is the plan. Nothing has been written yet. Organizing applies "
                   "exactly this plan."))
        gui_a11y.changed(self.preview_status)
        self.report_button.state(["!disabled"])
        self._update_organize_label()

    def _update_organize_label(self) -> None:
        summary = (self.preview.summary or {}) if self.preview else {}
        count = summary.get('written', 0) + summary.get('other', 0)
        self.primary.configure(
            text=ngettext("Organize {count} file", "Organize {count} files", count).format(
                count=number(count)) if count else _("Organize"))

    def _open_preview_report(self) -> None:
        if self.preview is not None and self.preview.report_path:
            open_path(self.preview.report_path)

    # ------------------------------------------------------- step 3: organize

    def _build_organize(self) -> ttk.Frame:
        frame = ttk.Frame(self.content)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(7, weight=1)

        phases = ttk.Frame(frame)
        phases.grid(row=0, column=0, sticky="ew", pady=(0, px(16)))
        phases.columnconfigure(0, weight=1)
        self.phase_rows: List[ttk.Label] = []
        self.phase_states: List[ttk.Label] = []
        for row, (name, _names) in enumerate(RUN_PHASES):
            label = ttk.Label(phases, text=_(name))
            label.grid(row=row, column=0, sticky="w", pady=px(2))
            state = ttk.Label(phases, text=_("Waiting"), font=self.font_small)
            state.grid(row=row, column=1, sticky="e", pady=px(2))
            self.phase_rows.append(label)
            self.phase_states.append(state)

        self.run_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.run_bar.grid(row=1, column=0, sticky="ew")
        gui_a11y.name(self.run_bar, _("Progress of the current phase"))
        self.run_count = ttk.Label(frame, text="")
        self.run_count.grid(row=2, column=0, sticky="w", pady=(GAP, 0))
        self.run_file = ttk.Label(frame, text="", font=self.font_small)
        self.run_file.grid(row=3, column=0, sticky="w")
        ttk.Label(frame, text=_("Your computer will not sleep while this runs. You can stop "
                                "at any time and resume later."),
                  font=self.font_small, wraplength=px(760)).grid(row=4, column=0, sticky="w", pady=(px(16), px(8)))

        self.details_button = self._button(frame, N_("&Show details"), self._toggle_details)
        self.details_button.grid(row=5, column=0, sticky="w")
        self.details = tk.Text(frame, height=10, wrap="word", borderwidth=0,
                               font=self.font_mono, highlightthickness=0, state="disabled")
        self.details.grid(row=7, column=0, sticky="nsew", pady=(GAP, 0))
        self.details.bind("<Tab>", self._focus_next)
        self.details.bind("<Shift-Tab>", self._focus_previous)
        self.details.grid_remove()
        return frame

    def _toggle_details(self) -> None:
        if self.details.winfo_ismapped():
            self.details.grid_remove()
            self._relabel(self.details_button, N_("&Show details"))
        else:
            self.details.grid()
            self._relabel(self.details_button, N_("&Hide details"))

    def _log(self, message: str, level: str) -> None:
        """A log line in the details, the level word first (never color alone)."""
        level = level.lower()
        if level not in ('warning', 'error', 'critical') and not message.strip():
            return
        word = {'warning': _("Warning: {message}"), 'error': _("Error: {message}"),
                'critical': _("Error: {message}")}.get(level, _("Info: {message}"))
        self.details.configure(state="normal")
        self.details.insert("end", word.format(message=message.strip()) + "\n", level)
        self.details.see("end")
        self.details.configure(state="disabled")

    def _start_run(self, resume: bool = False) -> None:
        if not resume and (self.preview is None or not self.preview.manifest_path):
            return
        self.show_step(3)
        for state in self.phase_states:
            state.configure(text=_("Waiting"))
        self._phase_started = {}
        self.run_bar.configure(value=0)
        self.run_count.configure(text=_("Starting"))
        self.run_file.configure(text="")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.configure(state="disabled")
        self._start_worker(dry_run=False, resume=resume)

    def _stop(self) -> None:
        if not self._ask(_("Stop organizing?"),
                         _("Stop after the current file? Files already copied stay, and you "
                           "can resume later."), _("Stop"), _("Keep going")):
            return
        self.cancel_event.set()
        self.run_count.configure(text=_("Stopping after the current file"))
        self.primary.state(["disabled"])

    def _progress(self, current: int, total: int, phase: str, details: str) -> None:
        percent = (current / total * 100) if total else 0
        if self.step == 2:
            self.preview_bar.configure(value=percent)
            self.preview_status.configure(
                text=_("{phase}: {current} of {total}. Nothing is written yet.").format(
                    phase=_(phase), current=number(current), total=number(total)))
            return
        if self.step != 3:
            return
        index = next((i for i, (_name, names) in enumerate(RUN_PHASES) if phase in names),
                     None)
        if index is not None:
            for i, state in enumerate(self.phase_states):
                state.configure(text=_("Done") if i < index else
                                (_("Running") if i == index else _("Waiting")))
            self.phase_rows[index].configure(font=self.font_label)
            for i, row in enumerate(self.phase_rows):
                if i != index:
                    row.configure(font=tkfont.nametofont("TkDefaultFont"))
        started = self._phase_started.setdefault(phase, time.monotonic())
        elapsed = time.monotonic() - started
        text = ngettext("{current} of {total} file.", "{current} of {total} files.",
                        total).format(current=number(current), total=number(total))
        if total and 0 < current < total and elapsed > 10:
            text += " " + _("About {time} left.").format(
                time=_duration(elapsed / current * (total - current)))
        self.run_bar.configure(value=percent)
        gui_a11y.changed(self.run_bar)
        self.run_count.configure(text=text)
        self.run_file.configure(text=details)
        self.root.title(f"{percent:.0f}% - Takeout Organizer")

    # ----------------------------------------------------------- step 4: done

    def _build_done(self) -> ttk.Frame:
        frame = ttk.Frame(self.content)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        banner = ttk.Frame(frame, padding=(0, 0, 0, px(12)))
        banner.grid(row=0, column=0, sticky="ew")
        banner.columnconfigure(1, weight=1)
        self.done_bar = tk.Frame(banner, width=px(4))
        self.done_bar.grid(row=0, column=0, sticky="ns", padx=(0, px(12)))
        self.done_banner = ttk.Label(banner, text="", wraplength=px(820), justify="left",
                                     font=self.font_label)
        self.done_banner.grid(row=0, column=1, sticky="w", pady=px(4))

        self.done_summary = tk.Text(frame, height=16, wrap="none", borderwidth=0,
                                    font=self.font_mono, highlightthickness=0)
        self.done_summary.grid(row=1, column=0, sticky="nsew")
        # A read-only text must never trap Tab (WCAG 2.1.2).
        self.done_summary.bind("<Tab>", self._focus_next)
        self.done_summary.bind("<Shift-Tab>", self._focus_previous)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="w", pady=(px(12), 0))
        self.report_run_button = self._button(buttons, N_("Open &report"),
                                              self._open_run_report)
        self.report_run_button.grid(row=0, column=0, padx=(0, GAP))
        self.log_button = self._button(buttons, N_("Open &log"), self._open_log)
        self.log_button.grid(row=0, column=1, padx=(0, GAP))
        self.start_over_button = self._button(buttons, N_("&Start over"), self._start_over)
        self.start_over_button.grid(row=0, column=2, padx=(0, GAP))
        self.undo_button = self._button(buttons, N_("&Undo this run"), self._undo)
        self.undo_button.grid(row=0, column=3, padx=(0, GAP))
        self.diagnostics_button = self._button(buttons, N_("&Copy diagnostics"),
                                               self._copy_diagnostics)
        self.diagnostics_button.grid(row=0, column=4, padx=(0, GAP))
        self.copied = ttk.Label(frame, text="")
        self.copied.grid(row=3, column=0, sticky="w", pady=(GAP, 0))
        return frame

    def _copy_diagnostics(self) -> None:
        """Versions and this run's summary and warnings, without folder paths,
        for a bug report (the same text as `takeout-organizer diagnose`)."""
        from .diagnostics import report
        self.root.clipboard_clear()
        self.root.clipboard_append(report(self.dest.get() or None))
        self.copied.configure(text=_("Diagnostics copied. Paste them into your bug report; "
                                     "they hold no folder paths."))
        gui_a11y.changed(self.copied)

    @staticmethod
    def _focus_next(event) -> str:
        event.widget.tk_focusNext().focus_set()
        return "break"

    @staticmethod
    def _focus_previous(event) -> str:
        event.widget.tk_focusPrev().focus_set()
        return "break"

    def _show_done(self, processor) -> None:
        self.processor = processor
        self.copied.configure(text="")
        self.primary.state(["!disabled"])
        self.show_step(4)
        summary = processor.summary or {}
        level = 'ok'
        if processor.cancelled:
            done = sum(1 for o in processor.outcomes.values() if o in ('written', 'already'))
            total = len(processor.plan)
            banner = ngettext("Stopped. {done} of {total} file is done. Resume to finish; "
                              "nothing is copied twice.",
                              "Stopped. {done} of {total} files are done. Resume to finish; "
                              "nothing is copied twice.", total).format(
                done=number(done), total=number(total))
            level = 'warning'
            self.primary.configure(text=_("Resume"))
        elif not summary:
            banner = tr(processor.problem) if processor.problem \
                else _("The run did not finish. Open the log to see why.")
            level = 'problem'
        elif not summary.get('balanced', True):
            banner = _("The numbers do not add up (MISMATCH). Open the report and the log.")
            level = 'problem'
        elif summary.get('written', 0) == 0 and summary.get('failed', 0):
            banner = _("No files could be written. Open the log to see why.")
            level = 'problem'
        elif summary.get('failed'):
            banner = (ngettext("All {count} file is accounted for.",
                               "All {count} files are accounted for.", summary['input'])
                      .format(count=number(summary['input'])) + " "
                      + ngettext("{count} could not be copied; the report lists it.",
                                 "{count} could not be copied; the report lists them.",
                                 summary['failed']).format(count=number(summary['failed'])))
            level = 'warning'
        else:
            banner = ngettext("Done. All {count} file is accounted for.",
                              "Done. All {count} files are accounted for.",
                              summary['input']).format(count=number(summary['input']))
            flagged = len(processor.dedup_result.flagged) if processor.dedup_result else 0
            look = []
            if flagged:
                look.append(ngettext("{count} possible duplicate needs a look in the report.",
                                     "{count} possible duplicates need a look in the report.",
                                     flagged).format(count=number(flagged)))
            if processor.undated:
                look.append(ngettext("{count} guessed date needs a look in the report.",
                                     "{count} guessed dates need a look in the report.",
                                     processor.undated).format(count=number(processor.undated)))
            if look:
                banner += " " + " ".join(look)
                level = 'warning'
        self.done_banner.configure(text=banner)
        self.done_bar.configure(background=self._banner_color(level))
        self._done_level = level
        gui_a11y.changed(self.done_banner)
        self.done_summary.configure(state="normal")
        self.done_summary.delete("1.0", "end")
        self.done_summary.insert("1.0", "\n".join(self._summary_lines(processor)))
        self.done_summary.configure(state="disabled")
        if level == 'ok':
            self.log_button.grid_remove()
        else:
            self.log_button.grid()
        wrote = summary.get('written', 0) + summary.get('other', 0) if summary else 0
        if wrote or processor.cancelled:
            self.undo_button.grid()
            self.undo_button.state(["!disabled"])
        else:
            self.undo_button.grid_remove()
        self.root.title(f"{_('Done')} - Takeout Organizer")
        self.root.bell()  # long runs: say it is time to come back

    def _open_run_report(self) -> None:
        if self.processor is not None and self.processor.report_path:
            open_path(self.processor.report_path)

    def _open_log(self) -> None:
        if self.processor is not None and self.processor.log_file:
            open_path(self.processor.log_file)

    def _undo(self) -> None:
        """Delete the files this run wrote, after saying exactly what."""
        from . import undo
        plan, reason = undo.plan(self.dest.get(), self.processor.run_id)
        if plan is None or not plan.files:
            self.done_banner.configure(
                text=tr(reason) if reason else _("This run left nothing to undo."))
            return
        count = len(plan.files)
        question = ngettext("Delete {count} file ({size}) written by this run? Your Takeout "
                            "files are not affected.",
                            "Delete {count} files ({size}) written by this run? Your Takeout "
                            "files are not affected.", count).format(
            count=number(count), size=human(plan.total_bytes))
        if plan.changed:
            question += " " + ngettext("{count} file edited since will be kept.",
                                       "{count} files edited since will be kept.",
                                       len(plan.changed)).format(count=number(len(plan.changed)))
        if not self._ask(_("Undo this run?"), question, _("Delete files"), _("Keep them")):
            return
        deleted = undo.apply(self.dest.get(), plan)
        self.done_banner.configure(text=ngettext(
            "Undone. {count} file was deleted from the destination.",
            "Undone. {count} files were deleted from the destination.", deleted).format(
            count=number(deleted)))
        self.done_bar.configure(background=self._banner_color('ok'))
        self.undo_button.state(["disabled"])
        gui_a11y.changed(self.done_banner)

    def _start_over(self) -> None:
        self.preview = None
        self.processor = None
        self.show_step(1)

    def _ask(self, title: str, message: str, yes: str, no: str) -> bool:
        """A small modal question. Esc and the window's close mean no; focus
        starts on the safe answer."""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        answer = {'yes': False}
        body = ttk.Frame(dialog, padding=PAD)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text=message, wraplength=px(420), justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, px(16)))

        def close(result: bool) -> None:
            answer['yes'] = result
            dialog.grab_release()
            dialog.destroy()

        keep = ttk.Button(body, text=no, command=lambda: close(False))
        keep.grid(row=1, column=0, sticky="w")
        ttk.Button(body, text=yes, style="Accent.TButton", command=lambda: close(True)).grid(
            row=1, column=1, sticky="e", padx=(GAP, 0))
        dialog.bind("<Escape>", lambda event: close(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
        keep.focus_set()
        dialog.grab_set()
        self.root.wait_window(dialog)
        return answer['yes']

    # ------------------------------------------------------------------ worker

    def _start_worker(self, dry_run: bool, resume: bool = False) -> None:
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        options = self._options()
        plan_file = None if dry_run or resume else self.preview.manifest_path
        source, dest = self.source.get(), self.dest.get()
        # The worker holds only the queue, never the window: Tk objects must be
        # created, used and garbage-collected on the main thread only.
        messages = self.messages

        def work():
            from .main_processor import MainProcessor, SetupError
            try:
                processor = MainProcessor(
                    source, dest, dry_run=dry_run, plan_file=plan_file, resume=resume,
                    console=False, cancel_event=cancel,
                    progress_callback=lambda *a: messages.put(('progress', a)),
                    log_callback=lambda m, level: messages.put(('log', (m, level))),
                    **options)
            except SetupError as exc:
                messages.put(('setup_error', exc.messages))   # shown in the window's language
                return
            processor.run()
            messages.put(('preview_done' if dry_run else 'run_done', processor))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.messages.get_nowait()
                if kind == 'progress':
                    self._progress(*data)
                elif kind == 'log':
                    self._log(*data)
                elif kind == 'preview_done':
                    if self.step == 2:
                        self._show_preview(data)
                elif kind == 'run_done':
                    self._show_done(data)
                elif kind == 'setup_error':
                    self.primary.state(["!disabled"])
                    self.show_step(1)
                    self.source_status.configure(text=_("Problem: {message}").format(
                        message="\n".join(tr(m) for m in data)))
                    self._set_status(self.source_status, 'problem')
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._poll)
        except tk.TclError:
            pass  # the window was closed

    # ---------------------------------------------------------------- settings

    @staticmethod
    def _label_of(choices, value: str) -> str:
        return _(next((label for label, v in choices if v == value), choices[0][0]))

    @staticmethod
    def _value_of(choices, label: str) -> str:
        return next((v for lab, v in choices if _(lab) == label), choices[0][1])

    @staticmethod
    def _summary_lines(processor) -> List[str]:
        """The run's accounting summary in the window's language."""
        from .main_processor import summary_text
        if not processor.summary_table:
            return processor.summary_lines
        return summary_text(processor.summary_table, _, number)

    def _template_ok(self) -> bool:
        """A usable name template, or the problem under its field (WCAG 3.3.1)."""
        from .naming import check_template
        try:
            if self.rename_on.get():
                check_template(self.rename_template.get().strip())
        except ValueError as exc:
            self._toggle_advanced(show=True)   # the field lives in the options panel
            self.root.update_idletasks()
            self.rename_status.configure(text=_("Problem: {message}").format(
                message=tr(exc.args[0]) if exc.args else str(exc)))
            self._set_status(self.rename_status, 'problem')
            self.rename_entry.focus_set()
            return False
        self.rename_status.configure(text=_(RENAME_HELP))
        self._set_status(self.rename_status, 'info')
        return True

    def _remember(self) -> None:
        options = self._options()
        self.settings.source = self.source.get()
        self.settings.dest = self.dest.get()
        self.settings.layout = options['layout']
        self.settings.dedup = options['dedup_mode']
        self.settings.albums = self.albums.get()
        self.settings.time_zone = self.time_zone.get().strip()
        self.settings.rename = options['rename'] or ''
        self.settings.rename_template = self.rename_template.get().strip()
        self.settings.heic_copies = bool(options['heic_copies'])
        self.settings.place_names = options['place_names']
        self.settings.seen_guide = True
        self.settings.save()

    def run(self) -> None:
        """Show the window until it is closed."""
        self.root.mainloop()


if __name__ == '__main__':
    GooglePhotosOrganizerGUI().run()
