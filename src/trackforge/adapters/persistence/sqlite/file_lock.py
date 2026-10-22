"""Single-writer file lock for a TrackForge data directory.

Linux ``flock`` locks are advisory but reliable between processes: the second
process fails immediately with :class:`CatalogLocked` instead of waiting
forever.  The lock is held for the duration of a run/command and released in
``finally`` by the context manager.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import suppress
from pathlib import Path
from types import TracebackType

from trackforge.domain.errors import CatalogError, CatalogLocked

_LOCK_FILE_NAME = ".lock"


class FileLock:
    """An exclusive, non-blocking lock on ``<data_dir>/.lock``."""

    def __init__(self, data_dir: Path) -> None:
        candidate = Path(data_dir)
        if candidate.name == _LOCK_FILE_NAME:
            self._data_dir = candidate.parent
            self._path = candidate
        else:
            self._data_dir = candidate
            self._path = candidate / _LOCK_FILE_NAME
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        """Path of the lock file."""

        return self._path

    @property
    def locked(self) -> bool:
        """Whether this instance currently holds the lock."""

        return self._fd is not None

    def acquire(self) -> None:
        """Acquire the lock or raise :class:`CatalogLocked` immediately."""

        if self._fd is not None:
            return
        try:
            created = not self._data_dir.exists()
            self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created:
                os.chmod(self._data_dir, 0o700)
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise CatalogError(
                "cannot create the TrackForge data-directory lock",
                context={"path": str(self._path), "error": str(exc)},
            ) from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = _read_owner_pid(fd)
            os.close(fd)
            raise CatalogLocked(
                "another TrackForge process is using this data directory",
                context={
                    "data_dir": str(self._data_dir),
                    "lock_file": str(self._path),
                    "owner_pid": owner,
                },
            ) from None
        except OSError as exc:
            os.close(fd)
            raise CatalogError(
                "cannot acquire the TrackForge data-directory lock",
                context={"path": str(self._path), "error": str(exc)},
            ) from exc

        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            # Diagnostics are best-effort; the flock itself is already held.
            pass
        self._fd = fd

    def release(self) -> None:
        """Release the lock if this instance holds it."""

        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    async def __aenter__(self) -> FileLock:
        self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        with suppress(Exception):
            self.release()


def _read_owner_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


__all__ = ["CatalogLocked", "FileLock"]
