"""
Metadata Handler for Google Photos Takeout
Extracts metadata from JSON and applies it to media files using ExifTool
"""

import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone, tzinfo

from .exiftool import ExifToolProcess, argfile_line
from .i18n import Text
from .dates import (
    camera_offset, exif_datestr, offset_text, parse_exif_date, parse_offset, to_local,
    utc_agrees, zone_for_location,
)


# Earliest believable capture time: the first surviving photograph (1826).
_MIN_TIMESTAMP = int((datetime(1826, 1, 1, tzinfo=timezone.utc)
                      - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())

# A capture time this far past "now" is a broken value, not a clock skew.
_FUTURE_SLACK_SECONDS = 24 * 60 * 60

# ExifTool exits 0 with these warnings even though the tag was not written.
_SILENT_FAILURES = ("Error converting value", "Can't write")


def _quicktime_utc(value: str) -> str:
    """A QuickTime date as stored (UTC). Written with a zone, read back without."""
    try:
        moment = datetime.strptime(value, '%Y:%m:%d %H:%M:%S%z')
    except ValueError:
        return value
    return moment.astimezone(timezone.utc).strftime('%Y:%m:%d %H:%M:%S')


def _numbers(value: str) -> List[float]:
    """GPS coordinates written as 'lat, lon, alt' and read back as 'lat lon alt'."""
    return [float(part) for part in re.split(r'[ ,]+', value.strip()) if part]


def _without_zone(value: str) -> str:
    """'2023:06:15 23:30:45' from a date written with its zone; EXIF keeps
    the zone apart (OffsetTimeOriginal), XMP keeps it in the date."""
    return str(value)[:19]


def _magnitude(value: str) -> float:
    """A GPS coordinate without its sign: written as a magnitude plus N/S or
    E/W, read back signed from XMP."""
    return abs(float(value))


# After a write, what was written is read back to confirm it is in the file.
# Each field: its name in messages, the tags written for it, the tags it is
# read back from, and how values compare. A bare name matches the tag in any
# group, since ExifTool puts a value where the format allows (a GIF holds
# everything in XMP; WebP and HEIC have no IPTC). A field is there when every
# value written for it is found in one of its tags. File dates, deletions and
# the IPTC character set are not read back.
_FIELDS = (
    ('date', ('DateTimeOriginal', 'MWG:DateTimeOriginal'), ('DateTimeOriginal',),
     _without_zone),
    ('location', ('GPSLatitude',), ('GPSLatitude',), _magnitude),
    ('location', ('GPSLongitude',), ('GPSLongitude',), _magnitude),
    ('description', ('MWG:Description', 'Description', 'ImageDescription',
                     'Caption-Abstract'),
     ('Description', 'ImageDescription', 'Caption-Abstract'), 'text'),
    ('keywords', ('MWG:Keywords', 'Keywords', 'Subject'), ('Keywords', 'Subject'), 'text'),
    ('people', ('XMP-iptcExt:PersonInImage',), ('PersonInImage',), None),
    ('rating', ('Rating',), ('Rating',), float),
    # Videos: the QuickTime and Keys tags are written by group, read the same.
    ('date', ('QuickTime:CreateDate',), ('QuickTime:CreateDate',), _quicktime_utc),
    ('date', ('Keys:CreationDate',), ('Keys:CreationDate',), None),
    ('location', ('Keys:GPSCoordinates',), ('Keys:GPSCoordinates',), _numbers),
    ('description', ('Keys:Description',), ('Keys:Description',), None),
    ('rating', ('Keys:UserRating',), ('Keys:UserRating',), float),
)


def _written(args: List[str]) -> Dict[str, List[str]]:
    """The -Tag=value arguments of a write: tag name -> values (deletions,
    written as -Tag=, are left out)."""
    written: Dict[str, List[str]] = {}
    for arg in args:
        if arg.startswith('-') and '=' in arg:
            name, value = arg[1:].split('=', 1)
            if value != '':
                written.setdefault(name, []).append(value)
    return written


def read_back_names(args: List[str]) -> List[str]:
    """The tags to read back after a write with these arguments."""
    written = _written(args)
    names = []
    for _, sources, reads, _ in _FIELDS:
        if any(source in written for source in sources):
            names += [name for name in reads if name not in names]
    return names


def _same(written: str, found: Any, compare: Any) -> bool:
    if compare is None:
        return str(found) == written
    if compare == 'text':
        # IPTC limits lengths (captions 2000 bytes, keywords 64) and ExifTool
        # cuts longer values, so the start of the value is what can be there.
        # A character cut in half reads back as up to three dots.
        text = str(found)
        start = text.rstrip('.' + chr(0xFFFD))
        return text == written or (start != '' and len(text) - len(start) <= 3
                                   and written.startswith(start))
    try:
        a, b = compare(written), compare(str(found))
    except (TypeError, ValueError):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(abs(x - y) <= 1e-6 for x, y in zip(a, b))
    if isinstance(a, float):
        return abs(a - b) <= 1e-6
    return a == b


def _values(found: Dict[str, Any], name: str) -> List[Any]:
    """Every value read back for a tag: by group:name, or a bare name in any
    group. List tags (Keywords) give each item."""
    values: List[Any] = []
    for key, value in found.items():
        if key == name or (':' not in name and key.split(':', 1)[-1] == name):
            values += value if isinstance(value, list) else [value]
    return values


def missing_fields(args: List[str], found: Dict[str, Any]) -> List[str]:
    """The fields (date, location, ...) whose written values are not all in
    the file as read back."""
    written = _written(args)
    missing: List[str] = []
    for label, sources, reads, compare in _FIELDS:
        values = [value for source in sources for value in written.get(source, [])]
        have = [item for name in reads for item in _values(found, name)]
        if values and not all(any(_same(value, item, compare) for item in have)
                              for value in values):
            if label not in missing:
                missing.append(label)
    return missing


# Formats that cannot hold an EXIF block; their dates go in XMP.
_NO_EXIF = {'.gif'}

# Date policies: which date wins when Google's and the camera's disagree.
GOOGLE_FIRST = 'google-first'   # Google's (it reflects edits made in Google Photos)
CAMERA_FIRST = 'camera-first'   # the camera's, whenever the file has one
DATE_POLICIES = (GOOGLE_FIRST, CAMERA_FIRST)

# Embedded tags read before writing, so camera data is kept, not overwritten.
_EXISTING_TAGS = [
    '-DateTimeOriginal', '-SubSecTimeOriginal', '-OffsetTimeOriginal',
    '-Composite:GPSLatitude', '-Composite:GPSLongitude',
    '-QuickTime:CreateDate', '-Keys:CreationDate',
    '-CurrentIPTCDigest',   # present only when the file already has IPTC
]


def _local_datestr(timestamp: int) -> str:
    """A UTC epoch as EXIF local time on this computer (no offset)."""
    return exif_datestr(to_local(timestamp))


def _embedded_gps(tags: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """(latitude, longitude) when both are numbers and not 0,0. Cameras
    without a fix write 0,0, which means "no location", as in Google's JSON."""
    lat, lon = tags.get('GPSLatitude'), tags.get('GPSLongitude')
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if lat == 0 and lon == 0:
        return None
    return float(lat), float(lon)


def offset_text_of(offset) -> str:
    """'+09:00' style text for a timedelta offset."""
    return offset_text(datetime(2000, 1, 1, tzinfo=timezone(offset)))


def _with_offset(moment: datetime) -> str:
    """'2023:06:15 23:30:45+09:00': ExifTool turns this into the right instant
    whatever zone the computer is in."""
    return exif_datestr(moment) + offset_text(moment)


def bundled_exiftool() -> Optional[str]:
    """ExifTool inside a standalone build (PyInstaller), or None. The build
    keeps it in an exiftool folder next to the app's own files: exiftool.exe
    with its exiftool_files on Windows, the Perl script with its lib folder on
    macOS."""
    if not getattr(sys, 'frozen', False):
        return None
    for base in (getattr(sys, '_MEIPASS', None), os.path.dirname(sys.executable)):
        for name in ('exiftool.exe', 'exiftool'):
            path = os.path.join(base or '', 'exiftool', name)
            if not base or not os.path.isfile(path):
                continue
            # The macOS build ships the Perl script, which needs the system's
            # Perl; without it, an ExifTool installed otherwise (Homebrew) is used.
            if name == 'exiftool' and not (os.path.exists('/usr/bin/perl')
                                           or shutil.which('perl')):
                return None
            return path
    return None


def check_timestamp(raw: Any) -> Tuple[Optional[int], Optional[str]]:
    """Validate a sidecar epoch value.

    Returns (timestamp, None) when usable, or (None, reason) with reason one of
    'invalid', 'zero', 'too_old' or 'future'. Google writes 0 when it has no
    date; writing that would file the photo under 1970.
    """
    try:
        ts = int(float(raw))
    except (TypeError, ValueError):
        return None, 'invalid'
    if ts == 0:
        return None, 'zero'
    if ts < _MIN_TIMESTAMP:
        return None, 'too_old'
    if ts > time.time() + _FUTURE_SLACK_SECONDS:
        return None, 'future'
    return ts, None


def _writes_file_create_date() -> bool:
    """FileCreateDate exists only on Windows and macOS filesystems."""
    return os.name == 'nt' or sys.platform == 'darwin'


class MetadataHandler:
    """Handles metadata extraction and application using ExifTool"""

    _MISSING_MSG = Text(
        "ExifTool not found. Install it (Windows: winget install OliverBetz.ExifTool, "
        "macOS: brew install exiftool, Debian/Ubuntu: sudo apt install "
        "libimage-exiftool-perl) or pass --exiftool PATH.")

    def __init__(self, exiftool_path: Optional[str] = None, require: bool = True, *,
                 timezone: Optional[tzinfo] = None, date_policy: str = GOOGLE_FIRST,
                 overwrite_gps: bool = False, people_as_keywords: bool = False,
                 keep_open: bool = False):
        """
        Initialize metadata handler.

        Args:
            exiftool_path: Path to exiftool executable (if None, searches PATH).
            require: If True, raise when exiftool is absent. Pass False for
                dry-run/preview flows that never write metadata.
            timezone: Zone for capture times when neither GPS nor the camera
                gives one. None means this computer's zone.
            date_policy: GOOGLE_FIRST or CAMERA_FIRST, for when the sidecar
                date and the camera's date are different moments.
            overwrite_gps: Replace GPS already in the file with Google's.
            people_as_keywords: Also add people's names to the keywords.
            keep_open: Run every command through one long-running ExifTool
                process (about 30 times faster per file than starting
                ExifTool each time). Call close() when done.
        """
        self.exiftool_path = exiftool_path or self._find_exiftool()
        if date_policy not in DATE_POLICIES:
            raise ValueError(f"date_policy must be one of {DATE_POLICIES}")
        self.timezone = timezone
        self.date_policy = date_policy
        self.overwrite_gps = overwrite_gps
        self.people_as_keywords = people_as_keywords
        self._writable: Optional[set] = None  # extensions ExifTool can write
        self._process: Optional[ExifToolProcess] = (
            ExifToolProcess(self.exiftool_path) if keep_open and self.exiftool_path else None
        )

        if not self.exiftool_path and require:
            raise RuntimeError(self._MISSING_MSG)

    def _find_exiftool(self) -> Optional[str]:
        """The ExifTool a standalone build ships, else one on the PATH or in a
        usual place."""
        bundled = bundled_exiftool()
        if bundled:
            return bundled
        # Try to import settings for configured paths
        try:
            from .config import EXIFTOOL_SEARCH_PATHS, EXIFTOOL_PATH

            # If specific path is configured, try it first
            if EXIFTOOL_PATH and os.path.exists(EXIFTOOL_PATH):
                return EXIFTOOL_PATH

            # Try configured search paths
            for path in EXIFTOOL_SEARCH_PATHS:
                if os.path.exists(path):
                    return path

        except ImportError:
            pass  # Fall back to default search

        # Try different possible names in PATH
        possible_names = ['exiftool', 'exiftool.exe']

        for name in possible_names:
            if shutil.which(name):
                return name

        # Check common Windows installation paths
        if os.name == 'nt':
            common_paths = [
                r'C:\Windows\exiftool.exe',
                r'C:\Program Files\exiftool\exiftool.exe',
                r'C:\exiftool\exiftool.exe',
            ]

            for path in common_paths:
                if os.path.exists(path):
                    return path

        return None

    def extract_metadata_from_json(self, json_path: str) -> Optional[Dict[str, Any]]:
        """
        Extract metadata from Google Takeout JSON file

        Args:
            json_path: Path to JSON file

        Returns:
            Dictionary with extracted metadata or None if error. A rejected
            capture date is reported as 'date_rejected'; when only the upload
            date is usable it is returned as 'upload_timestamp'.
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            metadata = {}

            # Extract timestamp (most important field)
            taken = (data.get('photoTakenTime') or {}).get('timestamp')
            if taken is not None:
                timestamp, rejected = check_timestamp(taken)
                if timestamp is not None:
                    metadata['timestamp'] = timestamp
                else:
                    metadata['date_rejected'] = rejected

            # Fall back to the upload date. It can be years after capture, so
            # it is kept apart and never written as the capture date.
            if 'timestamp' not in metadata:
                uploaded = (data.get('creationTime') or {}).get('timestamp')
                if uploaded is not None:
                    timestamp, _ = check_timestamp(uploaded)
                    if timestamp is not None:
                        metadata['upload_timestamp'] = timestamp

            # Extract GPS coordinates. 0,0 means "no GPS"; Google then sometimes
            # still has the camera's own location in geoDataExif.
            for key in ('geoData', 'geoDataExif'):
                geo = data.get(key) or {}
                if geo.get('latitude', 0.0) != 0.0 or geo.get('longitude', 0.0) != 0.0:
                    metadata['latitude'] = geo.get('latitude')
                    metadata['longitude'] = geo.get('longitude')
                    metadata['altitude'] = geo.get('altitude')
                    break

            # Extract description
            if 'description' in data and data['description']:
                metadata['description'] = data['description']

            # Extract tags/keywords
            if 'tags' in data and data['tags']:
                metadata['keywords'] = data['tags']

            # Extract people (as keywords)
            if 'people' in data and data['people']:
                people_names = [person.get('name', '') for person in data['people'] if 'name' in person]
                if people_names:
                    metadata['people'] = people_names

            # Extract favorite status
            if 'favorited' in data and data['favorited']:
                metadata['rating'] = 5  # Map favorited to 5-star rating

            # Extract title
            if 'title' in data:
                metadata['title'] = data['title']

            return metadata

        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON file: {e}")
        except Exception as e:
            raise RuntimeError(f"Error extracting metadata: {e}")

    @staticmethod
    def _file_date_args(date_str: str) -> List[str]:
        """File system date tags (FileCreateDate only where it exists)."""
        args = [f'-FileModifyDate={date_str}']
        if _writes_file_create_date():
            args.append(f'-FileCreateDate={date_str}')
        return args

    def read_existing(self, media_path: str) -> Dict[str, Any]:
        """Dates, offset and GPS already embedded in a file (numeric values).

        QuickTime dates come back as stored, which is UTC. Empty on any error,
        which makes the caller write Google's values as before.
        """
        try:
            result = self._run_exiftool(
                ['-json', '-n', *_EXISTING_TAGS, os.path.abspath(media_path)]
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data:
                    return data[0]
        except Exception:
            pass
        return {}

    def _zone(self, metadata: Dict[str, Any], existing: Dict[str, Any]) -> Optional[tzinfo]:
        """Zone for a capture time: where it was taken (GPS), else the camera's
        offset, else the --timezone choice, else None (this computer)."""
        position = _embedded_gps(existing) or _embedded_gps(
            {'GPSLatitude': metadata.get('latitude'), 'GPSLongitude': metadata.get('longitude')}
        )
        if position is not None:
            zone = zone_for_location(*position)
            if zone is not None:
                return zone
        offset = parse_offset(existing.get('OffsetTimeOriginal'))
        if offset is not None:
            return timezone(offset)
        return self.timezone

    def capture_time(self, metadata: Dict[str, Any],
                     existing: Optional[Dict[str, Any]] = None) -> Optional[datetime]:
        """The sidecar capture moment as an aware local time, or None."""
        if 'timestamp' not in metadata:
            return None
        return to_local(metadata['timestamp'], self._zone(metadata, existing or {}))

    def position(self, metadata: Dict[str, Any],
                 existing: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        """Where the photo was taken, as the written file will say: the camera's
        location when it is kept, else Google's. 0,0 means none."""
        sidecar = _embedded_gps(
            {'GPSLatitude': metadata.get('latitude'), 'GPSLongitude': metadata.get('longitude')}
        )
        if self.overwrite_gps and sidecar is not None:
            return sidecar
        return _embedded_gps(existing) or sidecar

    def _has_gps(self, existing: Dict[str, Any]) -> bool:
        """The camera recorded a real location, which is kept."""
        return _embedded_gps(existing) is not None and not self.overwrite_gps

    def _people_tags(self, metadata: Dict[str, Any]) -> List[str]:
        """People's names in the IPTC field meant for them (PersonInImage)."""
        return [f'-XMP-iptcExt:PersonInImage={name}' for name in metadata.get('people', [])]

    def _keeps_camera_date(self, camera: datetime, metadata: Dict[str, Any],
                           existing: Dict[str, Any]) -> bool:
        """Whether a write keeps the camera's date instead of Google's: it is
        the same moment, or the date policy says the camera wins."""
        if self.date_policy == CAMERA_FIRST:
            return True
        offset = parse_offset(existing.get('OffsetTimeOriginal'))
        return camera_offset(camera, offset, metadata['timestamp']) is not None

    def predicted_capture(self, metadata: Dict[str, Any], existing: Dict[str, Any]
                          ) -> Tuple[Optional[datetime], Optional[str]]:
        """The local capture time a photo will carry after
        apply_metadata_to_image, and where it comes from ('sidecar' or
        'exif'), or (None, None). The plan files photos by this, so it must
        make the same choice as the write; both use _keeps_camera_date."""
        camera = parse_exif_date(existing.get('DateTimeOriginal'))
        if 'timestamp' not in metadata:
            return (camera, 'exif') if camera is not None else (None, None)
        if camera is not None and self._keeps_camera_date(camera, metadata, existing):
            return camera, 'exif'
        return self.capture_time(metadata, existing).replace(tzinfo=None), 'sidecar'

    def _image_date_tags(self, metadata: Dict[str, Any],
                         existing: Dict[str, Any]) -> List[str]:
        """Date tags for a photo, keeping a camera date that is the same moment."""
        camera = parse_exif_date(existing.get('DateTimeOriginal'))
        camera_tag_offset = parse_offset(existing.get('OffsetTimeOriginal'))

        if 'timestamp' not in metadata:
            if 'upload_timestamp' in metadata:
                # Only the upload date is known: too uncertain to write as the
                # capture date, so only the file times carry it.
                local = to_local(metadata['upload_timestamp'], self.timezone)
                return self._file_date_args(_with_offset(local))
            return []

        if camera is not None and self._keeps_camera_date(camera, metadata, existing):
            # Keep the camera's date: it has sub-seconds and the zone the
            # camera was set to. Add the offset when only it was missing.
            agreed = camera_offset(camera, camera_tag_offset, metadata['timestamp'])
            tags = []
            offset = camera_tag_offset if camera_tag_offset is not None else agreed
            if camera_tag_offset is None and agreed is not None:
                tags.append(f'-OffsetTimeOriginal={offset_text_of(agreed)}')
            file_date = exif_datestr(camera)
            if offset is not None:
                file_date += offset_text_of(offset)
            return tags + self._file_date_args(file_date)

        local = self.capture_time(metadata, existing)
        date_str = exif_datestr(local)
        offset = offset_text(local)
        # MWG writes the EXIF date, the XMP date with its zone, and the IPTC
        # date when the file already has IPTC, so every app reads the same.
        tags = [
            f'-MWG:DateTimeOriginal={date_str}{offset}',
            f'-MWG:CreateDate={date_str}{offset}',
            f'-OffsetTimeOriginal={offset}',
            f'-OffsetTimeDigitized={offset}',
        ]
        if existing.get('SubSecTimeOriginal') not in (None, ''):
            # The camera's sub-seconds belong to the date being replaced.
            tags += ['-SubSecTimeOriginal=', '-SubSecTimeDigitized=']
        return tags + self._file_date_args(date_str + offset)

    def apply_metadata_to_image(self, image_path: str, metadata: Dict[str, Any],
                                 preserve_original: bool = False,
                                 existing: Optional[Dict[str, Any]] = None) -> bool:
        """
        Apply metadata to image file using ExifTool

        Args:
            image_path: Path to image file
            metadata: Metadata dictionary from extract_metadata_from_json
            preserve_original: If True, keep original file as backup

        Returns:
            True if successful, False otherwise
        """
        try:
            options = []
            tags = []

            # Don't create backup unless requested
            if not preserve_original:
                options.append('-overwrite_original')

            has_gps = 'latitude' in metadata and 'longitude' in metadata
            has_text = any(key in metadata for key in ('description', 'keywords', 'people'))
            if existing is None:
                existing = (self.read_existing(image_path)
                            if 'timestamp' in metadata or has_gps or has_text else {})
            # Through MWG: EXIF, XMP, and IPTC only where the file has it already.
            options.extend(['-use', 'MWG'])

            # Capture time, in the zone where the photo was taken
            date_tags = self._image_date_tags(metadata, existing)
            mwg_date = next((t for t in date_tags if t.startswith('-MWG:DateTimeOriginal=')), None)
            if mwg_date and os.path.splitext(image_path)[1].lower() in _NO_EXIF:
                # No EXIF block: MWG would put the date only in XMP's
                # photoshop:DateCreated; apps also look for DateTimeOriginal.
                date_tags.append('-XMP-exif:DateTimeOriginal=' + mwg_date.split('=', 1)[1])
            tags.extend(date_tags)

            # GPS coordinates, unless the camera already recorded its own
            if has_gps and not self._has_gps(existing):
                lat = metadata['latitude']
                lon = metadata['longitude']

                # Set GPS coordinates
                tags.extend([
                    f'-GPSLatitude={abs(lat)}',
                    f'-GPSLatitudeRef={"N" if lat >= 0 else "S"}',
                    f'-GPSLongitude={abs(lon)}',
                    f'-GPSLongitudeRef={"E" if lon >= 0 else "W"}',
                ])

                alt = metadata.get('altitude')
                if alt is not None:
                    tags.extend([
                        f'-GPSAltitude={abs(alt)}',
                        f'-GPSAltitudeRef#={1 if alt < 0 else 0}',
                    ])

            # Description: EXIF ImageDescription, XMP dc:description, and the
            # IPTC caption only when the file already has IPTC.
            if 'description' in metadata:
                tags.append(f"-MWG:Description={metadata['description']}")

            # Keywords/Tags. People's names go in PersonInImage; as keywords
            # they would show up as tags, not people, in Immich and Lightroom.
            keywords = []
            if 'keywords' in metadata:
                keywords.extend(metadata['keywords'])
            if 'people' in metadata and self.people_as_keywords:
                keywords.extend(metadata['people'])
            tags.extend(self._people_tags(metadata))

            tags.extend(f'-MWG:Keywords={keyword}' for keyword in keywords)

            # IPTC text is Latin-1 unless the file says otherwise. Where the file
            # has IPTC (MWG then writes the caption and keywords there too), mark
            # it UTF-8 so non-Latin text survives; never add IPTC to a file.
            if 'description' in metadata or keywords:
                options.extend(['-charset', 'iptc=UTF8'])
                if existing.get('CurrentIPTCDigest'):
                    tags.append('-IPTC:CodedCharacterSet=UTF8')

            # Rating
            if 'rating' in metadata:
                tags.append(f'-Rating={metadata["rating"]}')

            if not tags:
                return True

            self._write(options + tags + [os.path.abspath(image_path)], read_back=True)
            return True

        except Exception as e:
            raise RuntimeError(f"Failed to apply metadata to image: {e}")

    def apply_metadata_to_video(self, video_path: str, metadata: Dict[str, Any],
                                 preserve_original: bool = False,
                                 existing: Optional[Dict[str, Any]] = None) -> bool:
        """
        Apply metadata to video file using ExifTool

        Args:
            video_path: Path to video file
            metadata: Metadata dictionary from extract_metadata_from_json
            preserve_original: If True, keep original file as backup

        Returns:
            True if successful, False otherwise
        """
        try:
            options = []
            tags = []

            # Don't create backup unless requested
            if not preserve_original:
                options.append('-overwrite_original')

            # Enable QuickTime UTC handling and large file support
            options.extend([
                '-api', 'QuickTimeUTC=1',
                '-api', 'LargeFileSupport=1'
            ])

            has_gps = 'latitude' in metadata and 'longitude' in metadata
            if existing is None:
                existing = (self.read_existing(video_path)
                            if 'timestamp' in metadata or has_gps else {})

            # Capture time. QuickTime stores UTC; QuickTimeUTC=1 makes ExifTool
            # convert the zoned value below, so the computer's zone plays no
            # part. Keys:CreationDate keeps the local time and zone (Apple
            # Photos reads it first).
            if 'timestamp' in metadata:
                local = self.capture_time(metadata, existing)
                stored = parse_exif_date(existing.get('CreateDate'))
                agrees = stored is not None and utc_agrees(stored, metadata['timestamp'])
                keep = agrees or (stored is not None and self.date_policy == CAMERA_FIRST)
                date_str = _with_offset(local)

                if not keep:
                    tags.extend([
                        f'-QuickTime:CreateDate={date_str}',
                        f'-TrackCreateDate={date_str}',
                        f'-MediaCreateDate={date_str}',
                    ])
                if not keep or (agrees and not existing.get('CreationDate')):
                    tags.append(f'-Keys:CreationDate={date_str}')
                if not keep or agrees:
                    tags.extend(self._file_date_args(date_str))
            elif 'upload_timestamp' in metadata:
                local = to_local(metadata['upload_timestamp'], self.timezone)
                tags.extend(self._file_date_args(_with_offset(local)))

            # GPS coordinates (QuickTime format), unless the camera recorded
            # its own. ExifTool only accepts the comma-separated form once an
            # altitude is appended.
            if has_gps and not self._has_gps(existing):
                lat = metadata['latitude']
                lon = metadata['longitude']
                alt = metadata.get('altitude')

                gps_coords = f"{lat}, {lon}"
                if alt:
                    gps_coords += f", {alt}"

                tags.append(f'-Keys:GPSCoordinates={gps_coords}')

            # Description
            if 'description' in metadata:
                desc = metadata['description']
                tags.append(f'-Keys:Description={desc}')

            # Rating
            if 'rating' in metadata:
                tags.append(f'-Keys:UserRating={metadata["rating"]}')

            if not tags:
                return True

            self._write(options + tags + [os.path.abspath(video_path)], read_back=True)
            return True

        except Exception as e:
            raise RuntimeError(f"Failed to apply metadata to video: {e}")

    def write_name_date(self, media_path: str, local: datetime, is_image: bool) -> None:
        """Write a date read from the file's name (IMG_20190704_101500.jpg) as
        its capture date, for a file that has none, so photo apps show it on
        that day and not on the day it was copied. A name gives the camera's
        clock, not its zone, so a photo gets no offset; a video stores UTC, so
        its time is taken in --timezone or this computer's zone."""
        if is_image:
            # Through MWG, like every photo write: ExifTool keeps a module it
            # loaded for later commands, so each write says what it uses.
            date_str = exif_datestr(local)
            args = ['-overwrite_original', '-use', 'MWG', f'-MWG:DateTimeOriginal={date_str}',
                    f'-MWG:CreateDate={date_str}', *self._file_date_args(date_str)]
        else:
            zoned = _with_offset(local.replace(tzinfo=self.timezone) if self.timezone
                                 else local.astimezone())
            args = ['-overwrite_original', '-api', 'QuickTimeUTC=1',
                    '-api', 'LargeFileSupport=1', f'-QuickTime:CreateDate={zoned}',
                    f'-Keys:CreationDate={zoned}', *self._file_date_args(zoned)]
        self._write(args + [os.path.abspath(media_path)], read_back=True)

    def copy_tags(self, source: str, target: str) -> None:
        """Copy every tag of source into target (a JPG copy of a HEIC) except
        the orientation: the copy's pixels are upright already. Raises when
        the date did not reach the copy."""
        self._write(['-overwrite_original', '-tagsFromFile', os.path.abspath(source),
                     '-all:all', '--Orientation', os.path.abspath(target)])
        names = ['DateTimeOriginal']
        wanted = self.read_back(os.path.abspath(source), names)
        found = self.read_back(os.path.abspath(target), names)
        if any(key.endswith(':DateTimeOriginal') for key in wanted) and \
                not any(key.endswith(':DateTimeOriginal') for key in found):
            raise RuntimeError("the date did not reach the JPG copy")

    def clone(self) -> 'MetadataHandler':
        """A handler with the same settings and its own long-running ExifTool
        process, for another thread: a handler is used by one thread at a
        time. Call close() on it when done."""
        other = MetadataHandler(self.exiftool_path, timezone=self.timezone,
                                date_policy=self.date_policy, overwrite_gps=self.overwrite_gps,
                                people_as_keywords=self.people_as_keywords, keep_open=True)
        other._writable = self._writable
        return other

    def close(self) -> None:
        """Stop the long-running ExifTool process, if one was started."""
        if self._process is not None:
            self._process.close()

    def _run_exiftool(self, args: List[str]) -> subprocess.CompletedProcess:
        """Run one ExifTool command.

        With keep_open, it goes to the long-running process over stdin.
        Otherwise ExifTool is started with the arguments in a UTF-8 file.

        On Windows, command-line arguments reach ExifTool in the ANSI code page,
        which turns non-ASCII captions, names and file paths into '?'. An
        argument file is read as UTF-8 on every platform. The file is named
        relative to ExifTool's working directory, so only its plain ASCII name
        crosses the command line, whatever the temp folder is called. File
        paths in args must therefore be absolute.
        """
        if self._process is not None:
            return self._process.execute(args)

        fd, argfile = tempfile.mkstemp(prefix='exiftool_', suffix='.args')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                for arg in args:
                    f.write(argfile_line(arg) + '\n')
            return subprocess.run(
                [self.exiftool_path, '-charset', 'filename=utf8',
                 '-@', os.path.basename(argfile)],
                cwd=os.path.dirname(argfile),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=False
            )
        finally:
            os.remove(argfile)

    def can_write(self, media_path: str) -> bool:
        """Whether ExifTool can write this file type (its -listwf list, read once)."""
        if not self.exiftool_path:
            return True  # no ExifTool (a preview): assume the write will be tried
        if self._writable is None:
            result = self._run_exiftool(['-listwf'])
            words = result.stdout.split() if result.returncode == 0 else []
            self._writable = {w.lower() for w in words if w.isalnum() and w.isupper()}
        if not self._writable:
            return True  # list unavailable: let the write itself decide
        return os.path.splitext(media_path)[1].lstrip('.').lower() in self._writable

    def write_xmp_sidecar(self, xmp_path: str, metadata: Dict[str, Any]) -> bool:
        """Put the metadata in `name.ext.xmp` next to a file ExifTool cannot
        write (BMP, AVI, MKV, ...). digiKam, darktable and Immich read these.
        Returns False when there is nothing to put in it."""
        tags = []
        local = self.capture_time(metadata)
        if local is not None:
            zoned = _with_offset(local)
            tags += [f'-XMP-exif:DateTimeOriginal={zoned}',
                     f'-XMP-photoshop:DateCreated={zoned}',
                     f'-XMP-xmp:CreateDate={zoned}']
        if 'latitude' in metadata and 'longitude' in metadata:
            tags += [f'-XMP-exif:GPSLatitude={metadata["latitude"]}',
                     f'-XMP-exif:GPSLongitude={metadata["longitude"]}']
            if metadata.get('altitude') is not None:
                alt = metadata['altitude']
                tags += [f'-XMP-exif:GPSAltitude={abs(alt)}',
                         f'-XMP-exif:GPSAltitudeRef#={1 if alt < 0 else 0}']
        if 'description' in metadata:
            tags.append(f'-XMP-dc:Description={metadata["description"]}')
        tags += [f'-XMP-dc:Subject={keyword}' for keyword in metadata.get('keywords', [])]
        if self.people_as_keywords:
            tags += [f'-XMP-dc:Subject={name}' for name in metadata.get('people', [])]
        tags += self._people_tags(metadata)
        if 'rating' in metadata:
            tags.append(f'-XMP-xmp:Rating={metadata["rating"]}')
        if not tags:
            return False
        self._write(['-overwrite_original'] + tags + [os.path.abspath(xmp_path)])
        return True

    def _write(self, args: List[str], read_back: bool = False) -> None:
        """Run a write and raise if ExifTool failed, including the warnings it
        exits 0 for while leaving the tag unwritten. With read_back, the tags
        written are read from the file afterwards, and any that is not there
        as written is an error too: ExifTool can report success for a value
        it did not store."""
        result = self._run_exiftool(args)

        if result.returncode != 0:
            raise RuntimeError(f"ExifTool error: {result.stderr.strip()}")

        silent = [line.strip() for line in (result.stderr or '').splitlines()
                  if any(marker in line for marker in _SILENT_FAILURES)]
        if silent:
            raise RuntimeError(f"ExifTool did not write: {'; '.join(silent)}")

        names = read_back_names(args) if read_back else []
        if names:
            missing = missing_fields(args, self.read_back(args[-1], names))
            if missing:
                raise RuntimeError("ExifTool reported success, but the file does not hold "
                                   f"what was written for: {', '.join(missing)}")

    def read_back(self, media_path: str, names: List[str]) -> Dict[str, Any]:
        """Tags as stored in the file (numeric values, QuickTime dates in UTC),
        by group:name. Empty when the file cannot be read."""
        result = self._run_exiftool(['-json', '-n', '-G1', '-api', 'LargeFileSupport=1',
                                     *[f'-{name}' for name in names], media_path])
        try:
            data = json.loads(result.stdout) if result.returncode == 0 else []
        except ValueError:
            data = []
        return data[0] if data else {}

    def apply_metadata(self, media_path: str, json_path: str,
                       is_image: bool, preserve_original: bool = False) -> bool:
        """
        Apply metadata from JSON to media file

        Args:
            media_path: Path to media file
            json_path: Path to JSON metadata file
            is_image: True if media is image, False if video
            preserve_original: If True, keep original file as backup

        Returns:
            True if successful, False otherwise
        """
        # Extract metadata from JSON
        metadata = self.extract_metadata_from_json(json_path)

        if not metadata:
            raise ValueError("No metadata extracted from JSON")

        # Apply to image or video
        if is_image:
            return self.apply_metadata_to_image(media_path, metadata, preserve_original)
        else:
            return self.apply_metadata_to_video(media_path, metadata, preserve_original)

    def verify_metadata(self, media_path: str) -> Dict[str, Any]:
        """
        Verify metadata was applied to a file

        Args:
            media_path: Path to media file

        Returns:
            Dictionary with metadata information
        """
        try:
            result = self._run_exiftool([
                '-DateTimeOriginal',
                '-GPSLatitude',
                '-GPSLongitude',
                '-Description',
                '-Rating',
                '-json',
                os.path.abspath(media_path)
            ])

            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data:
                    return data[0]

            return {}

        except Exception:
            return {}

    def get_exiftool_version(self) -> str:
        """Get ExifTool version"""
        try:
            result = subprocess.run(
                [self.exiftool_path, '-ver'],
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode == 0:
                return result.stdout.strip()

            return "Unknown"

        except Exception:
            return "Unknown"
