"""A static HTML report of a run: one file, no server, works offline.

It shows what happened to every file, with thumbnails for the duplicate
decisions, so a run can be checked in minutes. It is written to meet WCAG 2.2
AA: landmarks and a skip link, table captions and header scopes, text for
every status (never color alone), alt text on every image, visible focus, and
colors with at least 4.5:1 contrast in light and dark mode.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - Pillow is a core dependency
    Image = None
    ImageOps = None

THUMB_EDGE = 480
THUMB_QUALITY = 80

# Pairs shown with thumbnails; the rest are listed in duplicates_report.md.
MAX_PAIRS = 500


@dataclass
class FileInfo:
    """What the report shows about one file."""
    name: str
    path: str                     # where it is now (for a thumbnail), may be gone
    size: int = 0
    width: int = 0
    height: int = 0
    has_sidecar: bool = False
    outcome: str = ''             # Kept, Not copied, ...
    thumb: Optional[str] = None   # path of the thumbnail, relative to the report


@dataclass
class ReportData:
    title_date: datetime
    source: str
    dest: str
    dry_run: bool
    settings: List[Tuple[str, str]]
    summary_lines: List[str]
    summary: Dict
    failed: List[Tuple[str, str]] = field(default_factory=list)          # name, reason
    without_metadata: List[Tuple[str, str]] = field(default_factory=list)
    flagged: List[Tuple[FileInfo, FileInfo, int, str]] = field(default_factory=list)
    dropped: List[Tuple[FileInfo, FileInfo]] = field(default_factory=list)
    undated: List[str] = field(default_factory=list)
    by_year: Dict[str, int] = field(default_factory=dict)
    albums: Dict[str, int] = field(default_factory=dict)
    log_file: Optional[str] = None
    total_flagged: int = 0
    total_dropped: int = 0


def make_thumbnail(source: str, target: str) -> bool:
    """A JPEG of at most THUMB_EDGE pixels on the long side, upright."""
    if Image is None:
        return False
    try:
        with Image.open(source) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_EDGE, THUMB_EDGE))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            im.convert('RGB').save(target, 'JPEG', quality=THUMB_QUALITY)
        return True
    except Exception:
        return False


_CSS = """
:root {
  --surface: #FFFFFF; --surface-alt: #F5F6F8; --text: #1B1F24; --text-muted: #57606A;
  --primary: #0B5CAD; --success: #116329; --warning: #8A5300; --error: #B42318;
  --border-input: #6E7781; --focus: #0B5CAD; --track: #DDE1E6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #17191C; --surface-alt: #202327; --text: #E6E8EB; --text-muted: #A9B1BA;
    --primary: #6CB0F5; --success: #4AC26B; --warning: #E3A008; --error: #FF8A80;
    --border-input: #7D8590; --focus: #6CB0F5; --track: #30363D;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--surface); color: var(--text);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.page { max-width: 1120px; margin: 0 auto; padding: 0 24px 48px; }
@media (max-width: 600px) { .page { padding: 0 16px 32px; } }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 16px; top: 16px; background: var(--surface); padding: 8px; z-index: 10; }
a { color: var(--primary); }
a:focus, button:focus, input:focus, summary:focus {
  outline: 2px solid var(--focus); outline-offset: 2px;
}
h1 { font-size: 28px; line-height: 36px; font-weight: 600; margin: 32px 0 4px; }
h2 { font-size: 22px; margin: 40px 0 12px; }
h3 { font-size: 18px; margin: 24px 0 8px; }
.muted { color: var(--text-muted); font-size: 14px; }
nav ul { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 8px 20px; }
.banner {
  margin: 24px 0; padding: 16px; border: 1px solid var(--border-input);
  border-left: 4px solid var(--success); border-radius: 8px; background: var(--surface-alt);
}
.banner.attention { border-left-color: var(--warning); }
.banner.problem { border-left-color: var(--error); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
.tile { padding: 16px; border: 1px solid var(--border-input); border-radius: 8px; }
.tile .value { font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .label { font-size: 14px; color: var(--text-muted); }
pre.accounting {
  background: var(--surface-alt); padding: 16px; border-radius: 8px; overflow-x: auto;
  font-variant-numeric: tabular-nums;
}
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
caption { text-align: left; font-weight: 600; padding: 8px 0; }
th, td { text-align: left; padding: 12px 8px; border-bottom: 1px solid var(--track); }
th { position: sticky; top: 0; background: var(--surface); }
tbody tr:nth-child(even) { background: var(--surface-alt); }
td.num, th.num { text-align: right; }
.bar { display: inline-block; height: 8px; background: var(--primary); vertical-align: middle; }
.pairs { display: grid; gap: 16px; }
.pair { border: 1px solid var(--border-input); border-radius: 8px; padding: 16px; }
.pair .files { display: flex; flex-wrap: wrap; gap: 16px; }
figure { margin: 0; width: 240px; }
figure img, figure .noimg {
  width: 240px; height: 180px; object-fit: contain; background: var(--surface-alt);
  display: block; border-radius: 4px;
}
figure .noimg { display: flex; align-items: center; justify-content: center; }
figcaption { font-size: 14px; margin-top: 8px; overflow-wrap: anywhere; }
.badge { font-weight: 600; }
.badge.kept { color: var(--success); }
.badge.dropped { color: var(--error); }
ul.files-list { columns: 2 320px; padding-left: 20px; overflow-wrap: anywhere; }
dl.settings { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; }
dl.settings dt { color: var(--text-muted); }
dl.settings dd { margin: 0; overflow-wrap: anywhere; }
"""


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _figure(info: FileInfo) -> str:
    details = [_e(info.name)]
    if info.width and info.height:
        details.append(f"{info.width} by {info.height} pixels")
    if info.size:
        details.append(f"{info.size / 1024:,.0f} KB")
    details.append("Has Google sidecar: " + ("yes" if info.has_sidecar else "no"))
    css = 'kept' if info.outcome.startswith('Kept') else 'dropped'
    alt = f"{info.name}, " + (f"{info.width} by {info.height} pixels, " if info.width else "") \
        + info.outcome.lower()
    image = (f'<img src="{_e(info.thumb)}" alt="{_e(alt)}" loading="lazy">' if info.thumb
             else '<div class="noimg" role="img" aria-label="'
                  f'{_e(alt)}">No preview</div>')
    return (f'<figure>{image}<figcaption>{"<br>".join(details)}<br>'
            f'<span class="badge {css}">{_e(info.outcome)}</span></figcaption></figure>')


def render(data: ReportData) -> str:
    s = data.summary or {}
    day = data.title_date.strftime('%d %b %Y').lstrip('0')
    failed = s.get('failed', 0)
    review = max(len(data.flagged), data.total_flagged)
    if not s.get('balanced', True):
        banner_class, banner = 'problem', ("The numbers do not add up (MISMATCH). Some files "
                                           "have no recorded outcome; see the log.")
    elif failed or review or data.undated or data.without_metadata:
        parts = []
        if failed:
            parts.append(f"{failed:,} files failed to copy")
        if review:
            parts.append(f"{review:,} possible duplicates need review")
        if data.undated:
            parts.append(f"{len(data.undated):,} files have only a guessed date")
        if data.without_metadata:
            parts.append(f"{len(data.without_metadata):,} files have no new metadata")
        banner_class = 'attention'
        banner = (f"All {s.get('input', 0):,} files are accounted for. "
                  + "; ".join(parts) + ".")
    else:
        banner_class, banner = '', f"All {s.get('input', 0):,} files are accounted for."
    if data.dry_run:
        banner = "Preview only: nothing was written. " + banner

    tiles = [
        ("Input files", s.get('input', 0)),
        ("Photos and videos " + ("to be written" if data.dry_run else "written"),
         s.get('written', 0)),
        ("Duplicates not copied", s.get('duplicates', 0)),
        ("Need a look", failed + review + len(data.undated) + len(data.without_metadata)),
    ]

    out = [
        '<!DOCTYPE html>', '<html lang="en">', '<head>', '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Takeout Organizer report, {_e(day)}</title>',
        f'<style>{_CSS}</style>', '</head>', '<body>',
        '<a class="skip" href="#summary">Skip to summary</a>',
        '<div class="page">',
        '<header>',
        '<h1>Takeout Organizer report</h1>',
        f'<p class="muted">{_e(data.title_date.strftime("%d %b %Y, %H:%M").lstrip("0"))}. '
        f'From {_e(data.source)} to {_e(data.dest)}.</p>',
        '<nav aria-label="Sections"><ul>',
        '<li><a href="#summary">Summary</a></li>',
        '<li><a href="#attention">Needs attention</a></li>',
        '<li><a href="#duplicates">Duplicates removed</a></li>',
        '<li><a href="#years">Photos by year</a></li>',
        '<li><a href="#albums">Albums</a></li>',
        '<li><a href="#settings">Settings used</a></li>',
        '<li><a href="#help">Help</a></li>',
        '</ul></nav>',
        '</header>',
        '<main>',
        f'<div class="banner {banner_class}" role="status">{_e(banner)}</div>',
        '<section id="summary" aria-labelledby="summary-h">',
        '<h2 id="summary-h">Summary</h2>',
        '<div class="tiles">',
    ]
    for label, value in tiles:
        out.append(f'<div class="tile"><div class="value">{value:,}</div>'
                   f'<div class="label">{_e(label)}</div></div>')
    out += ['</div>', '<h3>Every file accounted for</h3>',
            '<pre class="accounting">' + _e("\n".join(data.summary_lines)) + '</pre>',
            '</section>']

    # Needs attention
    out += ['<section id="attention" aria-labelledby="attention-h">',
            '<h2 id="attention-h">Needs attention</h2>']
    if not (data.failed or data.flagged or data.undated or data.without_metadata):
        out.append('<p>Nothing needs attention.</p>')
    if data.failed:
        out.append(_table("Files that could not be copied", ("File", "Reason"), data.failed))
    if data.without_metadata:
        out.append(_table("Files copied without new metadata", ("File", "Reason"),
                          data.without_metadata))
    if data.flagged:
        out += ['<h3>Possible duplicates, both kept</h3>',
                '<p>These look alike but at least one check says they may be different '
                'photos, so both were kept. Compare them and delete one if they are the '
                'same.</p>', '<div class="pairs">']
        if review > len(data.flagged):
            out.append(f'<p>Showing the first {len(data.flagged):,} of {review:,}. The full '
                       'list is in .takeout-organizer/duplicates.md.</p>')
        for a, b, distance, reason in data.flagged:
            out.append(f'<div class="pair" role="group" aria-label="{_e(a.name)} and '
                       f'{_e(b.name)}"><p>Why both were kept: {_e(reason)} '
                       f'(visual difference {distance} of 64).</p><div class="files">'
                       f'{_figure(a)}{_figure(b)}</div></div>')
        out.append('</div>')
    if data.undated:
        out += ['<h3>Files with only a guessed date (in _undated/)</h3>',
                '<p>No capture date was found, so these were filed by their upload date or '
                'file time. Check them and move them where they belong.</p>',
                '<ul class="files-list">'] + [f'<li>{_e(n)}</li>' for n in data.undated] \
            + ['</ul>']
    out.append('</section>')

    # Duplicates removed
    out += ['<section id="duplicates" aria-labelledby="duplicates-h">',
            '<h2 id="duplicates-h">Duplicates removed</h2>']
    if data.dropped:
        total = max(len(data.dropped), data.total_dropped)
        out.append(f'<p>{total:,} copies were not copied because the same photo was '
                   'already kept. The kept copy is the one with the Google sidecar, then the '
                   'larger one.</p>')
        if total > len(data.dropped):
            out.append(f'<p>Showing the first {len(data.dropped):,}. The full list is in '
                       '.takeout-organizer/duplicates.md.</p>')
        out.append('<div class="pairs">')
        for dropped, kept in data.dropped:
            out.append(f'<div class="pair" role="group" aria-label="{_e(dropped.name)} '
                       f'and the kept copy"><div class="files">{_figure(dropped)}'
                       f'{_figure(kept)}</div></div>')
        out.append('</div>')
    else:
        out.append('<p>No duplicates were removed.</p>')
    out.append('</section>')

    # Photos by year
    out += ['<section id="years" aria-labelledby="years-h">',
            '<h2 id="years-h">Photos by year</h2>']
    if data.by_year:
        most = max(data.by_year.values()) or 1
        out += ['<table><caption>Photos and videos per year</caption><thead><tr>'
                '<th scope="col">Year</th><th scope="col" class="num">Files</th>'
                '<th scope="col">Share</th></tr></thead><tbody>']
        for year, count in sorted(data.by_year.items()):
            width = max(2, round(240 * count / most))
            out.append(f'<tr><td>{_e(year)}</td><td class="num">{count:,}</td>'
                       f'<td><span class="bar" style="width:{width}px" aria-hidden="true">'
                       f'</span></td></tr>')
        out.append('</tbody></table>')
    else:
        out.append('<p>No dated photos.</p>')
    out.append('</section>')

    # Albums
    out += ['<section id="albums" aria-labelledby="albums-h">',
            '<h2 id="albums-h">Albums</h2>']
    if data.albums:
        out.append(_table("Album playlists in Albums/", ("Album", "Files"),
                          sorted(data.albums.items()), numeric_last=True))
    else:
        out.append('<p>No albums.</p>')
    out.append('</section>')

    # Settings
    out += ['<section id="settings" aria-labelledby="settings-h">',
            '<h2 id="settings-h">Settings used</h2>', '<dl class="settings">']
    for name, value in data.settings:
        out.append(f'<dt>{_e(name)}</dt><dd>{_e(value)}</dd>')
    if data.log_file:
        out.append(f'<dt>Full log</dt><dd>{_e(data.log_file)}</dd>')
    out += ['</dl>', '</section>', '</main>']

    out += ['<footer id="help" aria-labelledby="help-h">', '<h2 id="help-h">Help</h2>',
            '<p>Your Takeout files were not changed. Photos are in folders by the date they '
            'were taken; files with only a guessed date are in <code>_undated/</code>, other '
            'files in <code>_other/</code>, and album playlists in <code>Albums/</code>. '
            'Running the same command again copies nothing twice.</p>',
            '<p>Documentation: '
            '<a href="https://github.com/regmibishal1/takeout-organizer#readme">'
            'Takeout Organizer README</a>.</p>',
            '</footer>', '</div>', '</body>', '</html>']
    return "\n".join(out) + "\n"


def _table(caption: str, headers: Tuple[str, str], rows, numeric_last: bool = False) -> str:
    num = ' class="num"' if numeric_last else ''
    lines = [f'<table><caption>{_e(caption)}</caption><thead><tr>'
             f'<th scope="col">{_e(headers[0])}</th>'
             f'<th scope="col"{num}>{_e(headers[1])}</th></tr></thead><tbody>']
    for first, second in rows:
        value = f"{second:,}" if numeric_last else _e(second)
        lines.append(f'<tr><td>{_e(first)}</td><td{num}>{value}</td></tr>')
    lines.append('</tbody></table>')
    return "".join(lines)


def write(path: str, data: ReportData) -> str:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(render(data))
    return path
