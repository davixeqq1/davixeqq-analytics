"""Cross-platform single-run lock (standard library only).

A run that may store a snapshot or post to Discord holds an exclusive,
non-blocking OS lock on a lock file next to the database:

* POSIX (Raspberry Pi, GitHub runners): ``fcntl.flock`` on the open file.
* Windows: ``msvcrt.locking`` on the first byte of the open file.

The operating system releases the lock when the file handle is closed —
including when the process crashes, is killed or the machine reboots — so a
stale lock can never block future runs. The lock file itself is never deleted
(deleting it while another process waits on it would allow two holders).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

if os.name == "nt":  # pragma: no cover - platform specific
    import msvcrt
else:  # pragma: no cover - platform specific
    import fcntl


class LockBusy(RuntimeError):
    """Another run holds the lock."""


class RunLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        """Try to take the lock without waiting. Returns False if another holder has it."""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        try:  # informational only (who holds it); readable by humans after release on Windows
            fh.seek(1 if os.name == "nt" else 0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} since={time.strftime('%Y-%m-%dT%H:%M:%S')}\n".encode())
        except OSError:
            pass
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # closing the handle below releases the lock in any case
        finally:
            fh.close()

    def __enter__(self) -> "RunLock":
        if not self.acquire():
            raise LockBusy(f"Another yt-report run holds {self.path.name}.")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path_for(database_path: Path) -> Path:
    """The lock lives next to the database it protects (``data/yt_report.db`` -> ``data/yt_report.lock``)."""
    return database_path.with_suffix(".lock")
