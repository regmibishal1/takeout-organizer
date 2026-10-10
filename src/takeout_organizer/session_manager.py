"""
Session Manager for Google Photos Takeout Organizer
Handles progress tracking and recovery for large-scale processing operations
"""

import json
import os
from datetime import datetime
from typing import Dict, Optional


class SessionManager:
    """Manages processing state for resume capability and progress tracking"""

    def __init__(self, session_file: Optional[str] = "processing_session.json",
                 resume: bool = True):
        """
        Args:
            session_file: Where state is saved. None keeps it in memory only
                (dry runs must not leave state behind).
            resume: Load an existing session file. When False, a stale file is
                ignored and replaced by this run's state.
        """
        self.session_file = session_file
        if session_file and resume:
            self.state = self._load_state()
        else:
            self.state = self._create_new_state()

    def _load_state(self) -> Dict:
        """Load existing session state or create new one"""
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Could not load session file: {e}")
                return self._create_new_state()
        return self._create_new_state()

    def _create_new_state(self) -> Dict:
        """Create a new session state"""
        return {
            'session_id': datetime.now().strftime('%Y%m%d_%H%M%S'),
            'started_at': datetime.now().isoformat(),
            'last_updated': datetime.now().isoformat(),
            'source_folder': None,
            'destination_folder': None,
            'phase': 'not_started',  # not_started, extracting, matching, processing, organizing, completed
            'processed_files': [],
            'failed_files': [],
            'skipped_files': [],
            'file_hashes': {},  # hash -> file path mapping for duplicate detection
            'statistics': {
                'total_zips': 0,
                'extracted_zips': 0,
                'total_media_files': 0,
                'total_json_files': 0,
                'matched_files': 0,
                'unmatched_media': 0,
                'unmatched_json': 0,
                'processed_files': 0,
                'failed_files': 0,
                'skipped_duplicates': 0,
                'bytes_processed': 0
            },
            'errors': [],
            'warnings': []
        }

    def save(self):
        """Save current state to disk"""
        self.state['last_updated'] = datetime.now().isoformat()
        if not self.session_file:
            return
        try:
            # Save to temporary file first, then rename (atomic operation)
            temp_file = f"{self.session_file}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)

            # Atomic rename
            if os.path.exists(self.session_file):
                os.replace(temp_file, self.session_file)
            else:
                os.rename(temp_file, self.session_file)
        except Exception as e:
            print(f"Error saving session state: {e}")

    def set_folders(self, source: str, destination: str):
        """Set source and destination folders"""
        self.state['source_folder'] = source
        self.state['destination_folder'] = destination
        self.save()

    def set_phase(self, phase: str):
        """Update current processing phase"""
        self.state['phase'] = phase
        self.save()

    def mark_file_processed(self, file_path: str, file_hash: Optional[str] = None):
        """Mark a file as successfully processed"""
        if file_path not in self.state['processed_files']:
            self.state['processed_files'].append(file_path)
            self.state['statistics']['processed_files'] += 1

            if file_hash:
                self.state['file_hashes'][file_hash] = file_path

            # Save every 100 files to minimize data loss
            if len(self.state['processed_files']) % 100 == 0:
                self.save()

    def mark_file_failed(self, file_path: str, error: str):
        """Mark a file as failed with error message"""
        self.state['failed_files'].append({
            'file': file_path,
            'error': error,
            'timestamp': datetime.now().isoformat()
        })
        self.state['statistics']['failed_files'] += 1
        self.save()

    def mark_file_skipped(self, file_path: str, reason: str):
        """Mark a file as skipped with reason"""
        self.state['skipped_files'].append({
            'file': file_path,
            'reason': reason,
            'timestamp': datetime.now().isoformat()
        })
        self.save()

    def is_file_processed(self, file_path: str) -> bool:
        """Check if file was already processed"""
        return file_path in self.state['processed_files']

    def is_duplicate(self, file_hash: str) -> Optional[str]:
        """Check if file hash already exists, return original path if duplicate"""
        return self.state['file_hashes'].get(file_hash)

    def add_error(self, error_msg: str, details: Optional[Dict] = None):
        """Add error to session log"""
        error_entry = {
            'message': error_msg,
            'timestamp': datetime.now().isoformat()
        }
        if details:
            error_entry['details'] = details

        self.state['errors'].append(error_entry)
        self.save()

    def add_warning(self, warning_msg: str, details: Optional[Dict] = None):
        """Add warning to session log"""
        warning_entry = {
            'message': warning_msg,
            'timestamp': datetime.now().isoformat()
        }
        if details:
            warning_entry['details'] = details

        self.state['warnings'].append(warning_entry)
        # Don't save warnings immediately (too frequent)

    def update_statistics(self, **kwargs):
        """Update statistics"""
        for key, value in kwargs.items():
            if key in self.state['statistics']:
                self.state['statistics'][key] = value

    def increment_statistic(self, key: str, amount: int = 1):
        """Increment a statistic counter"""
        if key in self.state['statistics']:
            self.state['statistics'][key] += amount

    def get_progress(self) -> Dict:
        """Get current progress information"""
        stats = self.state['statistics']
        total_files = stats.get('total_media_files', 0)
        processed = stats.get('processed_files', 0)

        progress_pct = 0
        if total_files > 0:
            progress_pct = (processed / total_files) * 100

        return {
            'phase': self.state['phase'],
            'processed': processed,
            'total': total_files,
            'percentage': progress_pct,
            'failed': stats.get('failed_files', 0),
            'skipped': stats.get('skipped_duplicates', 0)
        }

    def generate_report(self) -> str:
        """Generate a final processing report"""
        stats = self.state['statistics']

        report = f"""
# Google Photos Takeout Processing Report

**Session ID:** {self.state['session_id']}
**Started:** {self.state['started_at']}
**Completed:** {datetime.now().isoformat()}
**Source:** {self.state['source_folder']}
**Destination:** {self.state['destination_folder']}

## Summary Statistics

- **Total ZIP files:** {stats['total_zips']}
- **Extracted ZIPs:** {stats['extracted_zips']}
- **Media files found:** {stats['total_media_files']}
- **JSON files found:** {stats['total_json_files']}
- **Successfully matched:** {stats['matched_files']}
- **Unmatched media:** {stats['unmatched_media']}
- **Unmatched JSON:** {stats['unmatched_json']}
- **Successfully processed:** {stats['processed_files']}
- **Failed:** {stats['failed_files']}
- **Skipped duplicates:** {stats['skipped_duplicates']}
- **Data processed:** {self._format_bytes(stats['bytes_processed'])}

## Errors ({len(self.state['errors'])})

"""

        for i, error in enumerate(self.state['errors'][:50], 1):  # Limit to first 50
            report += f"{i}. [{error['timestamp']}] {error['message']}\n"

        if len(self.state['errors']) > 50:
            report += f"\n... and {len(self.state['errors']) - 50} more errors\n"

        report += f"\n## Warnings ({len(self.state['warnings'])})\n\n"

        for i, warning in enumerate(self.state['warnings'][:50], 1):
            report += f"{i}. [{warning['timestamp']}] {warning['message']}\n"

        if len(self.state['warnings']) > 50:
            report += f"\n... and {len(self.state['warnings']) - 50} more warnings\n"

        report += "\n## Failed Files\n\n"

        for i, failed in enumerate(self.state['failed_files'][:100], 1):
            report += f"{i}. {failed['file']}\n   Error: {failed['error']}\n"

        if len(self.state['failed_files']) > 100:
            report += f"\n... and {len(self.state['failed_files']) - 100} more failed files\n"

        return report

    def _format_bytes(self, bytes_count: int) -> str:
        """Format bytes to human-readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024.0:
                return f"{bytes_count:.2f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.2f} PB"

    def save_report(self, output_path: Optional[str] = None):
        """Save the final report to a markdown file"""
        if output_path is None:
            output_path = f"processing_report_{self.state['session_id']}.md"

        report = self.generate_report()

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)

        return output_path
