"""Capture times, time zones and dates found in file names.

Google's sidecar stores the capture moment as a UTC epoch. A photo needs it as
local wall-clock time plus an offset, and the right zone is the one where the
photo was taken, not the one of the computer running this tool.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional, Tuple

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .i18n import Text

# GPS position to time zone needs the optional extra:
# `pip install takeout-organizer[timezones]` (tzfpy, plus tzdata on Windows).
try:
    import tzfpy
except ImportError:  # pragma: no cover - optional
    tzfpy = None

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Camera and sidecar times count as the same moment within this many seconds.
AGREE_SECONDS = 2

# Real UTC offsets are whole multiples of 15 minutes, at most 14 hours.
ZONE_STEP_SECONDS = 15 * 60
MAX_ZONE_OFFSET_SECONDS = 14 * 60 * 60

_OFFSET_RE = re.compile(r'^(?:UTC|GMT)?\s*([+-])(\d{1,2}):?(\d{2})?$', re.IGNORECASE)


def has_location_lookup() -> bool:
    """True when GPS positions can be turned into time zones."""
    return tzfpy is not None


def parse_timezone(value: str) -> tzinfo:
    """A zone from user input: '+09:00', '-0530', 'UTC' or an IANA name.

    Raises ValueError with a message that says what to type instead.
    """
    text = value.strip()
    if text.upper() in ('UTC', 'Z', 'GMT'):
        return timezone.utc
    match = _OFFSET_RE.match(text)
    if match:
        sign, hours, minutes = match.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
        if delta.total_seconds() > MAX_ZONE_OFFSET_SECONDS:
            raise ValueError(Text("Time zone offset out of range: {zone}", zone=value))
        return timezone(-delta if sign == '-' else delta)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    raise ValueError(Text(
        "Unknown time zone: {zone}. Use an offset such as +09:00, or a name such as Asia/Tokyo "
        "(names need: pip install tzdata on Windows).", zone=value))


def zone_for_location(latitude: float, longitude: float) -> Optional[tzinfo]:
    """The time zone at a GPS position, or None without the optional extra."""
    if not has_location_lookup():
        return None
    try:
        name = tzfpy.get_tz(longitude, latitude)
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def to_local(timestamp: int, zone: Optional[tzinfo] = None) -> datetime:
    """A UTC epoch as an aware local datetime in zone (default: this computer's).

    Works for dates before 1970, where datetime.fromtimestamp fails on Windows.
    """
    moment = EPOCH + timedelta(seconds=timestamp)
    if zone is not None:
        return moment.astimezone(zone)
    try:
        return moment.astimezone()  # this computer's zone, daylight saving aware
    except (OSError, OverflowError, ValueError):
        # Windows cannot look up its zone rules before 1970; use today's offset.
        return moment.astimezone(datetime.now().astimezone().tzinfo)


def offset_text(moment: datetime) -> str:
    """'+09:00' style offset of an aware datetime."""
    seconds = int(moment.utcoffset().total_seconds())
    sign = '-' if seconds < 0 else '+'
    seconds = abs(seconds)
    return f"{sign}{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def exif_datestr(moment: datetime) -> str:
    return moment.strftime('%Y:%m:%d %H:%M:%S')


def parse_exif_date(value) -> Optional[datetime]:
    """Naive datetime from an EXIF/QuickTime date string; None if empty or zero."""
    text = str(value or '').strip()
    try:
        return datetime.strptime(text[:19], '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None


def parse_offset(value) -> Optional[timedelta]:
    """timedelta from an EXIF offset such as '+09:00'."""
    match = _OFFSET_RE.match(str(value or '').strip())
    if not match:
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
    return -delta if sign == '-' else delta


def _naive_epoch(moment: datetime) -> float:
    return (moment.replace(tzinfo=timezone.utc) - EPOCH).total_seconds()


def camera_offset(camera: datetime, offset: Optional[timedelta],
                  timestamp: int) -> Optional[timedelta]:
    """The offset under which a camera's local time is the sidecar's moment.

    With an offset tag, that offset if the two agree. Without one, the offset is
    inferred: the times must differ by a whole time-zone step (15 minutes). None
    when the camera date is a different moment (for example a date the user
    corrected in Google Photos).
    """
    diff = _naive_epoch(camera) - timestamp
    if offset is not None:
        return offset if abs(diff - offset.total_seconds()) <= AGREE_SECONDS else None
    if abs(diff) > MAX_ZONE_OFFSET_SECONDS + AGREE_SECONDS:
        return None
    steps = round(diff / ZONE_STEP_SECONDS)
    if abs(diff - steps * ZONE_STEP_SECONDS) > AGREE_SECONDS:
        return None
    return timedelta(seconds=steps * ZONE_STEP_SECONDS)


def utc_agrees(utc_naive: datetime, timestamp: int) -> bool:
    """A naive UTC time (QuickTime stores these) is the sidecar's moment."""
    return abs(_naive_epoch(utc_naive) - timestamp) <= AGREE_SECONDS


# Dates in camera, phone and app file names. Group order: y, m, d, H, M, S.
_FILENAME_PATTERNS = (
    # IMG_20230615_123456, VID_20230615_123456, PXL_20230615_123456789,
    # 20230615_123456, Screenshot_20230615-123456
    (re.compile(r'(?<!\d)((?:18|19|20)\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})'), True),
    # Screenshot_2023-06-15-12-34-56, 2023-06-15 12.34.56
    (re.compile(r'(?<!\d)((?:18|19|20)\d{2})-(\d{2})-(\d{2})[-_ ](\d{2})[-.](\d{2})[-.](\d{2})'),
     True),
    # WhatsApp: IMG-20230615-WA0001 (date only)
    (re.compile(r'(?<!\d)((?:18|19|20)\d{2})(\d{2})(\d{2})-WA\d+', re.IGNORECASE), False),
)

# Pixel phones name files by UTC time.
_UTC_NAME_PREFIXES = ('PXL_',)


def date_from_filename(name: str) -> Optional[Tuple[datetime, bool]]:
    """(naive datetime, is_utc) for a date in the file name, or None.

    Implausible values (before 1826, more than a day ahead) are ignored.
    """
    for pattern, has_time in _FILENAME_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        parts = [int(p) for p in match.groups()]
        if not has_time:
            parts += [0, 0, 0]
        try:
            moment = datetime(*parts)
        except ValueError:
            continue
        if moment.year < 1826 or _naive_epoch(moment) > time.time() + 24 * 60 * 60:
            continue
        return moment, name.upper().startswith(_UTC_NAME_PREFIXES)
    return None
