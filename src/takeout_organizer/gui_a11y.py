"""Names and announcements for screen readers, through Tk 9.1's `accessible`.

Tk 9.1 exposes ttk widgets to screen readers (NVDA, Narrator, VoiceOver,
Orca) and lets an app give them names and descriptions and announce changes.
Python's bundled Tk is 8.6 for now, where every call here does nothing, so
the window becomes readable the day a Python with Tk 9.1 is used.

Canvas items are not exposed by Tk 9.1, so the window draws nothing that
carries meaning only on a canvas.
"""

from __future__ import annotations

import tkinter as tk

AVAILABLE = tk.TkVersion >= 9.1


def _call(widget, *args) -> None:
    if not AVAILABLE:
        return
    try:
        widget.tk.call("tk", "accessible", *args)
    except tk.TclError:
        pass  # an accessibility call must never break the window


def name(widget, text: str) -> None:
    """The name a screen reader reads for the widget."""
    _call(widget, "set_acc_name", widget, text)


def describe(widget, text: str) -> None:
    """Extra help read after the name (the helper text under a field)."""
    _call(widget, "set_acc_description", widget, text)


def changed(widget) -> None:
    """Tell screen readers a value changed (progress, step, status text)."""
    _call(widget, "emit_selection_change", widget)
