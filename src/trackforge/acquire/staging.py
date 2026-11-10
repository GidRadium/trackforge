"""Staging area and collision-safe commits.

Staging is deliberately file-based: a job gets its own directory, downloads
land there, and only a successful commit moves them to the output target.
Collisions use numeric subfolders (``1/``, ``2/``), never ``(1)`` suffixes.
"""

# pyright: standard

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .models import CollisionPolicy


def _coerce_policy(policy: CollisionPolicy | str) -> CollisionPolicy:
    if isinstance(policy, CollisionPolicy):
        return policy
    try:
        return CollisionPolicy(policy)
    except ValueError as exc:
        raise ValueError(f"unknown collision policy: {policy!r}") from exc


def allocate_target(
    target: Path,
    policy: CollisionPolicy = CollisionPolicy.SUBFOLDER,
) -> Path | None:
    """Choose the final path for ``target`` according to ``policy``.

    ``SKIP`` returns ``None``; ``ERROR`` raises :class:`FileExistsError`;
    ``OVERWRITE`` returns ``target`` unchanged; ``SUBFOLDER`` returns the
    first free ``target.parent/<n>/target.name``.
    """
    path = Path(target)
    selected = _coerce_policy(policy)
    if not path.exists():
        return path
    if selected is CollisionPolicy.ERROR:
        raise FileExistsError(path)
    if selected is CollisionPolicy.SKIP:
        return None
    if selected is CollisionPolicy.OVERWRITE:
        return path

    number = 1
    while (path.parent / str(number) / path.name).exists():
        number += 1
    return path.parent / str(number) / path.name


@dataclass(frozen=True, slots=True)
class StagingArea:
    """A root directory containing one subdirectory per job."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser())

    def job_dir(self, job_id: str) -> Path:
        """Create and return an isolated directory for ``job_id``."""
        _validate_component(job_id, "job_id")
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=False)
        return path

    @staticmethod
    def allocate_target(
        target: Path,
        policy: CollisionPolicy = CollisionPolicy.SUBFOLDER,
    ) -> Path | None:
        return allocate_target(target, policy)

    def commit(
        self,
        source: Path,
        target: Path,
        *,
        policy: CollisionPolicy = CollisionPolicy.SUBFOLDER,
    ) -> Path | None:
        """Move ``source`` to a collision-safe ``target``.

        Returns the final path, or ``None`` when the policy is ``SKIP``.
        """
        source_path = Path(source)
        destination = allocate_target(target, policy)
        if destination is None:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        _move(source_path, destination)
        return destination

    def commit_group(
        self,
        media: Path,
        sidecars: Iterable[Path],
        target: Path,
        *,
        policy: CollisionPolicy = CollisionPolicy.SUBFOLDER,
    ) -> Path | None:
        """Commit a media file and all sidecars into one collision folder.

        The subfolder (``1/``, ``2/``, ...) is chosen once for the whole
        group, so a sidecar never ends up in a different directory from its
        media file.
        """
        media_path = Path(media)
        sidecar_paths = tuple(Path(sidecar) for sidecar in sidecars)
        target_path = Path(target)
        selected = _coerce_policy(policy)

        sources = (media_path, *sidecar_paths)
        for source in sources:
            if not source.exists():
                raise FileNotFoundError(source)

        sidecar_names = [sidecar.name for sidecar in sidecar_paths]
        if len(set(sidecar_names) | {target_path.name}) != len(sidecar_names) + 1:
            raise ValueError("media and sidecars must have distinct file names")

        directory = _allocate_group_directory(
            target_path,
            sidecar_names,
            selected,
        )
        if directory is None:
            return None

        directory.mkdir(parents=True, exist_ok=True)
        media_destination = directory / target_path.name
        _move(media_path, media_destination)
        for sidecar, name in zip(sidecar_paths, sidecar_names, strict=True):
            _move(sidecar, directory / name)
        return media_destination


def _allocate_group_directory(
    target: Path,
    sidecar_names: list[str],
    policy: CollisionPolicy,
) -> Path | None:
    """Return the directory for a whole media+sidecars group."""
    names = (target.name, *sidecar_names)

    def occupied(directory: Path) -> bool:
        return any((directory / name).exists() for name in names)

    if not occupied(target.parent):
        return target.parent
    if policy is CollisionPolicy.ERROR:
        raise FileExistsError(target)
    if policy is CollisionPolicy.SKIP:
        return None
    if policy is CollisionPolicy.OVERWRITE:
        return target.parent

    number = 1
    while occupied(target.parent / str(number)):
        number += 1
    return target.parent / str(number)


def _move(source: Path, destination: Path) -> None:
    """Move a file, falling back to copy+fsync+replace across filesystems."""
    try:
        os.replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            with source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        source.unlink()
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _validate_component(value: str, label: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"{label} must be a non-empty path component")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    if "/" in value or "\\" in value:
        raise ValueError(f"{label} must not contain path separators")
    if Path(value).name != value:
        raise ValueError(f"{label} must be a single path component")
