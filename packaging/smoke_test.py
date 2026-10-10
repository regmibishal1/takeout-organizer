"""Check a standalone build on a small Takeout: it starts, uses the ExifTool
it ships, organizes a photo with its date and place, and verifies.

Usage: python packaging/smoke_test.py PATH_TO_takeout-organizer[.exe]
Run it with ExifTool off the PATH to be sure the bundled one is used.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

from PIL import Image


def run(exe: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run([exe, *args], capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=600)
    print(f"$ takeout-organizer {' '.join(args)}  -> exit {result.returncode}")
    return result


def main(exe: str) -> int:
    base = tempfile.mkdtemp(prefix='smoke_')
    src, dest = os.path.join(base, 'Takeout'), os.path.join(base, 'Organized')
    os.makedirs(src)
    photo = io.BytesIO()
    Image.new('RGB', (32, 24), (200, 40, 40)).save(photo, 'JPEG')
    with zipfile.ZipFile(os.path.join(src, 'takeout-001.zip'), 'w') as zf:
        folder = 'Takeout/Google Photos/Photos from 2023'
        zf.writestr(f'{folder}/IMG_1001.jpg', photo.getvalue())
        zf.writestr(f'{folder}/IMG_1001.jpg.supplemental-metadata.json', json.dumps({
            'title': 'IMG_1001.jpg', 'photoTakenTime': {'timestamp': '1686839445'},
            'geoData': {'latitude': 35.6586, 'longitude': 139.7454, 'altitude': 40.0}}))

    problems = []
    version = run(exe, '--version')
    if version.returncode != 0 or 'takeout-organizer' not in version.stdout:
        problems.append(f"--version: {version.stdout}{version.stderr}")
    diagnose = run(exe, 'diagnose')
    print(diagnose.stdout)
    if 'bundled with this build' not in diagnose.stdout:
        problems.append("diagnose does not report the bundled ExifTool")
    result = run(exe, 'run', '--source', src, '--dest', dest, '--yes', '--json')
    try:
        summary = json.loads(result.stdout).get('summary', {})
    except ValueError:
        summary = {}
        problems.append(f"run printed no JSON summary: {result.stdout[-500:]}{result.stderr[-500:]}")
    if summary and not (summary.get('balanced') and summary.get('written') == 1):
        problems.append(f"unexpected summary: {summary}")
    if not os.path.exists(os.path.join(dest, '2023', '06', 'IMG_1001.jpg')):
        problems.append("the photo is not in 2023/06")
    verify = run(exe, 'verify', '--dest', dest)
    print(verify.stdout)
    if verify.returncode != 0:
        problems.append("verify found a problem")
    # The window's translations ship with the build.
    top = os.path.dirname(os.path.abspath(exe))
    for _ in range(3):   # the folder, or the .app's Contents on macOS
        found = [os.path.join(base, name) for base, _dirs, names in os.walk(top)
                 for name in names if name == 'de.po']
        if found:
            break
        top = os.path.dirname(top)
    if not found:
        problems.append("the build has no translations (locale/de.po)")
    # The place names ship with the build: the photo was taken by Tokyo Tower.
    places = os.path.join(base, 'With places')
    run(exe, 'run', '--source', src, '--dest', places, '--yes', '--place-names')
    if not os.path.exists(os.path.join(places, '2023', '06 Tokyo', 'IMG_1001.jpg')):
        problems.append("with --place-names, the photo is not in 2023/06 Tokyo")

    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("smoke test passed" if not problems else "smoke test FAILED")
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
