"""Languages of the window: the catalog reader, the catalogs themselves, and
the helpers the window formats its text with."""

from __future__ import annotations

import os
import re
import string
import sys
import unicodedata
from pathlib import Path

import pytest

from takeout_organizer import i18n
from takeout_organizer.i18n import mnemonic, parse_po

ROOT = Path(__file__).resolve().parent.parent
LOCALE = ROOT / "src" / "takeout_organizer" / "locale"
CODES = sorted(p.stem for p in LOCALE.glob("*.po"))

sys.path.insert(0, str(ROOT / "packaging"))
import update_translations  # noqa: E402

SAMPLE = r'''
# A translator's comment
msgid ""
msgstr ""
"Language: xx\n"
"Plural-Forms: nplurals=3; plural=(n==1 ? 0 : n==2 ? 1 : 2);\n"
"X-Language-Name: Test\n"

#: gui.py
msgid "Open"
msgstr "Ouvrir"

msgid "Two "
"lines"
msgstr "Deux "
"lignes"

msgid "A \"quote\"\tand a\\ backslash\n"
msgstr "Q \"q\"\tb\\ c\n"

#, fuzzy
msgid "Not checked yet"
msgstr "Pas encore"

msgid "Untranslated"
msgstr ""

msgid "{count} file"
msgid_plural "{count} files"
msgstr[0] "one {count}"
msgstr[1] "two {count}"
msgstr[2] "many {count}"
msgctxt "menu"
msgid "Next"
msgstr "Suivant"
'''


def test_the_catalog_reader_handles_what_po_files_hold():
    messages, headers = parse_po(SAMPLE)
    assert headers["X-Language-Name"] == "Test" and headers["Language"] == "xx"
    assert messages["Open"] == "Ouvrir"
    assert messages["Two lines"] == "Deux lignes"
    assert messages['A "quote"\tand a\\ backslash\n'] == 'Q "q"\tb\\ c\n'
    assert "Not checked yet" not in messages and "Untranslated" not in messages
    assert messages[("{count} file", "{count} files")] == ["one {count}", "two {count}",
                                                           "many {count}"]
    assert messages["Next"] == "Suivant"   # an entry right after another, with a context
    rule = i18n.plural_rule(headers["Plural-Forms"])
    assert [rule(n) for n in (1, 2, 5)] == [0, 1, 2]


def test_a_broken_catalog_is_reported_with_its_line():
    with pytest.raises(ValueError, match="line 2"):
        parse_po('msgid "a"\nmsgstr "unterminated\n')


@pytest.mark.parametrize("text, expected", [
    ("&Help", ("Help", 0)), ("Open &report", ("Open report", 5)),
    ("Save && quit", ("Save & quit", -1)), ("Plain", ("Plain", -1)),
])
def test_shortcut_letters(text, expected):
    assert mnemonic(text) == expected


def test_languages_follow_the_system_and_fall_back_to_english(monkeypatch):
    assert i18n.activate("de_AT.UTF-8") == "de"
    assert i18n._("Organize") == "Ordnen"
    assert i18n.activate("pt_BR") == "en" and i18n._("Organize") == "Organize"
    monkeypatch.setenv("LANGUAGE", "fr_CA:en")
    assert i18n.activate("") == "fr"
    assert i18n.ngettext("{count} second", "{count} seconds", 0) == "{count} seconde"
    assert i18n.number(1234567) == "1 234 567" and i18n.decimal(2.5) == "2,5"
    assert i18n.activate("en") == "en" and i18n.number(1234567) == "1,234,567"


def test_every_catalog_names_its_language():
    names = i18n.languages()
    assert list(names)[0] == "en"
    assert {code: names[code] for code in CODES} == {
        "de": "Deutsch", "es": "Espa\u00f1ol", "fr": "Fran\u00e7ais"}


def test_the_catalogs_are_up_to_date_with_the_window():
    """Run packaging/update_translations.py when this fails."""
    messages = update_translations.collect()
    pot = (LOCALE / "messages.pot").read_text(encoding="utf-8")
    assert pot == update_translations.render(messages, update_translations.POT_HEADERS)
    for code in CODES:
        text = (LOCALE / f"{code}.po").read_text(encoding="utf-8")
        translations, headers = parse_po(text)
        expected = update_translations.render(
            messages, update_translations.parse_po_header(text), translations)
        assert text == expected, code


def _entries(code):
    return parse_po((LOCALE / f"{code}.po").read_text(encoding="utf-8"))[0]


@pytest.mark.parametrize("code", CODES)
def test_every_message_is_translated(code):
    translated = _entries(code)
    for (msgid, plural), _places in update_translations.collect():
        key = msgid if plural is None else (msgid, plural)
        assert key in translated, f"{code}: {msgid!r} is not translated"
    assert "fuzzy" not in (LOCALE / f"{code}.po").read_text(encoding="utf-8")


def _fields(text):
    return sorted(name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name)


@pytest.mark.parametrize("code", CODES)
def test_translations_keep_the_placeholders(code):
    for key, value in _entries(code).items():
        msgid = key if isinstance(key, str) else key[1]
        for text in ([value] if isinstance(value, str) else value):
            assert _fields(text) == _fields(msgid), (code, msgid, text)
            text.format(**{name: 1 for name in _fields(text)})   # formats cleanly


@pytest.mark.parametrize("code", CODES)
def test_translations_use_plain_punctuation(code):
    """Accented letters and the Spanish question mark, but no typographic
    dashes or quotes and no exclamation marks, as in the rest of the project."""
    for key, value in _entries(code).items():
        for text in ([value] if isinstance(value, str) else value):
            for char in text:
                if ord(char) < 128:
                    assert char != "!", (code, text)
                    continue
                category = unicodedata.category(char)
                assert category[0] in "LM" or char == "\u00bf", (code, char, text)


@pytest.mark.parametrize("code", ["en"] + CODES)
def test_shortcut_letters_are_unique_where_buttons_show_together(code):
    i18n.activate(code)
    screens = [
        ("&Help", "&Back", "Open &report", "Open &log", "&Start over", "&Undo this run",
         "&Copy diagnostics"),
        ("&Help", "&Show details"),
        ("&Help", "&Hide details"),
    ]
    for buttons in screens:
        letters = []
        for button in buttons:
            label, index = mnemonic(i18n._(button))
            assert index >= 0, (code, button)
            letters.append(label[index].lower())
        if code != "en":   # English keeps its long-standing Help and Hide on H
            assert len(set(letters)) == len(letters), (code, buttons, letters)


def test_the_summary_table_shows_in_the_window_language():
    from takeout_organizer.main_processor import summary_text
    table = {"rows": [("Input files", {}, 1234, 0),
                      ("in {folder}/ (date is a guess)", {"folder": "_undated"}, 2, 2)],
             "accounted": 1234, "balanced": True, "notes": []}
    english = summary_text(table)
    assert english[0].startswith("Input files") and english[0].endswith("1,234")
    i18n.activate("de")
    german = summary_text(table, i18n._, i18n.number)
    assert german[0].startswith("Eingabedateien") and german[0].endswith("1.234")
    assert german[1].strip().startswith("in _undated/ (Datum gesch\u00e4tzt)")
    assert german[2].startswith("Insgesamt erfasst") and german[2].endswith("alle Dateien erfasst")


def test_the_source_text_stays_ascii():
    """Only the catalogs and the place data hold other characters."""
    for path in (ROOT / "src" / "takeout_organizer").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), path.name
    assert re.search(r"[^\x00-\x7f]", (LOCALE / "messages.pot").read_text("utf-8")) is None
    assert os.path.isfile(LOCALE / "messages.pot")
