"""Sortable file names (--rename): a template such as "{date:%Y%m%d_%H%M%S}_{name}"
gives each photo a name that sorts by capture time in any file browser.

Fields: {date} or {date:FORMAT} (strftime; the default is %Y%m%d_%H%M%S) and
{name}, the file's own name without its extension. The extension is always
kept. A file whose date is only a guess keeps its own name, as it goes to
_undated/ so that a wrong date never hides it.
"""

from __future__ import annotations

import re
import string
from datetime import datetime

FIELDS = ('date', 'name')
DEFAULT_DATE_FORMAT = '%Y%m%d_%H%M%S'

# Characters a file name cannot hold on Windows, macOS or Linux.
_NOT_IN_NAMES = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def check_template(template: str) -> str:
    """The template if it is usable, else ValueError saying what is wrong."""
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(template) if field is not None]
    except ValueError as exc:
        raise ValueError(f"The name template {template!r} is not valid ({exc}).") from None
    unknown = sorted({field for field in fields if field not in FIELDS})
    if unknown:
        raise ValueError(f"The name template uses {', '.join('{' + f + '}' for f in unknown)}; "
                         "it can use {date}, {date:FORMAT} and {name}.")
    if not fields:
        raise ValueError("The name template must use {date} or {name}, or every photo would "
                         "get the same name.")
    return template


def render(template: str, file_name: str, date: datetime) -> str:
    """The new file name for file_name taken at date (its extension kept)."""
    stem, ext = _split(file_name)

    class Date:
        def __format__(self, spec: str) -> str:
            return date.strftime(spec or DEFAULT_DATE_FORMAT)

    name = template.format(date=Date(), name=stem)
    name = _NOT_IN_NAMES.sub('_', name).strip(' .') or stem
    return name + ext


def _split(file_name: str):
    dot = file_name.rfind('.')
    if dot <= 0:
        return file_name, ''
    return file_name[:dot], file_name[dot:]
