"""Collect the window's text into src/takeout_organizer/locale/messages.pot and
bring every <code>.po there up to date with it.

Run it after changing text in the window:

    python packaging/update_translations.py

Text is collected from _(), N_(), ngettext() and Text() calls with literal strings, in
the order it appears in the source. A .po keeps its translations; new text is
added untranslated (it shows in English until translated) and text no longer
used is dropped. tests/test_i18n.py fails when a catalog is out of date.
"""

from __future__ import annotations

import ast
import os
import sys
from typing import Dict, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PACKAGE = os.path.join(ROOT, 'src', 'takeout_organizer')
LOCALE = os.path.join(PACKAGE, 'locale')
SOURCES = ('gui.py', 'gui_support.py', 'gui_a11y.py', 'main_processor.py', 'preflight.py',
           'dates.py', 'naming.py', 'undo.py', 'metadata_handler.py')

sys.path.insert(0, os.path.join(ROOT, 'src'))
from takeout_organizer.i18n import parse_po  # noqa: E402

Message = Tuple[str, Optional[str]]   # (msgid, msgid_plural or None)


def collect() -> List[Tuple[Message, List[str]]]:
    """Every message with the places it is used, in source order."""
    found: Dict[Message, List[str]] = {}
    for name in SOURCES:
        with open(os.path.join(PACKAGE, name), encoding='utf-8') as f:
            tree = ast.parse(f.read(), name)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id in ('_', 'N_', 'ngettext', 'Text')]
        for node in sorted(calls, key=lambda n: (n.lineno, n.col_offset)):
            args = node.args
            if not args or not _literal(args[0]):
                continue
            if node.func.id == 'ngettext' or (node.func.id == 'Text' and len(args) > 1):
                # Text(singular, plural, count=...) is a plural message too.
                if len(args) < 2 or not _literal(args[1]):
                    continue
                key: Message = (args[0].value, args[1].value)
            else:
                key = (args[0].value, None)
            places = found.setdefault(key, [])
            if name not in places:
                places.append(name)
    return list(found.items())


def _literal(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _quote(text: str) -> str:
    escaped = (text.replace('\\', '\\\\').replace('"', '\\"').replace('\t', '\\t')
               .replace('\n', '\\n'))
    return f'"{escaped}"'


def _field(keyword: str, text: str) -> List[str]:
    """A keyword and its string; text with line breaks is split after each."""
    if '\n' not in text.rstrip('\n'):
        return [f"{keyword} {_quote(text)}"]
    parts = text.split('\n')
    lines = [f'{keyword} ""']
    for i, part in enumerate(parts):
        piece = part + ('\n' if i < len(parts) - 1 else '')
        if piece:
            lines.append(_quote(piece))
    return lines


def render(messages, headers: str, translations=None, nplurals: int = 2) -> str:
    """A .pot (translations None) or a .po with the given translations."""
    out = ['msgid ""', 'msgstr ""'] + [_quote(line + '\n') for line in headers.splitlines()]
    for (msgid, plural), places in messages:
        out.append('')
        out.append('#: ' + ' '.join(places))
        if any(c in msgid for c in '{}'):
            out.append('#, python-brace-format')
        out += _field('msgid', msgid)
        if plural is None:
            value = (translations or {}).get(msgid, '')
            out += _field('msgstr', value if isinstance(value, str) else '')
        else:
            out += _field('msgid_plural', plural)
            forms = (translations or {}).get((msgid, plural)) or [''] * nplurals
            for i in range(nplurals):
                out += _field(f'msgstr[{i}]', forms[i] if i < len(forms) else '')
    return '\n'.join(out) + '\n'


POT_HEADERS = """Project-Id-Version: takeout-organizer
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit
Plural-Forms: nplurals=2; plural=(n != 1);"""


def po_header_text(path: str) -> str:
    """The header block of an existing .po, as written."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    return parse_po_header(text)


def parse_po_header(text: str) -> str:
    lines = []
    header = text.split('\n\n', 1)[0]
    for line in header.splitlines():
        line = line.strip()
        if line.startswith('"'):
            lines.append(ast.literal_eval(line).rstrip('\n'))
    return '\n'.join(lines)


def update() -> List[str]:
    """Write messages.pot and every .po; returns the files that changed."""
    messages = collect()
    changed = []
    targets = {os.path.join(LOCALE, 'messages.pot'): render(messages, POT_HEADERS)}
    for name in sorted(os.listdir(LOCALE)):
        if not name.endswith('.po'):
            continue
        path = os.path.join(LOCALE, name)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        translations, headers = parse_po(text)
        nplurals = int(headers.get('Plural-Forms', 'nplurals=2').split('nplurals=')[1]
                       .split(';')[0])
        targets[path] = render(messages, parse_po_header(text), translations, nplurals)
    for path, text in targets.items():
        old = None
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                old = f.read()
        if old != text:
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
            changed.append(path)
    return changed


if __name__ == '__main__':
    os.makedirs(LOCALE, exist_ok=True)
    for path in update():
        print(f"updated {os.path.relpath(path, ROOT)}")
