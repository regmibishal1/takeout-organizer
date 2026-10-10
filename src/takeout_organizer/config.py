"""
Configuration Settings for Google Photos Takeout Organizer
"""

import os
from pathlib import Path

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# ExifTool Configuration
# UPDATE THIS PATH to point to your ExifTool installation
EXIFTOOL_PATH = None  # Set to None to search in PATH, or specify full path

# Common ExifTool installation locations (will be checked automatically)
EXIFTOOL_SEARCH_PATHS = [
    # Project-specific ExifTool location (installed in tools folder)
    os.path.join(PROJECT_ROOT, 'tools', 'exiftool', 'exiftool.exe'),

    # Other common locations
    os.path.join(PROJECT_ROOT, 'exiftool.exe'),
    os.path.join(PROJECT_ROOT, 'tools', 'exiftool.exe'),
    r'C:\Windows\exiftool.exe',
    r'C:\Tools\exiftool.exe',
    r'C:\Program Files\exiftool\exiftool.exe',
]

# Processing Settings
DEFAULT_DRY_RUN = False
DEFAULT_PRESERVE_ORIGINALS = False
DEFAULT_KEEP_JSON = False

# Logging Settings
LOG_LEVEL = 'INFO'
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# Performance Settings
HASH_ALGORITHM = 'sha256'  # Hash algorithm for duplicate detection
SAVE_STATE_INTERVAL = 100  # Save session state every N files

# File Extensions
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.heic', '.png', '.gif', '.webp', '.tiff', '.tif', '.bmp', '.dng', '.raw'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.mpg', '.mpeg', '.m4v', '.webm', '.wmv', '.3gp', '.flv'}

# UI Settings
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 600
WINDOW_MIN_WIDTH = 700
WINDOW_MIN_HEIGHT = 500
