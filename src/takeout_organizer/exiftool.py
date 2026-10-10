"""One long-running ExifTool process for a whole run.

Starting ExifTool costs about 200 ms (more on Windows), and a run used to start
it up to three times per file. With `-stay_open`, one process reads commands
from stdin; each command ends with `-execute`, and a marker echoed to stdout
and stderr says when its output is complete.

Arguments travel as UTF-8 lines on stdin, so non-ASCII captions and file paths
reach ExifTool intact on Windows, where command-line arguments would not.
"""

from __future__ import annotations

import atexit
import os
import queue
import signal
import subprocess
import sys
import threading
import weakref
from typing import List, Optional

# With -stay_open True, ExifTool keeps reading its argument file after the end
# of it, so it does not exit when this process dies and its stdin closes. It
# is ended with this process instead: on a normal exit by an atexit handler
# (closing the window during a run, for example), and when this process is
# killed by a Windows job object or, on Linux, the kernel (PR_SET_PDEATHSIG).
# macOS has neither, so there only a normal exit ends it.
_open_processes: 'weakref.WeakSet[ExifToolProcess]' = weakref.WeakSet()
_kernel32 = None  # Windows: kernel32 with the job object functions typed


@atexit.register
def _close_open_processes() -> None:
    for process in list(_open_processes):
        process.close(force=True)


def _windows_job():
    """A new job object whose processes are killed when its last handle
    closes. This process holds the only handle, so they die with it. Each
    ExifTool process gets its own, so stopping one ends everything it started
    (Chocolatey's exiftool.exe is a wrapper that starts the real one). Returns
    (kernel32, handle), or None on failure."""
    global _kernel32
    import ctypes
    from ctypes import wintypes
    if _kernel32 is None:
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic),
                        ('IoInfo', ctypes.c_ulonglong * 6),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.extended_info = Extended
        _kernel32 = kernel32
    kernel32 = _kernel32
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = kernel32.extended_info()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    extended_limit_information = 9
    if not kernel32.SetInformationJobObject(
            job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    return kernel32, job


def _end_with_this_process(proc: subprocess.Popen):
    """Windows: put the ExifTool process in a job object of its own, and
    return the job (or None). Best effort: a failure leaves it as before and
    never stops the run."""
    if os.name != 'nt':
        return None
    try:
        job = _windows_job()
        if job is not None:
            kernel32, handle = job
            if kernel32.AssignProcessToJobObject(handle, int(proc._handle)):
                return job
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        pass
    return None


def _linux_death_signal():
    """Linux: a function run in the child before ExifTool starts, asking the
    kernel to kill it when the thread that started it dies. That thread is
    the run's; if it ends while the process is still wanted, the next command
    starts a new one. None elsewhere."""
    if not sys.platform.startswith('linux'):
        return None
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl  # resolved here, not in the child
    except (OSError, AttributeError):
        return None
    pr_set_pdeathsig = 1

    def set_death_signal() -> None:
        prctl(pr_set_pdeathsig, signal.SIGKILL)
    return set_death_signal


def argfile_line(arg: str) -> str:
    """One ExifTool argument line; multi-line values use C-string escapes."""
    if '\n' in arg or '\r' in arg:
        escaped = arg.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r')
        return f'#[CSTR]{escaped}'
    return arg


class ExifToolError(RuntimeError):
    """The ExifTool process died, hung, or could not be started."""


class ExifToolProcess:
    """A `-stay_open` ExifTool process. Not thread-safe: one caller at a time.

    `execute(args)` returns a subprocess.CompletedProcess-like result whose
    returncode is 1 when ExifTool reported an error, as the CLI would.
    """

    # A single command on a very large video can take a while.
    TIMEOUT_SECONDS = 600

    def __init__(self, executable: str):
        self.executable = executable
        self._proc: Optional[subprocess.Popen] = None
        self._out: Optional[queue.Queue] = None
        self._err: Optional[queue.Queue] = None
        self._counter = 0
        self._job = None                       # Windows: the process's job object
        self._threads: List[threading.Thread] = []  # the pipe readers

    def _start(self) -> None:
        # Its own process group, so Ctrl+C in the terminal reaches only this
        # tool, which then stops after the current file; ExifTool must not die
        # in the middle of writing one.
        if os.name == 'nt':
            isolation = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            isolation = {'start_new_session': True}
        try:
            self._proc = subprocess.Popen(
                [self.executable, '-stay_open', 'True', '-@', '-',
                 '-common_args', '-charset', 'filename=utf8'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=_linux_death_signal(), **isolation,
            )
        except OSError as exc:
            raise ExifToolError(f"Could not start ExifTool: {exc}") from exc
        self._job = _end_with_this_process(self._proc)
        self._threads = []
        _open_processes.add(self)
        self._out = self._reader(self._proc.stdout)
        self._err = self._reader(self._proc.stderr)

    def _reader(self, stream) -> queue.Queue:
        """Drain a pipe on a thread, so a full stderr can never block stdout."""
        lines: queue.Queue = queue.Queue()

        def pump() -> None:
            for raw in iter(stream.readline, b''):
                lines.put(raw.decode('utf-8', errors='replace').rstrip('\r\n'))
            lines.put(None)  # end of stream: the process exited

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self._threads.append(thread)
        return lines

    def _collect(self, lines: queue.Queue, marker: str) -> List[str]:
        collected = []
        while True:
            try:
                line = lines.get(timeout=self.TIMEOUT_SECONDS)
            except queue.Empty:
                self.close(force=True)
                raise ExifToolError("ExifTool stopped responding") from None
            if line is None:
                self.close(force=True)
                raise ExifToolError("ExifTool exited unexpectedly")
            if line == marker:
                return collected
            collected.append(line)

    def execute(self, args: List[str]) -> subprocess.CompletedProcess:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        self._counter += 1
        marker = f'{{ready{self._counter}}}'
        lines = [argfile_line(arg) for arg in args]
        # -echo4 prints the marker to stderr once the command is done;
        # -executeN prints {readyN} to stdout.
        lines += ['-echo4', marker, f'-execute{self._counter}']
        try:
            self._proc.stdin.write(('\n'.join(lines) + '\n').encode('utf-8'))
            self._proc.stdin.flush()
        except OSError as exc:
            self.close(force=True)
            raise ExifToolError(f"ExifTool exited unexpectedly: {exc}") from exc

        stdout = self._collect(self._out, marker)
        stderr = self._collect(self._err, marker)
        # The command-line tool exits 1 on an error, and also when every new
        # value was rejected ("Nothing to do.").
        failed = any(line.startswith('Error') or line == 'Nothing to do.' for line in stderr)
        return subprocess.CompletedProcess(
            args=args, returncode=1 if failed else 0,
            stdout='\n'.join(stdout), stderr='\n'.join(stderr),
        )

    def close(self, force: bool = False) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if not force and proc.poll() is None:
            try:
                proc.stdin.write(b'-stay_open\nFalse\n')
                proc.stdin.flush()
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            self._kill_all(proc)
            proc.wait()
        # A wrapper can end and leave the real ExifTool running (Chocolatey's
        # exiftool.exe does): end whatever is left in the job.
        self._end_job()
        try:
            proc.stdin.close()
        except OSError:
            pass
        # Close the output pipes only once their readers are done: closing a
        # pipe another thread is reading would wait for that read to end.
        for thread in self._threads:
            thread.join(timeout=5)
        if not any(thread.is_alive() for thread in self._threads):
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

    def _kill_all(self, proc: subprocess.Popen) -> None:
        """Kill ExifTool and anything it started: its job on Windows, its
        process group elsewhere (it runs in a session of its own)."""
        if self._job is not None:
            kernel32, handle = self._job
            if kernel32.TerminateJobObject(handle, 1):
                return
        elif os.name != 'nt':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except OSError:
                pass
        proc.kill()

    def _end_job(self) -> None:
        if self._job is not None:
            kernel32, handle = self._job
            self._job = None
            kernel32.TerminateJobObject(handle, 1)
            kernel32.CloseHandle(handle)

    def __enter__(self) -> 'ExifToolProcess':
        return self

    def __exit__(self, *exc) -> None:
        self.close()
