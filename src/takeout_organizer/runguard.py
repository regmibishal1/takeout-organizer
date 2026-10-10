"""Guards for long runs: one run per destination, and no sleep while running."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from typing import Optional

# The tool's own folder in the destination (hidden; never scanned as input).
STATE_DIR = '.takeout-organizer'


def _process_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        # os.kill(pid, 0) would terminate the process on Windows.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """`dest/.takeout-organizer/lock`, holding the PID and start time of the run
    writing to that destination. A lock left by a process that is gone is stale
    and taken over."""

    def __init__(self, dest_folder: str):
        self.path = os.path.join(dest_folder, STATE_DIR, 'lock')
        self.held = False

    def holder(self) -> Optional[str]:
        """'started 14:02, process 4182' if another live run holds the lock."""
        try:
            with open(self.path, encoding='utf-8') as f:
                pid_text, started = (f.read().split('\n') + [''])[:2]
            pid = int(pid_text)
        except (OSError, ValueError):
            return None
        if pid == os.getpid() or not _process_alive(pid):
            return None
        return f"started {started}, process {pid}"

    def acquire(self) -> Optional[str]:
        """Take the lock; returns the other run's description if it is busy."""
        busy = self.holder()
        if busy:
            return busy
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(f"{os.getpid()}\n{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        self.held = True
        return None

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            os.remove(self.path)
        except OSError:
            pass


class KeepAwake:
    """Stop the computer from going to sleep during a run (best effort).

    Windows: SetThreadExecutionState. macOS: caffeinate. Linux: systemd-inhibit.
    """

    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self) -> None:
        self._helper: Optional[subprocess.Popen] = None
        self.active = False

    def start(self) -> bool:
        try:
            if os.name == 'nt':
                import ctypes
                self.active = bool(ctypes.windll.kernel32.SetThreadExecutionState(
                    self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED))
            elif sys.platform == 'darwin':
                self._helper = subprocess.Popen(
                    ['caffeinate', '-i', '-w', str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.active = True
            else:
                self._helper = subprocess.Popen(
                    ['systemd-inhibit', '--what=sleep', '--who=takeout-organizer',
                     '--why=Organizing photos', 'sleep', 'infinity'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.active = self._helper.poll() is None
        except Exception:
            self.active = False
        return self.active

    def stop(self) -> None:
        if os.name == 'nt' and self.active:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self._ES_CONTINUOUS)
            except Exception:
                pass
        if self._helper is not None:
            self._helper.terminate()
            try:
                self._helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._helper.kill()
            self._helper = None
        self.active = False
