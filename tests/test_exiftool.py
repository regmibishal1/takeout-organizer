"""The long-running ExifTool process (-stay_open), against the real ExifTool."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from takeout_organizer import exiftool as exiftool_module
from takeout_organizer.exiftool import ExifToolProcess
from takeout_organizer.main_processor import MainProcessor
from test_integration import _mp4_bytes

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="ExifTool not installed"
)

PIL = pytest.importorskip("PIL.Image", reason="Pillow needed to make a test JPEG")


@pytest.fixture
def photo(tmp_path):
    # A non-ASCII folder and name: these cannot cross the Windows command line.
    folder = tmp_path / "\u65e5\u672c"
    folder.mkdir()
    path = folder / "\u5199\u771f.jpg"
    PIL.new("RGB", (8, 8), (10, 20, 30)).save(path, "JPEG")
    return str(path)


def test_values_and_paths_arrive_exactly(photo):
    caption = "Caf\u00e9 a\\b C:\\temp\nsecond line"
    with ExifToolProcess("exiftool") as et:
        result = et.execute(["-overwrite_original", f"-XMP-dc:Description={caption}", photo])
        assert result.returncode == 0, result.stderr
        tags = json.loads(et.execute(["-json", "-XMP-dc:Description", photo]).stdout)[0]
    assert tags["Description"] == caption


def test_errors_set_the_return_code(photo, tmp_path):
    with ExifToolProcess("exiftool") as et:
        missing = et.execute(["-json", str(tmp_path / "missing.jpg")])
        assert missing.returncode == 1 and "File not found" in missing.stderr
        rejected = et.execute(["-overwrite_original", "-DateTimeOriginal=garbage", photo])
        assert rejected.returncode == 1 and "Nothing to do." in rejected.stderr
        # The process is still usable after a failed command.
        assert et.execute(["-json", photo]).returncode == 0


def test_options_do_not_leak_into_the_next_command(tmp_path):
    # QuickTimeUTC changes how video dates read; it must apply to one command only.
    video = tmp_path / "VID.mp4"
    video.write_bytes(_mp4_bytes())
    with ExifToolProcess("exiftool") as et:
        et.execute(["-overwrite_original", "-api", "QuickTimeUTC=1",
                    "-QuickTime:CreateDate=2023:06:15 23:30:45+09:00", str(video)])
        converted = et.execute(["-json", "-api", "QuickTimeUTC=1", "-QuickTime:CreateDate",
                                str(video)])
        stored = et.execute(["-json", "-QuickTime:CreateDate", str(video)])
    assert json.loads(stored.stdout)[0]["CreateDate"] == "2023:06:15 14:30:45"  # UTC
    assert json.loads(converted.stdout)[0]["CreateDate"] != "2023:06:15 14:30:45"

def test_a_dead_process_is_restarted(photo):
    with ExifToolProcess("exiftool") as et:
        assert et.execute(["-json", photo]).returncode == 0
        et._proc.kill()
        et._proc.wait()
        assert et.execute(["-json", photo]).returncode == 0


def test_a_run_starts_exiftool_once(tmp_path, monkeypatch):
    starts = []
    original = exiftool_module.ExifToolProcess._start

    def counting_start(self):
        starts.append(1)
        original(self)

    monkeypatch.setattr(exiftool_module.ExifToolProcess, "_start", counting_start)
    album = tmp_path / "src" / "Takeout" / "Google Photos" / "Photos from 2023"
    album.mkdir(parents=True)
    for i in range(3):
        PIL.new("RGB", (8, 8), (i * 80, 0, 0)).save(album / f"IMG_{i}.jpg", "JPEG")
        (album / f"IMG_{i}.jpg.json").write_text(json.dumps({
            "title": f"IMG_{i}.jpg", "photoTakenTime": {"timestamp": str(1686839445 + i)},
        }))

    processor = MainProcessor(str(tmp_path / "src"), str(tmp_path / "out"))
    assert processor.run()
    assert len(starts) == 1  # three files, each read, written and verified
    assert len(list((tmp_path / "out").rglob("*.jpg"))) == 3
    assert processor.metadata_handler._process._proc is None  # closed after the run


# --- ExifTool ends with the run ---------------------------------------------------

# Starts ExifTool, prints its process ID and those of the processes it
# started (a wrapper starts the real ExifTool), then exits or waits.
_HOLD_EXIFTOOL = """
import os, subprocess, sys, time
from takeout_organizer.exiftool import ExifToolProcess
process = ExifToolProcess(sys.argv[2])
process.execute(["-ver"])
pids = [process._proc.pid]
if os.name == "nt":
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_Process -Filter 'ParentProcessId=%d')"
                          ".ProcessId" % pids[0]], capture_output=True, text=True).stdout
    pids += [int(p) for p in out.split()]
print(" ".join(map(str, pids)), flush=True)
if sys.argv[1] == "wait":
    time.sleep(600)
"""


@pytest.fixture(params=["exiftool", "wrapper"])
def exiftool_command(request, tmp_path):
    """ExifTool itself, and on Windows also behind a wrapper that starts it,
    as Chocolatey's exiftool.exe does."""
    if request.param == "exiftool":
        return "exiftool"
    if os.name != "nt":
        pytest.skip("the wrapper case is for Windows")
    wrapper = tmp_path / "exiftool.bat"
    wrapper.write_text(f'@echo off\r\n"{shutil.which("exiftool")}" %*\r\n')
    return str(wrapper)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"  # a zombie has exited
    except OSError:
        return True  # macOS: no /proc


def _all_exit_within(pids, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not any(_alive(pid) for pid in pids):
            return True
        time.sleep(0.2)
    for pid in pids:  # do not leave processes behind when the test fails
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return False


def test_exiftool_ends_when_python_exits_without_closing_it(exiftool_command):
    # As when the window is closed during a run: nothing closes ExifTool.
    child = subprocess.run([sys.executable, "-c", _HOLD_EXIFTOOL, "exit", exiftool_command],
                           capture_output=True, text=True, timeout=60)
    assert _all_exit_within([int(p) for p in child.stdout.split()], 5)


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS cannot tie a child to its parent")
def test_exiftool_ends_when_the_run_is_killed(exiftool_command):
    child = subprocess.Popen([sys.executable, "-c", _HOLD_EXIFTOOL, "wait", exiftool_command],
                             stdout=subprocess.PIPE, text=True)
    pids = [int(p) for p in child.stdout.readline().split()]
    assert all(_alive(pid) for pid in pids)
    child.kill()  # TerminateProcess on Windows, SIGKILL elsewhere: no cleanup runs
    child.wait()
    assert _all_exit_within(pids, 5)
