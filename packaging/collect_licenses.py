"""Gather the license notices of everything a standalone build ships into one
folder, build/licenses, which the PyInstaller spec puts in the build.

Usage (from the repository root, in the environment the build is made from):
    python packaging/collect_licenses.py EXIFTOOL_DIR
"""

from __future__ import annotations

import os
import shutil
import sys
import tkinter
from importlib import metadata

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
OUT = os.path.join(ROOT, 'build', 'licenses')

# Python packages the build ships (their own license files, from their metadata).
PACKAGES = ('pillow', 'sv-ttk', 'tzfpy', 'tzdata', 'pi-heif')

_NOTICE_WORDS = ('license', 'licence', 'copying', 'notice', 'authors')

# GeoNames ships no license file; this is the attribution CC BY 4.0 asks for.
GEONAMES_NOTICE = """\
The place names in takeout_organizer/data/places.tsv come from GeoNames
(https://www.geonames.org/), file cities15000.zip from
https://download.geonames.org/export/dump/.

GeoNames data is licensed under the Creative Commons Attribution 4.0
International License (CC BY 4.0): https://creativecommons.org/licenses/by/4.0/

Changes: only towns and cities with over 15,000 people are kept, without
parts of cities; positions are rounded to 3 decimals; names with two forms
("Biel/Bienne") keep the first. The data is provided as is, without
warranties of any kind.
"""


def _copy(source: str, folder: str) -> None:
    target = os.path.join(OUT, folder)
    os.makedirs(target, exist_ok=True)
    shutil.copy2(source, os.path.join(target, os.path.basename(source)))


def collect(exiftool_dir: str) -> list:
    shutil.rmtree(OUT, ignore_errors=True)
    found = []
    for package in PACKAGES:
        dist = metadata.distribution(package)
        for file in dist.files or []:
            name = str(file).lower()
            if any(word in name for word in _NOTICE_WORDS) and \
                    os.path.isfile(str(file.locate())):
                _copy(str(file.locate()), package)
                found.append(f"{package}: {file}")
    # The Python runtime, and Tcl/Tk (the window).
    for name in ('LICENSE.txt', 'LICENSE'):
        path = os.path.join(sys.base_prefix, name)
        if os.path.isfile(path):
            _copy(path, 'python')
            found.append(f"python: {name}")
            break
    tcl_library = tkinter.Tcl().eval('info library')
    for library, folder in ((tcl_library, 'tcl'),
                            (tcl_library.replace('tcl8', 'tk8').replace('tcl9', 'tk9'), 'tk')):
        path = os.path.join(library, 'license.terms')
        if os.path.isfile(path):
            _copy(path, folder)
            found.append(f"{folder}: license.terms")
    # The GeoNames place names (--place-names).
    os.makedirs(os.path.join(OUT, 'geonames'), exist_ok=True)
    with open(os.path.join(OUT, 'geonames', 'NOTICE.txt'), 'w', encoding='utf-8') as f:
        f.write(GEONAMES_NOTICE)
    found.append("geonames: NOTICE.txt")
    # ExifTool, with the Perl runtime it carries on Windows.
    for base, _, files in os.walk(exiftool_dir):
        for name in files:
            if any(word in name.lower() for word in _NOTICE_WORDS) \
                    or name.lower().startswith('readme'):
                _copy(os.path.join(base, name), 'exiftool')
                found.append(f"exiftool: {name}")
        if base != exiftool_dir:
            break   # the top folder and one level down (exiftool_files) are enough
    return found


if __name__ == '__main__':
    for line in collect(sys.argv[1]):
        print(line)
