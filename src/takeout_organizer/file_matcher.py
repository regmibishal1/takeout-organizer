"""
File Matcher for Google Photos Takeout
Handles complex matching patterns between media files and JSON metadata files
"""

import json
from collections import Counter
import os
import re
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Iterator
import unicodedata

# Since late 2024 Takeout names sidecars `IMG.jpg.supplemental-metadata.json`,
# cutting the suffix short when the name gets long (`IMG.jpg.supplemental-me.json`).
SUPPLEMENTAL = 'supplemental-metadata'

# Google caps the sidecar's base name at this many characters (older exports).
TRUNCATED_NAME_LENGTH = 46

# Match rules, strongest first. A sidecar is used by one file only, except
# through the shared rules: an edited copy shares its original's sidecar, and
# the video half of a Live or motion photo shares the still's.
RULES = ('exact', 'supplemental', 'counter', 'no-extension', 'alt-extension',
         'truncated', 'edited', 'live', 'title-index')
SHARED_RULES = {'edited', 'live'}

# Name rules weak enough that the sidecar's `title` must confirm the match. The
# exact-name rules are left alone: Google's naming there is unambiguous.
TITLE_CHECKED_RULES = {'no-extension', 'alt-extension', 'truncated', 'edited', 'live',
                       'title-index'}

# Google's own JSON files that are not sidecars.
NOT_SIDECARS = {'metadata.json', 'print-subscriptions.json', 'shared_album_comments.json',
                'user-generated-memory-titles.json'}

# Extensions that name the same format.
_SAME_EXTENSION = {'.jpeg': '.jpg', '.jpe': '.jpg', '.tiff': '.tif', '.heif': '.heic'}

# Characters a title may contain but a file name may not; Takeout writes '_'.
_UNSAFE_IN_NAMES = re.compile(r'[\\/:*?"<>|]')


class FileMatcher:
    """Matches media files with their corresponding JSON metadata files"""

    # Supported media extensions. Camera RAW and AVIF are images; .MP and .MV are
    # the video halves of Pixel motion photos; .mts/.m2ts come from camcorders.
    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif', '.webp', '.avif',
                        '.tiff', '.tif', '.bmp', '.dng', '.raw', '.cr2', '.cr3', '.nef',
                        '.arw', '.orf', '.rw2'}
    VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.mpg', '.mpeg', '.m4v', '.webm', '.wmv',
                        '.3gp', '.flv', '.mp', '.mv', '.mts', '.m2ts'}

    # Edit suffixes in different languages
    EDIT_SUFFIXES = ['-edited', '-modifi\u00e9', '-bearbeitet', '-modificado', '-modificato']

    def __init__(self, case_sensitive: bool = False):
        self.case_sensitive = case_sensitive
        # directory -> {normalized filename: real filename}, so each directory
        # is listed once instead of once per sidecar miss.
        self._dir_index: Dict[str, Dict[str, str]] = {}
        self._titles: Dict[str, Optional[str]] = {}  # sidecar path -> its title

    def normalize_filename(self, filename: str) -> str:
        """Normalize filename for comparison (handle Unicode, case, etc.)"""
        # Normalize Unicode (NFC vs NFD)
        filename = unicodedata.normalize('NFC', filename)

        if not self.case_sensitive:
            filename = filename.lower()

        return filename

    def is_media_file(self, filepath: str) -> bool:
        """Check if file is a media file (image or video)"""
        ext = Path(filepath).suffix.lower()
        return ext in self.IMAGE_EXTENSIONS or ext in self.VIDEO_EXTENSIONS

    def is_image_file(self, filepath: str) -> bool:
        """Check if file is an image"""
        ext = Path(filepath).suffix.lower()
        return ext in self.IMAGE_EXTENSIONS

    def is_video_file(self, filepath: str) -> bool:
        """Check if file is a video"""
        ext = Path(filepath).suffix.lower()
        return ext in self.VIDEO_EXTENSIONS

    def find_json_for_media(self, media_path: str, directory: Optional[str] = None) -> Optional[str]:
        """
        Find matching JSON file for a media file using multiple strategies

        Args:
            media_path: Path to media file
            directory: Optional directory to search in (defaults to media file's directory)

        Returns:
            Path to matching JSON file or None
        """
        found = self.find_json_with_rule(media_path, directory)
        return found[0] if found else None

    def find_json_with_rule(self, media_path: str,
                            directory: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """(sidecar path, rule name) for the first rule that finds a sidecar
        the title check (where it applies) accepts, or None.

        There is deliberately no prefix ("fuzzy") matching: IMG_7.jpg must
        never pick up IMG_77.jpg.json. Another photo's date written into a
        file is worse than no date.
        """
        if directory is None:
            directory = os.path.dirname(media_path)
        filename = os.path.basename(media_path)

        for candidate, rule in self._candidates(filename):
            json_path = self._try_json_path(directory, candidate)
            if json_path and (rule not in TITLE_CHECKED_RULES
                              or self._title_matches(json_path, filename)):
                return json_path, rule

        # Live Photos and motion photos: IMG_1234.MOV shares the sidecar of
        # IMG_1234.HEIC; PXL_x.MP shares the one of PXL_x.MP.jpg.
        if self.is_video_file(media_path):
            potential_photo = self._find_live_photo_pair(directory, filename)
            if potential_photo:
                found = self.find_json_with_rule(potential_photo, directory)
                if found:
                    return found[0], 'live'

        return None

    def _candidates(self, filename: str) -> Iterator[Tuple[str, str]]:
        """Sidecar names to look for, with the rule each one belongs to."""
        name_no_ext, ext = os.path.splitext(filename)

        # photo.jpg.json, then the newer photo.jpg.supplemental-metadata.json
        # (full or cut short), then photo.json.
        yield f"{filename}.json", 'exact'
        for suffix in self._supplemental_suffixes():
            yield f"{filename}.{suffix}.json", 'supplemental'

        # Duplicate counter moved: photo(1).jpg -> photo.jpg(1).json, or
        # photo.jpg.supplemental-metadata(1).json in newer exports.
        duplicate_match = re.match(r'(.+)\((\d+)\)(\.[^.]+)$', filename)
        if duplicate_match:
            base_name, number, extension = duplicate_match.groups()
            original = f"{base_name}{extension}"
            yield f"{original}({number}).json", 'counter'
            for suffix in self._supplemental_suffixes():
                yield f"{original}.{suffix}({number}).json", 'counter'

        yield f"{name_no_ext}.json", 'no-extension'

        # Extension variations: photo.jpeg -> photo.jpg.json
        for alt_ext in self._get_alternative_extensions(ext):
            yield f"{name_no_ext}{alt_ext}.json", 'alt-extension'

        # Truncated JSON names: Google cuts long names.
        if len(filename) > TRUNCATED_NAME_LENGTH:
            yield f"{filename[:TRUNCATED_NAME_LENGTH]}.json", 'truncated'

        # Edited variants share the original's sidecar, both ways round:
        # photo-edited.jpg -> photo.jpg.json, photo.jpg -> photo-edited.jpg.json
        for suffix in self.EDIT_SUFFIXES:
            if name_no_ext.lower().endswith(suffix):
                original = f"{name_no_ext[: -len(suffix)]}{ext}"
                yield f"{original}.json", 'edited'
                for sup in self._supplemental_suffixes():
                    yield f"{original}.{sup}.json", 'edited'
                yield f"{os.path.splitext(original)[0]}.json", 'edited'
        for suffix in self.EDIT_SUFFIXES:
            yield f"{name_no_ext}{suffix}{ext}.json", 'edited'

    @staticmethod
    def _supplemental_suffixes() -> Iterator[str]:
        """`supplemental-metadata`, then every shorter cut down to `s`."""
        for length in range(len(SUPPLEMENTAL), 0, -1):
            yield SUPPLEMENTAL[:length]

    def title_of(self, json_path: str) -> Optional[str]:
        """The sidecar's `title` (the original file name), read once."""
        if json_path not in self._titles:
            title = None
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('title'), str):
                    title = data['title']
            except (OSError, ValueError):
                pass
            self._titles[json_path] = title
        return self._titles[json_path]

    def _name_key(self, name: str) -> Tuple[str, str]:
        """(stem, extension) of a file name or title, comparable across the
        ways Takeout varies them: Unicode form, case, characters not allowed
        in file names, a duplicate counter, an edit suffix, jpg/jpeg."""
        name = _UNSAFE_IN_NAMES.sub('_', self.normalize_filename(name))
        stem, ext = os.path.splitext(name)
        stem = re.sub(r'\(\d+\)$', '', stem)
        for suffix in self.EDIT_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        return stem, _SAME_EXTENSION.get(ext, ext)

    def _title_matches(self, json_path: str, filename: str) -> bool:
        """False only when the sidecar's title names a different file.

        A missing title proves nothing either way and is accepted. Names cut
        short by Google still match when one is the start of the other and
        long enough (30 characters) that this cannot be a different photo.
        """
        title = self.title_of(json_path)
        if not title:
            return True
        title_stem, title_ext = self._name_key(title)
        stem, ext = self._name_key(filename)
        if title_ext and ext and title_ext != ext:
            return False
        if title_stem == stem:
            return True
        shorter, longer = sorted((title_stem, stem), key=len)
        return len(shorter) >= 30 and longer.startswith(shorter)

    def _try_json_path(self, directory: str, json_filename: str) -> Optional[str]:
        """Find a file in directory, handling case insensitivity.

        Returns the path with the file's real name as listed on disk, never the
        spelling asked for (case-insensitive filesystems accept either).
        """
        index = self._index(directory)
        if self.case_sensitive:
            real_name = json_filename if json_filename in index.values() else None
        else:
            real_name = index.get(self.normalize_filename(json_filename))
        return os.path.join(directory, real_name) if real_name else None

    def _index(self, directory: str) -> Dict[str, str]:
        """Normalized-name index of a directory, built on first use."""
        index = self._dir_index.get(directory)
        if index is None:
            # scandir knows each entry's type from the listing itself; a
            # separate isfile() per name is a disk lookup per file on Windows.
            try:
                with os.scandir(directory) as entries:
                    names = [e.name for e in entries if e.is_file()]
            except OSError:
                names = []
            index = {self.normalize_filename(name): name for name in names}
            self._dir_index[directory] = index
        return index

    def _get_alternative_extensions(self, ext: str) -> List[str]:
        """Get alternative extensions for a given extension"""
        ext_lower = ext.lower()

        alternatives = {
            '.jpg': ['.jpeg', '.JPG', '.JPEG'],
            '.jpeg': ['.jpg', '.JPG', '.JPEG'],
            '.heic': ['.HEIF', '.heif'],
            '.tif': ['.tiff', '.TIF', '.TIFF'],
            '.tiff': ['.tif', '.TIF', '.TIFF'],
        }

        return alternatives.get(ext_lower, [])

    def photo_of_video(self, video_path: str) -> Optional[str]:
        """The photo half of a Live or motion photo whose video this is, in the
        same folder, or None."""
        if not self.is_video_file(video_path):
            return None
        return self._find_live_photo_pair(os.path.dirname(video_path),
                                          os.path.basename(video_path))

    def _find_live_photo_pair(self, directory: str, video_filename: str) -> Optional[str]:
        """
        Find the photo component of a Live Photo pair
        iOS Live Photos: IMG_1234.HEIC (photo) + IMG_1234.MOV (video)
        Sometimes exported as: IMG_1234.JPG + IMG_1234(1).JPG
        """
        base_name = Path(video_filename).stem

        # Remove (1) suffix if present
        base_name = re.sub(r'\(\d+\)$', '', base_name)

        # Try common photo extensions, on the stem (IMG_1234.MOV -> IMG_1234.HEIC)
        # and on the full name (Pixel: PXL_x.MP -> PXL_x.MP.jpg)
        for stem in (base_name, video_filename):
            for photo_ext in ['.heic', '.HEIC', '.jpg', '.JPG', '.jpeg', '.JPEG']:
                photo_path = self._try_json_path(directory, f"{stem}{photo_ext}")
                if photo_path:
                    return photo_path

        return None

    def find_all_media_files(self, directory: str, recursive: bool = True) -> List[str]:
        """
        Find all media files in directory

        Args:
            directory: Directory to search
            recursive: Search subdirectories

        Returns:
            List of media file paths
        """
        media_files = []

        if recursive:
            for root, dirs, files in os.walk(directory):
                # Skip hidden directories and common non-photo directories
                dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() not in ['archive_browser.html']]

                for file in files:
                    if self.is_media_file(file):
                        full_path = os.path.join(root, file)
                        media_files.append(full_path)
        else:
            try:
                for file in os.listdir(directory):
                    if self.is_media_file(file):
                        full_path = os.path.join(directory, file)
                        if os.path.isfile(full_path):
                            media_files.append(full_path)
            except Exception:
                pass

        return media_files

    def find_all_json_files(self, directory: str, recursive: bool = True) -> List[str]:
        """Find all JSON files in directory"""
        json_files = []

        if recursive:
            for root, dirs, files in os.walk(directory):
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if file.lower().endswith('.json'):
                        full_path = os.path.join(root, file)
                        json_files.append(full_path)
        else:
            try:
                for file in os.listdir(directory):
                    if file.lower().endswith('.json'):
                        full_path = os.path.join(directory, file)
                        if os.path.isfile(full_path):
                            json_files.append(full_path)
            except Exception:
                pass

        return json_files

    def match_files(self, directory: str) -> Tuple[List[Dict], List[str], List[str]]:
        """
        Match all media files with their JSON files in a directory

        Each media file proposes its best sidecar; proposals are granted
        strongest rule first, and a sidecar goes to one file only unless it
        is shared through an edited copy or a Live/motion photo video. Files
        still without one may then find it anywhere in the export by its
        title, when exactly one unused sidecar has that title.

        Returns:
            Tuple of (matched_pairs, unmatched_media, unmatched_json)
            matched_pairs: List of dicts with 'media', 'json' and 'rule' keys
            unmatched_media: List of media files without JSON
            unmatched_json: List of JSON files without media
        """
        self._dir_index.clear()
        media_files = self.find_all_media_files(directory)
        json_files = [j for j in self.find_all_json_files(directory)
                      if os.path.basename(j).lower() not in NOT_SIDECARS]

        proposals = []
        for media_file in media_files:
            found = self.find_json_with_rule(media_file)
            if found:
                proposals.append((RULES.index(found[1]), media_file, found[0], found[1]))
        proposals.sort(key=lambda p: p[0])

        owner: Dict[str, str] = {}   # sidecar -> media holding it under a non-shared rule
        used = set()
        matched_pairs = []
        claimed = set()
        for _, media_file, json_file, rule in proposals:
            if rule not in SHARED_RULES:
                if json_file in owner:
                    continue  # taken by a stronger match; this file gets none
                owner[json_file] = media_file
            matched_pairs.append(self._pair(media_file, json_file, rule))
            used.add(json_file)
            claimed.add(media_file)

        unmatched_media = [m for m in media_files if m not in claimed]
        if unmatched_media:
            by_title = self._title_index([j for j in json_files if j not in used])
            keys = {m: self._name_key(os.path.basename(m)) for m in unmatched_media}
            files_per_key = Counter(keys.values())
            still_unmatched = []
            for media_file in unmatched_media:
                key = keys[media_file]
                candidates = [j for j in by_title.get(key, []) if j not in used]
                # Only a one-to-one match: one file, one unused sidecar.
                if len(candidates) == 1 and files_per_key[key] == 1:
                    matched_pairs.append(self._pair(media_file, candidates[0], 'title-index'))
                    used.add(candidates[0])
                else:
                    still_unmatched.append(media_file)
            unmatched_media = still_unmatched

        unmatched_json = [j for j in json_files if j not in used]
        return matched_pairs, unmatched_media, unmatched_json

    def _pair(self, media_file: str, json_file: str, rule: str) -> Dict:
        return {
            'media': media_file,
            'json': json_file,
            'rule': rule,
            'is_image': self.is_image_file(media_file),
            'is_video': self.is_video_file(media_file),
        }

    def _title_index(self, json_files: List[str]) -> Dict[Tuple[str, str], List[str]]:
        """Sidecars by the name key of their title, for finding a sidecar that
        sits in another folder than its photo (album folders often hold only
        sidecars while the photo is in "Photos from YYYY")."""
        index: Dict[Tuple[str, str], List[str]] = {}
        for json_file in json_files:
            title = self.title_of(json_file)
            if title:
                index.setdefault(self._name_key(title), []).append(json_file)
        return index
