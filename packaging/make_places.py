"""Make src/takeout_organizer/data/places.tsv from GeoNames' cities15000.zip.

The file holds every town and city with more than 15,000 people: its position
(3 decimals, about 110 m), population and name. --place-names names folders
after them (see places.py). Parts of a city (GeoNames feature code PPLX, such
as "Times Square") are left out, so a folder is named after the city.

Refreshing the file is a deliberate step, not part of a build: download
cities15000.zip from https://download.geonames.org/export/dump/ and run

    python packaging/make_places.py cities15000.zip

GeoNames data is CC BY 4.0 (geonames.org); this file is an adaptation of it.
The names are UTF-8, so the file is exempt from the repository's ASCII rule.
"""

from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from datetime import date

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
OUT = os.path.join(ROOT, 'src', 'takeout_organizer', 'data', 'places.tsv')

# Characters a folder name cannot hold on Windows, macOS or Linux.
_NOT_IN_NAMES = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder_name(name: str) -> str:
    """The name as a folder can hold it. Names with two forms, such as
    "Biel/Bienne", keep the first."""
    name = name.split('/')[0].strip(' .')
    if not name or _NOT_IN_NAMES.search(name):
        raise SystemExit(f"Cannot use the place name {name!r}; update make_places.py.")
    return name


def make(zip_path: str) -> int:
    rows = set()
    with zipfile.ZipFile(zip_path) as zf:
        text = io.TextIOWrapper(zf.open('cities15000.txt'), encoding='utf-8')
        for line in text:
            fields = line.rstrip('\n').split('\t')
            name, lat, lon, feature, population = (fields[1], fields[4], fields[5],
                                                   fields[7], fields[14])
            if feature == 'PPLX':
                continue
            rows.add((round(float(lat), 3), round(float(lon), 3), int(population or 0),
                      folder_name(name)))
    header = [
        "# Place names from GeoNames (geonames.org), CC BY 4.0. Adapted: towns and cities",
        "# with over 15,000 people, without parts of cities; latitude, longitude (3 decimals),",
        "# population, name.",
        f"# Made from cities15000.zip on {date.today().isoformat()}: {len(rows)} places.",
        "# Regenerate with packaging/make_places.py.",
    ]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(header) + '\n')
        for lat, lon, population, name in sorted(rows):
            f.write(f"{lat}\t{lon}\t{population}\t{name}\n")
    return len(rows)


if __name__ == '__main__':
    print(f"{make(sys.argv[1])} places written to {OUT}")
