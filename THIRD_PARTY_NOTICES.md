# Third-party notices

Takeout Organizer is MIT licensed (see [LICENSE](LICENSE)). Installed with pip or pipx, it
uses the packages below as separate libraries. The standalone builds on the Releases page
include them, and each one's license text is in the build's `licenses` folder.

| Component | Used for | License |
|---|---|---|
| [ExifTool](https://exiftool.org/) by Phil Harvey | Reading and writing metadata (bundled in the builds) | Same terms as Perl: Artistic License or GPL |
| Strawberry Perl runtime (in the Windows ExifTool package) | Running ExifTool on Windows | Its components' licenses, in `Licenses_Strawberry_Perl.zip` |
| [Python](https://www.python.org/) | The runtime of the builds | PSF License |
| Tcl/Tk | The window | Tcl/Tk license (BSD style) |
| [Pillow](https://python-pillow.org/) | Reading images for duplicate checks and thumbnails | MIT-CMU (HPND) |
| [sv-ttk](https://github.com/rdbende/Sun-Valley-ttk-theme) | The window's light and dark theme | MIT |
| [pi-heif](https://github.com/bigcat88/pillow_heif) | Reading HEIC photos (decoding only) | BSD-3-Clause; its bundled libheif and libde265 are LGPL-3.0, notices in `LICENSES_bundled.txt` |
| [tzfpy](https://github.com/ringsaturn/tzfpy) | Time zones from GPS positions | MIT; the time zone boundaries come from [timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder), derived from OpenStreetMap data, (c) OpenStreetMap contributors, ODbL 1.0 |
| [tzdata](https://github.com/python/tzdata) | Time zone rules on Windows | Apache-2.0 |
| [GeoNames](https://www.geonames.org/) place names (in every install, `data/places.tsv`) | Town and city names for `--place-names` | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); adapted: towns and cities with over 15,000 people, positions rounded |
| [PyInstaller](https://pyinstaller.org/) | Making the standalone builds | GPL-2.0 with an exception that places no conditions on the builds |

The builds decode HEIC with pi-heif only and do not include an HEIC encoder.
