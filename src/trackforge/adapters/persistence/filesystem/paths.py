"""Filesystem safety and atomic-write primitives.

Every path crossing into an adapter goes through :func:`ensure_within`; writes
go to a temporary file in the cache directory and are committed with
``os.replace`` after ``fsync``.  Temporary files are removed when an operation
fails, so a crash cannot leave a half-written object at its final path.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from trackforge.application.ports.storage import ArtifactCorrupted, PathEscapeError

__all__ = [
    "PathEscapeError",
    "atomic_write_bytes",
    "atomic_writer",
    "commit_temp",
    "ensure_private_directory",
    "ensure_within",
    "fsync_directory",
    "open_binary",
    "open_temp_file",
    "seal_temp",
    "stage_bytes",
]


def ensure_within(root: Path, path: Path) -> Path:
    """Resolve ``path`` and require it to stay inside ``root``.

    Relative paths are interpreted relative to ``root``.  Any ``..`` component
    is rejected even when it would normalize back inside the root, and symlinks
    are resolved before the containment check.  The returned path is absolute
    and resolved, which prevents a symlink from redirecting a later write.
    """

    root_path = Path(root).expanduser()
    candidate = Path(path).expanduser()

    if ".." in candidate.parts:
        raise PathEscapeError(
            "path must not contain '..'",
            context={"root": str(root_path), "path": str(candidate)},
        )

    if not root_path.is_absolute():
        root_path = Path.cwd() / root_path
    combined = candidate if candidate.is_absolute() else root_path / candidate

    try:
        resolved_root = root_path.resolve(strict=False)
        resolved_candidate = combined.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathEscapeError(
            "path could not be resolved safely",
            context={"root": str(root_path), "path": str(candidate)},
        ) from exc

    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        raise PathEscapeError(
            "path escapes its allowed root",
            context={"root": str(resolved_root), "path": str(resolved_candidate)},
        ) from None

    return resolved_candidate


def ensure_private_directory(path: Path, *, mode: int = 0o700) -> Path:
    """Create ``path`` if needed and enforce a private directory mode."""

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    try:
        os.chmod(path, mode)
    except OSError:
        # The directory may live on a filesystem that does not support modes;
        # the caller still owns containment checks.
        pass
    return path


def fsync_directory(path: Path) -> None:
    """Best-effort ``fsync`` of a directory entry after a rename."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def open_binary(path: Path) -> BinaryIO:
    """Open a regular file for binary reading without following a final symlink."""

    path = Path(path)
    if path.is_symlink():
        raise PathEscapeError(
            "refusing to open a symlink",
            context={"path": str(path)},
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathEscapeError(
                "refusing to follow a symlink",
                context={"path": str(path)},
            ) from exc
        raise

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactCorrupted(
                "expected a regular file",
                context={"path": str(path)},
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def open_temp_file(
    tmp_dir: Path,
    *,
    prefix: str,
    mode: int = 0o600,
) -> tuple[BinaryIO, Path]:
    """Create a private temporary file and return its stream and path."""

    ensure_private_directory(tmp_dir)
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=tmp_dir)
    path = Path(name)
    try:
        os.fchmod(descriptor, mode)
        stream = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return stream, path


def seal_temp(stream: BinaryIO, *, mode: int = 0o644) -> None:
    """Flush, fsync, chmod, and close a temporary file before committing it."""

    try:
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)
    finally:
        stream.close()


def stage_bytes(data: bytes, tmp_dir: Path, *, prefix: str, mode: int = 0o644) -> Path:
    """Write ``data`` to a sealed temporary file and return its path."""

    stream, path = open_temp_file(tmp_dir, prefix=prefix)
    try:
        stream.write(data)
        seal_temp(stream, mode=mode)
    except BaseException:
        try:
            stream.close()
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path


def commit_temp(temp: Path, target: Path, *, mode: int = 0o644) -> None:
    """Atomically move a sealed temporary file to ``target``.

    ``os.replace`` is retried through a local copy when the cache and data
    directories are on different filesystems.  The final rename still happens
    in the destination directory, so readers never see a partial file.
    """

    temp = Path(temp)
    target = Path(target)
    ensure_private_directory(target.parent)

    try:
        os.replace(temp, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_across_devices(temp, target, mode=mode)

    fsync_directory(target.parent)
    temp.unlink(missing_ok=True)


def _copy_across_devices(temp: Path, target: Path, *, mode: int) -> None:
    stream, local_temp = open_temp_file(
        target.parent,
        prefix=f".{target.name}-",
        mode=0o600,
    )
    committed = False
    try:
        with temp.open("rb") as source:
            shutil.copyfileobj(source, stream, length=1024 * 1024)
        seal_temp(stream, mode=mode)
        os.replace(local_temp, target)
        committed = True
    finally:
        if not committed:
            try:
                stream.close()
            except OSError:
                pass
            local_temp.unlink(missing_ok=True)


@contextmanager
def atomic_writer(
    target: Path,
    *,
    tmp_dir: Path,
    mode: int = 0o644,
) -> Iterator[BinaryIO]:
    """Yield a stream that is atomically committed to ``target`` on success."""

    target = Path(target)
    stream, temp = open_temp_file(tmp_dir, prefix=f"{target.name}-")
    committed = False
    try:
        yield stream
        seal_temp(stream, mode=mode)
        commit_temp(temp, target, mode=mode)
        committed = True
    finally:
        if not committed:
            try:
                stream.close()
            except OSError:
                pass
            temp.unlink(missing_ok=True)


def atomic_write_bytes(
    target: Path,
    data: bytes,
    *,
    tmp_dir: Path,
    mode: int = 0o644,
) -> None:
    """Atomically write ``data`` to ``target`` using a cache temporary file."""

    with atomic_writer(target, tmp_dir=tmp_dir, mode=mode) as stream:
        stream.write(data)
