"""
File Processor for Google Photos Takeout
Handles zip extraction, file processing, and organization
"""

import io
import os
import shutil
import tempfile
import zipfile
import hashlib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from datetime import datetime
import logging


# A copy in progress is written under this suffix and renamed when complete, so
# a crash never leaves a cut-off file under its real name. The suffix is unusual
# enough that cleaning up leftovers can never touch a user's own file.
PART_SUFFIX = '.takeout-organizer-part'

class SourceReadError(OSError):
    """Reading the Takeout ZIP itself failed (a network share dropped, a USB
    disk went to sleep), as opposed to writing the extracted file."""


class _SourceFile(io.FileIO):
    """A ZIP file whose read errors are raised as SourceReadError, and
    remembered: zipfile reports a failed read of the ZIP's directory as "not a
    zip file"."""

    read_failed = False

    def _read(self, size: int) -> bytes:
        return super().read(size)

    def read(self, size=-1):
        try:
            return self._read(size)
        except OSError as exc:
            self.read_failed = True
            raise SourceReadError(*exc.args) from exc

    def seek(self, offset, whence=0):
        try:
            return super().seek(offset, whence)
        except OSError as exc:
            self.read_failed = True
            raise SourceReadError(*exc.args) from exc


# Folder layouts for the output: 2023/06/15, 2023/06, 2023, or one folder.
LAYOUTS = ('year-month-day', 'year-month', 'year', 'flat')


class FileProcessor:
    """Processes Google Takeout files: extraction, deduplication, and organization"""

    def __init__(self, logger: Optional[logging.Logger] = None, work_dir: Optional[str] = None,
                 layout: str = 'year-month'):
        self.logger = logger or logging.getLogger(__name__)
        self.work_dir = work_dir  # for temporary copies; None means the OS temp dir
        if layout not in LAYOUTS:
            raise ValueError(f"layout must be one of {LAYOUTS}")
        self.layout = layout  # folders by date: see LAYOUTS
        # (zip name, member) for every ZIP entry that could not be extracted
        self.unreadable_members: List[tuple] = []
        # extraction folder -> ZIP file name, and merged file -> (ZIP file name,
        # path inside the ZIP): where each file came from, for stable IDs
        self.zip_of_dir: Dict[str, str] = {}
        self.origins: Dict[str, Tuple[str, str]] = {}

    def extract_zip_files(self, source_dir: str, extract_dir: str,
                          progress_callback: Optional[Callable[[int, int, str], None]] = None) -> List[str]:
        """
        Extract all ZIP files from source directory with robust handling for large network files

        Args:
            source_dir: Directory containing ZIP files
            extract_dir: Directory to extract to
            progress_callback: Callback function(current, total, filename)

        Returns:
            List of extracted directory paths
        """
        # Find all ZIP files
        zip_files = []
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                if file.lower().endswith('.zip'):
                    zip_files.append(os.path.join(root, file))

        self.logger.info(f"Found {len(zip_files)} ZIP files to extract")

        extracted_dirs = []
        total = len(zip_files)

        for idx, zip_path in enumerate(zip_files, 1):
            zip_name = os.path.basename(zip_path)

            if progress_callback:
                progress_callback(idx, total, zip_name)

            try:
                self.logger.info(f"Extracting {zip_name}...")

                # Create extraction subdirectory
                zip_basename = Path(zip_path).stem
                extract_subdir = os.path.join(extract_dir, zip_basename)

                # Try to extract - with fallback for large network files
                extracted_path = self._extract_zip_robust(zip_path, extract_subdir)

                if extracted_path:
                    extracted_dirs.append(extracted_path)
                    self.zip_of_dir[os.path.normpath(extracted_path)] = zip_name
                    self.logger.info(f"Extracted {zip_name} to {extracted_path}")
                else:
                    self.logger.error(f"Failed to extract {zip_name}")

            except zipfile.BadZipFile:
                self.logger.error(f"Bad ZIP file: {zip_name}")
            except Exception as e:
                self.logger.error(f"Error extracting {zip_name}: {e}")

        return extracted_dirs

    def _extract_zip_robust(self, zip_path: str, extract_to: str) -> Optional[str]:
        """Extract every entry of a ZIP into extract_to.

        Entries that could not be read from the ZIP (a network share dropping
        out, a USB disk going to sleep) are read again, once, from a copy of
        the ZIP in the work folder, and the log says so. An entry that fails
        its checksum is damaged in the ZIP itself and a write error is about
        the destination; a copy helps neither, so those are recorded as
        unreadable straight away. Returns None when the ZIP cannot be used.
        """
        os.makedirs(extract_to, exist_ok=True)
        zip_name = os.path.basename(zip_path)
        try:
            retry = self._extract_members(zip_path, zip_path, extract_to, None)
        except SourceReadError as exc:
            self.logger.warning(f"Could not read {zip_name} ({exc}).")
            retry = None   # every entry
        except zipfile.BadZipFile as exc:
            self.logger.error(f"{zip_name} is not a readable ZIP file ({exc}).")
            return None
        if retry == []:
            return extract_to

        what = "it" if retry is None else f"{len(retry)} of its entries"
        self.logger.warning(f"Reading {what} again from a copy of {zip_name} in the work folder.")
        temp_dir = tempfile.mkdtemp(prefix='zip_copy_', dir=self.work_dir)
        copy = os.path.join(temp_dir, zip_name)
        try:
            if shutil.disk_usage(temp_dir).free < os.path.getsize(zip_path):
                raise OSError("not enough free space in the work folder for the copy")
            self._copy_with_progress(zip_path, copy)
            still = self._extract_members(copy, zip_path, extract_to, retry)
        except (OSError, zipfile.BadZipFile) as exc:
            self.logger.error(f"The copy of {zip_name} did not help ({exc}).")
            if retry is None:
                return None
            still = retry
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        for member in still:
            self._record_unreadable(zip_path, member, extract_to)
        if retry is not None:
            self.logger.info(f"Read {len(retry) - len(still)} of {len(retry)} entries "
                             f"from the copy of {zip_name}.")
        return extract_to

    def _extract_members(self, path: str, zip_path: str, extract_to: str,
                         members: Optional[List[str]]) -> List[str]:
        """Extract the members (all when None) of the ZIP at path, which is
        zip_path or a copy of it. Returns the members that could not be read
        from it; those that fail otherwise are recorded as unreadable."""
        unread = []
        with _SourceFile(path, 'rb') as raw:
            try:
                zip_ref = zipfile.ZipFile(raw)
            except zipfile.BadZipFile:
                if raw.read_failed:
                    raise SourceReadError("its list of files could not be read") from None
                raise
            names = zip_ref.namelist() if members is None else members
            for i, member in enumerate(names, 1):
                try:
                    zip_ref.extract(member, extract_to)
                except SourceReadError as exc:
                    self.logger.warning(f"Could not read {member}: {exc}")
                    self._remove_partial(extract_to, member)
                    unread.append(member)
                except Exception as exc:
                    # Damaged in the ZIP, or the file could not be written.
                    self.logger.warning(f"Failed to extract {member}: {exc}")
                    self._record_unreadable(zip_path, member, extract_to)
                if i % 100 == 0:
                    self.logger.debug(f"Extracted {i}/{len(names)} files")
        return unread

    @staticmethod
    def _remove_partial(extract_to: str, member: str) -> None:
        """Remove what was written of an entry that failed: a damaged entry is
        written before its checksum fails, and must not be organized as if it
        were a good photo."""
        if member.endswith('/'):
            return
        try:
            os.remove(os.path.join(extract_to, *member.split('/')))
        except OSError:
            pass

    def _record_unreadable(self, zip_path: str, member: str, extract_to: str) -> None:
        """Note a ZIP entry that failed to extract, so the summary counts it."""
        if member.endswith('/'):
            return
        self._remove_partial(extract_to, member)
        entry = (os.path.basename(zip_path), member)
        if entry not in self.unreadable_members:
            self.unreadable_members.append(entry)

    def _copy_with_progress(self, src: str, dst: str, chunk_size: int = 10*1024*1024):
        """
        Copy file with progress logging (10MB chunks)

        Args:
            src: Source file path
            dst: Destination file path
            chunk_size: Size of chunks to copy (default 10MB)
        """
        total_size = os.path.getsize(src)
        copied = 0
        with open(src, 'rb') as fsrc:
            with open(dst, 'wb') as fdst:
                while True:
                    chunk = fsrc.read(chunk_size)
                    if not chunk:
                        break

                    fdst.write(chunk)
                    copied += len(chunk)

                    # Log progress every 100MB
                    if copied % (100 * 1024 * 1024) < chunk_size:
                        progress_pct = (copied / total_size) * 100
                        progress_gb = copied / (1024**3)
                        total_gb = total_size / (1024**3)
                        self.logger.info(f"Copying: {progress_gb:.2f}/{total_gb:.2f} GB ({progress_pct:.1f}%)")

        self.logger.info(f"Copy complete: {total_size / (1024**3):.2f} GB")

    def merge_takeout_folders(self, extracted_dirs: List[str], merged_dir: str) -> str:
        """
        Merge multiple Google Photos folders from different Takeout archives

        Args:
            extracted_dirs: List of extracted directories
            merged_dir: Target directory for merged content

        Returns:
            Path to merged Google Photos directory
        """
        os.makedirs(merged_dir, exist_ok=True)

        google_photos_dir = os.path.join(merged_dir, "Google Photos")
        os.makedirs(google_photos_dir, exist_ok=True)

        self.logger.info(f"Merging Takeout folders into {google_photos_dir}")

        for extract_dir in extracted_dirs:
            # Find Google Photos folder
            gp_source = None

            # Common paths: Takeout/Google Photos or just Google Photos
            potential_paths = [
                os.path.join(extract_dir, "Takeout", "Google Photos"),
                os.path.join(extract_dir, "Google Photos"),
            ]

            for path in potential_paths:
                if os.path.exists(path):
                    gp_source = path
                    break

            origin = (os.path.normpath(extract_dir),
                      self.zip_of_dir.get(os.path.normpath(extract_dir),
                                          os.path.basename(extract_dir) + '.zip'))
            if gp_source:
                self._merge_directory_contents(gp_source, google_photos_dir, origin)
            elif os.path.isdir(os.path.join(extract_dir, "Takeout")):
                # A Takeout of other Google products (Drive, Mail, ...).
                self.logger.warning(
                    f"{os.path.basename(extract_dir)} is a Takeout without Google Photos; skipped"
                )
            else:
                # A shared album's "Download all" ZIP: photos with no Takeout
                # folders. Keep them as an album named after the ZIP; they used
                # to be skipped, so the shared-album merge never saw them.
                album = os.path.basename(os.path.normpath(extract_dir))
                self.logger.info(f"{album}: no Takeout folders, treated as an album download")
                self._merge_directory_contents(extract_dir, os.path.join(google_photos_dir, album),
                                               origin)

        return google_photos_dir

    def _merge_directory_contents(self, src: str, dst: str,
                                  origin: Optional[Tuple[str, str]] = None):
        """
        Recursively merge directory contents, avoiding duplicates. Files are
        moved, not copied: both sides are this run's own temporary extraction,
        and copying needed twice the library's size in temporary space.

        Args:
            src: Source directory
            dst: Destination directory
            origin: (extraction folder, ZIP name), to record where each file
                came from in self.origins
        """
        if not os.path.exists(dst):
            os.makedirs(dst, exist_ok=True)

        for item in os.listdir(src):
            src_path = os.path.join(src, item)
            dst_path = os.path.join(dst, item)

            if os.path.isdir(src_path):
                # Recursively merge subdirectories
                self._merge_directory_contents(src_path, dst_path, origin)
            else:
                # Copy file if it doesn't exist or is different
                if not os.path.exists(dst_path):
                    self._record_origin(src_path, dst_path, origin)
                    shutil.move(src_path, dst_path)
                else:
                    # Check if files are different
                    if not self._files_are_identical(src_path, dst_path):
                        # Rename and copy
                        base, ext = os.path.splitext(dst_path)
                        counter = 1
                        while os.path.exists(f"{base}_{counter}{ext}"):
                            counter += 1
                        new_dst = f"{base}_{counter}{ext}"
                        self._record_origin(src_path, new_dst, origin)
                        shutil.move(src_path, new_dst)
                        self.logger.debug(f"Copied duplicate with new name: {new_dst}")

    def _record_origin(self, src_path: str, dst_path: str,
                       origin: Optional[Tuple[str, str]]) -> None:
        if origin is None:
            return
        extract_root, zip_name = origin
        member = os.path.relpath(src_path, extract_root).replace(os.sep, '/')
        self.origins[dst_path] = (zip_name, member)

    def _files_are_identical(self, file1: str, file2: str) -> bool:
        """Check if two files are identical by comparing size and hash"""
        # Quick check: file size
        if os.path.getsize(file1) != os.path.getsize(file2):
            return False

        # Full check: hash
        return self.calculate_file_hash(file1) == self.calculate_file_hash(file2)

    def calculate_file_hash(self, filepath: str, algorithm: str = 'sha256') -> str:
        """
        Calculate hash of a file

        Args:
            filepath: Path to file
            algorithm: Hash algorithm (md5, sha1, sha256)

        Returns:
            Hex digest of file hash
        """
        hash_func = hashlib.new(algorithm)

        with open(filepath, 'rb') as f:
            # Read file in chunks to handle large files
            for chunk in iter(lambda: f.read(8192), b''):
                hash_func.update(chunk)

        return hash_func.hexdigest()

    def date_folder(self, src_file: str, dest_base: str,
                    date: Optional[datetime] = None) -> str:
        """
        Date-based directory a file belongs in, per the layout. Creates nothing.

        Args:
            src_file: Source file path (its modification date is the fallback)
            dest_base: Base destination directory
            date: Date to use for organization (if None, uses file modification date)

        Returns:
            Destination directory path
        """
        if date is None:
            # Use file modification date as fallback
            timestamp = os.path.getmtime(src_file)
            date = datetime.fromtimestamp(timestamp)

        parts = {
            'year-month-day': [str(date.year), f"{date.month:02d}", f"{date.day:02d}"],
            'year-month': [str(date.year), f"{date.month:02d}"],
            'year': [str(date.year)],
            'flat': [],
        }[self.layout]
        return os.path.join(dest_base, *parts)

    def organize_by_date(self, src_file: str, dest_base: str,
                         date: Optional[datetime] = None, create_dirs: bool = True) -> str:
        """
        Organize file into date-based directory structure (YYYY/MM/DD)

        Args:
            src_file: Source file path
            dest_base: Base destination directory
            date: Date to use for organization (if None, uses file modification date)
            create_dirs: If False (dry run), only compute the path

        Returns:
            Destination file path
        """
        day_dir = self.date_folder(src_file, dest_base, date)

        if create_dirs:
            os.makedirs(day_dir, exist_ok=True)

        # Determine destination filename
        filename = os.path.basename(src_file)
        dest_path = os.path.join(day_dir, filename)

        # Handle name collision
        if os.path.exists(dest_path):
            dest_path = self._get_unique_filename(dest_path)

        return dest_path

    def find_identical_copy(self, src_file: str, dest_path: str) -> Optional[str]:
        """
        Find a byte-identical copy of src_file already at dest_path or at one of
        its collision names (name_1.ext, name_2.ext, ...), so a rerun into the
        same destination does not copy the library a second time.

        Returns:
            Path of the identical copy, or None
        """
        directory = os.path.dirname(dest_path)
        name, ext = os.path.splitext(os.path.basename(dest_path))

        candidate = dest_path
        counter = 0
        while os.path.exists(candidate):
            if self._files_are_identical(src_file, candidate):
                return candidate
            counter += 1
            candidate = os.path.join(directory, f"{name}_{counter}{ext}")

        return None

    def _get_unique_filename(self, filepath: str) -> str:
        """
        Get a unique filename by adding a counter if file exists

        Args:
            filepath: Desired file path

        Returns:
            Unique file path
        """
        if not os.path.exists(filepath):
            return filepath

        directory = os.path.dirname(filepath)
        filename = os.path.basename(filepath)
        name, ext = os.path.splitext(filename)

        counter = 1
        while True:
            new_name = f"{name}_{counter}{ext}"
            new_path = os.path.join(directory, new_name)

            if not os.path.exists(new_path):
                return new_path

            counter += 1

    def copy_file_safe(self, src: str, dst: str, overwrite: bool = False) -> bool:
        """
        Safely copy file, avoiding overwrites unless specified

        Args:
            src: Source file path
            dst: Destination file path
            overwrite: If True, allows overwriting existing files

        Returns:
            True if copied successfully, False otherwise
        """
        try:
            # Check if destination exists
            if os.path.exists(dst) and not overwrite:
                self.logger.warning(f"Destination exists, skipping: {dst}")
                return False

            # Create destination directory if needed
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            # Copy file with metadata, under a temporary name first
            part = dst + PART_SUFFIX
            shutil.copy2(src, part)
            os.replace(part, dst)

            return True

        except Exception as e:
            self.logger.error(f"Error copying file {src} to {dst}: {e}")
            try:
                os.remove(dst + PART_SUFFIX)
            except OSError:
                pass
            return False

    def move_file_safe(self, src: str, dst: str) -> bool:
        """Move a file into place, never over an existing one: a rename when
        both are on the same drive, else copy_file_safe (which leaves the
        source). The run lock keeps other runs out of the destination, so the
        check before the rename cannot be raced."""
        if os.path.exists(dst):
            self.logger.warning(f"Destination exists, skipping: {dst}")
            return False
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            return True
        except OSError:
            return self.copy_file_safe(src, dst, overwrite=False)

    def remove_partial_copies(self, dest_dir: str) -> int:
        """Delete copies a crashed run left half-written; returns how many."""
        removed = 0
        for root, _, files in os.walk(dest_dir):
            for name in files:
                if name.endswith(PART_SUFFIX):
                    try:
                        os.remove(os.path.join(root, name))
                        removed += 1
                    except OSError:
                        pass
        return removed

    def clean_temp_directory(self, temp_dir: str):
        """
        Clean up temporary directory

        Args:
            temp_dir: Directory to remove
        """
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                self.logger.info(f"Cleaned up temporary directory: {temp_dir}")
        except Exception as e:
            self.logger.error(f"Error cleaning up {temp_dir}: {e}")

    def get_file_size(self, filepath: str) -> int:
        """Get file size in bytes"""
        return os.path.getsize(filepath)

    def get_directory_size(self, directory: str) -> int:
        """Get total size of directory in bytes"""
        total_size = 0

        for root, dirs, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    total_size += os.path.getsize(file_path)
                except Exception:
                    pass

        return total_size

    def count_files(self, directory: str, extensions: Optional[List[str]] = None) -> int:
        """
        Count files in directory, optionally filtering by extension

        Args:
            directory: Directory to search
            extensions: List of extensions to count (e.g., ['.jpg', '.mp4'])

        Returns:
            Number of files
        """
        count = 0

        for root, dirs, files in os.walk(directory):
            for file in files:
                if extensions:
                    if any(file.lower().endswith(ext) for ext in extensions):
                        count += 1
                else:
                    count += 1

        return count
