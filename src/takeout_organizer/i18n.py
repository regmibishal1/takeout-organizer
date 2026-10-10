"""Languages of the window.

Translations are gettext catalogs, locale/<code>.po, read directly (no
compiled .mo files, so nothing to build). The window's text is marked with
_() where it is shown, plural phrases with ngettext(), and text defined at
import time with N_(), translated where it is shown. A catalog names its
language in its own words in an "X-Language-Name" header.

Logs, reports, the command line and diagnostics stay in English on purpose:
they go into bug reports.

Button labels mark their keyboard shortcut with &, as in "&Help": Alt+H
presses the button. mnemonic() splits that off; translators choose a letter of
their own word.
"""

from __future__ import annotations

import ast
import gettext
import locale
import os
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

LOCALE_DIR = os.path.join(os.path.dirname(__file__), 'locale')
SOURCE_LANGUAGE = 'en'

_messages: Dict[str, str] = {}
_plurals: Dict[Tuple[str, str], List[str]] = {}
_active = SOURCE_LANGUAGE
_thousands = ','
_decimal = '.'


def _english_plural(n: int) -> int:
    return int(n != 1)


_plural_index: Callable[[int], int] = _english_plural


def N_(text: str) -> str:
    """Marks text for translation where it is defined; _() translates it."""
    return text


def _(text: str) -> str:
    """The text in the active language (as it is when not translated)."""
    return _messages.get(text) or text


def ngettext(singular: str, plural: str, n: int) -> str:
    """The singular or plural text for n, in the active language."""
    forms = _plurals.get((singular, plural))
    if forms:
        index = _plural_index(n)
        if 0 <= index < len(forms) and forms[index]:
            return forms[index]
    return singular if n == 1 else plural


def number(n: int) -> str:
    """A whole number with the active language's thousands separator, from
    the catalog's "X-Thousands-Separator" header ("space" for a space)."""
    return f"{n:,}".replace(',', _thousands)


def decimal(x: float) -> str:
    """A number with one decimal, with the active language's decimal mark
    ("X-Decimal-Separator" header)."""
    return f"{x:.1f}".replace('.', _decimal)


def mnemonic(text: str) -> Tuple[str, int]:
    """("Help", 0) for "&Help": the label and the index of its shortcut
    letter, or -1 when it has none. "&&" is a literal &."""
    shown, underline, i = [], -1, 0
    while i < len(text):
        if text[i] == '&' and i + 1 < len(text):
            if text[i + 1] == '&':
                shown.append('&')
            else:
                if underline < 0:
                    underline = len(shown)
                shown.append(text[i + 1])
            i += 2
            continue
        shown.append(text[i])
        i += 1
    return ''.join(shown), underline


def active() -> str:
    return _active


def languages() -> Dict[str, str]:
    """Code -> the language's own name, for every catalog, English first."""
    names = {SOURCE_LANGUAGE: 'English'}
    try:
        files = sorted(f for f in os.listdir(LOCALE_DIR) if f.endswith('.po'))
    except OSError:
        files = []
    for name in files:
        code = name[:-3]
        try:
            headers = read_po(os.path.join(LOCALE_DIR, name))[1]
        except (OSError, ValueError):
            continue
        names[code] = headers.get('X-Language-Name', code)
    return names


def activate(code: Optional[str]) -> str:
    """Use the language code (from languages()); None or '' means the
    system's. Returns the code in use: English when there is no catalog."""
    global _messages, _plurals, _plural_index, _active, _thousands, _decimal
    code = (code or system_language() or SOURCE_LANGUAGE)
    code = _match(code, languages())
    _messages, _plurals, _plural_index = {}, {}, _english_plural
    _active, _thousands, _decimal = code, ',', '.'
    if code != SOURCE_LANGUAGE:
        messages, headers = read_po(os.path.join(LOCALE_DIR, f"{code}.po"))
        _plural_index = plural_rule(headers.get('Plural-Forms', ''))
        separator = headers.get('X-Thousands-Separator', ',')
        _thousands = ' ' if separator == 'space' else separator
        _decimal = headers.get('X-Decimal-Separator', '.')
        for key, value in messages.items():
            if isinstance(key, tuple):
                _plurals[key] = value
            else:
                _messages[key] = value
    return code


def _match(code: str, available) -> str:
    """'de_AT.UTF-8' -> 'de' when only 'de' exists; English otherwise."""
    code = code.split('.')[0].split('@')[0].replace('-', '_')
    for candidate in (code, code.split('_')[0].lower()):
        if candidate in available:
            return candidate
    return SOURCE_LANGUAGE


def system_language() -> Optional[str]:
    """The language the system shows its own text in, such as 'de_DE'."""
    for variable in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES'):
        value = os.environ.get(variable, '').split(':')[0]
        if value and value not in ('C', 'POSIX'):
            return value
    if sys.platform == 'win32':
        try:
            import ctypes
            ui = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return locale.windows_locale.get(ui)
        except Exception:
            return None
    if sys.platform == 'darwin':
        # Apps started from the Finder have no LANG; the user's choice lives here.
        try:
            out = subprocess.run(['defaults', 'read', '-g', 'AppleLanguages'],
                                 capture_output=True, text=True, timeout=5).stdout
            first = out.strip('()\n ').split(',')[0].strip().strip('"')
            return first or None
        except Exception:
            return None
    value = os.environ.get('LANG', '')
    return value if value and value not in ('C', 'POSIX') else None


def plural_rule(header: str) -> Callable[[int], int]:
    """The plural form index for n, from a Plural-Forms header."""
    for part in header.split(';'):
        name, _sep, expression = part.strip().partition('=')
        if name.strip() == 'plural' and expression.strip():
            try:
                return gettext.c2py(expression.strip())
            except (ValueError, SyntaxError):
                break
    return _english_plural


def read_po(path: str):
    """(messages, headers) of a .po file. messages maps msgid -> msgstr, and
    (msgid, msgid_plural) -> [msgstr[0], msgstr[1], ...]. Fuzzy and empty
    translations are left out, so their English shows."""
    with open(path, encoding='utf-8') as f:
        return parse_po(f.read())


def parse_po(text: str):
    messages: Dict[object, object] = {}
    headers: Dict[str, str] = {}
    entry: Dict[str, object] = {}
    fuzzy = False
    field: Optional[Tuple[str, Optional[int]]] = None

    def finish():
        nonlocal entry, fuzzy, field
        msgid = entry.get('msgid')
        if msgid is not None and not fuzzy:
            if msgid == '':
                for line in str(entry.get('msgstr', '')).splitlines():
                    name, sep, value = line.partition(':')
                    if sep:
                        headers[name.strip()] = value.strip()
            elif 'msgid_plural' in entry:
                forms = entry.get('msgstr_n', {})
                values = [forms[i] for i in sorted(forms)]
                if values and all(values):
                    messages[(msgid, entry['msgid_plural'])] = values
            elif entry.get('msgstr'):
                messages[msgid] = entry['msgstr']
        entry, fuzzy, field = {}, False, None

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            finish()
            continue
        if line.startswith('#'):
            if line.startswith('#,') and 'fuzzy' in line:
                if 'msgid' in entry:
                    finish()
                fuzzy = True
            continue
        if line.startswith('"'):
            if field is None:
                raise ValueError(f"line {number}: text outside an entry")
            _append(entry, field, _unquote(line, number))
            continue
        keyword, _sep, rest = line.partition(' ')
        if keyword in ('msgctxt', 'msgid') and 'msgid' in entry:
            finish()   # a new entry without a blank line before it
        if keyword in ('msgctxt', 'msgid', 'msgid_plural', 'msgstr'):
            field = (keyword, None)
        elif keyword.startswith('msgstr[') and keyword.endswith(']'):
            field = ('msgstr_n', int(keyword[7:-1]))
        else:
            raise ValueError(f"line {number}: unknown keyword {keyword!r}")
        _append(entry, field, _unquote(rest.strip(), number), start=True)
    finish()
    return messages, headers


def _append(entry, field, value: str, start: bool = False) -> None:
    name, index = field
    if index is None:
        entry[name] = value if start else entry.get(name, '') + value
    else:
        forms = entry.setdefault(name, {})
        forms[index] = value if start else forms.get(index, '') + value


def _unquote(text: str, number: int) -> str:
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        raise ValueError(f"line {number}: expected a quoted string")
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        raise ValueError(f"line {number}: bad escape in {text}") from None
