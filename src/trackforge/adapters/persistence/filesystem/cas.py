"""Content-addressed filesystem store.

Objects are addressed by the SHA-256 of their bytes and laid out as
``<data_dir>/cas/ab/cd/<sha256>``.  Writes stream through ``cache_dir/tmp``,
are fsynced, and are committed with an atomic rename.  Existing objects are
verified rather than overwritten.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from trackforge.adapters.persistence.filesystem.paths import (
    PathEscapeError,
    atomic_write_bytes,
    commit_temp,
    ensure_private_directory,
    ensure_within,
    open_binary,
    open_temp_file,
    seal_temp,
)
from trackforge.application.ports.storage import (
    ArtifactCorrupted,
    ArtifactNotFound,
    ContentHash,
    require_content_hash,
)
from trackforge.application.services.canonical_json import sha256_hex

__all__ = ["FileSystemCas"]

_DEFAULT_CHUNK_SIZE = 1024 * 1024


class FileSystemCas:
    """An immutable, deduplicating SHA-256 content store."""

    def __init__(
        self,
        data_dir: Path,
        cache_dir: Path,
        *,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        self._data_dir = Path(data_dir)
        self._cache_dir = Path(cache_dir)
        self._root = self._data_dir / "cas"
        self._tmp_dir = self._cache_dir / "tmp"
        self._chunk_size = chunk_size

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def root(self) -> Path:
        return self._root

    @property
    def tmp_dir(self) -> Path:
        return self._tmp_dir

    def path_for(self, hash: ContentHash) -> Path:
        """Return the deterministic path for a content hash without touching disk."""

        content_hash = require_content_hash(hash)
        return self._root / content_hash[:2] / content_hash[2:4] / content_hash

    async def put_bytes(self, data: bytes) -> ContentHash:
        """Store ``data`` and return its content hash."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        payload = bytes(data)
        digest = sha256_hex(payload)
        content_hash = ContentHash(digest)

        self._prepare_layout()
        target = self._safe_path(content_hash)
        if self._object_present(target):
            if not await self.verify(content_hash, expected_size=len(payload)):
                raise ArtifactCorrupted(
                    "existing CAS object is corrupted",
                    context={"hash": digest, "path": str(target)},
                )
            return content_hash

        self._prepare_target_parent(target)
        atomic_write_bytes(target, payload, tmp_dir=self._tmp_dir, mode=0o644)
        return content_hash

    async def put_file(self, path: Path) -> ContentHash:
        """Stream a file into CAS without loading it into memory.

        The source file is only read: its contents and mtime remain unchanged.
        """

        source = Path(path)
        self._prepare_layout()
        stream, temp = open_temp_file(self._tmp_dir, prefix="cas-")
        digest = hashlib.sha256()
        size = 0

        try:
            with source.open("rb") as source_stream:
                while chunk := source_stream.read(self._chunk_size):
                    digest.update(chunk)
                    stream.write(chunk)
                    size += len(chunk)
            seal_temp(stream, mode=0o644)
        except BaseException:
            try:
                stream.close()
            except OSError:
                pass
            temp.unlink(missing_ok=True)
            raise

        content_hash = ContentHash(digest.hexdigest())
        try:
            target = self._safe_path(content_hash)
            if self._object_present(target):
                if not await self.verify(content_hash, expected_size=size):
                    raise ArtifactCorrupted(
                        "existing CAS object is corrupted",
                        context={"hash": str(content_hash), "path": str(target)},
                    )
                return content_hash

            self._prepare_target_parent(target)
            commit_temp(temp, target, mode=0o644)
            return content_hash
        finally:
            temp.unlink(missing_ok=True)

    async def open(self, hash: ContentHash) -> BinaryIO:
        """Open a CAS object for reading without following symlinks."""

        content_hash = require_content_hash(hash)
        target = self._safe_path(content_hash)

        if not target.exists():
            raise ArtifactNotFound(
                "CAS object not found",
                context={"hash": str(content_hash)},
            )
        if not target.is_file():
            raise ArtifactCorrupted(
                "CAS object is not a regular file",
                context={"hash": str(content_hash), "path": str(target)},
            )
        try:
            return open_binary(target)
        except FileNotFoundError as exc:
            raise ArtifactNotFound(
                "CAS object not found",
                context={"hash": str(content_hash)},
            ) from exc
        except PathEscapeError as exc:
            raise ArtifactCorrupted(
                "CAS object is not a safe regular file",
                context={"hash": str(content_hash)},
            ) from exc

    async def exists(self, hash: ContentHash) -> bool:
        """Return whether a regular object exists at the hash path."""

        content_hash = require_content_hash(hash)
        try:
            target = self._safe_path(content_hash)
        except PathEscapeError:
            return False
        return target.is_file() and not target.is_symlink()

    async def verify(self, hash: ContentHash, expected_size: int | None = None) -> bool:
        """Stream the object and compare its hash and optional size."""

        content_hash = require_content_hash(hash)
        if expected_size is not None and expected_size < 0:
            return False
        try:
            target = self._safe_path(content_hash)
        except PathEscapeError:
            return False
        if target.is_symlink() or not target.is_file():
            return False

        try:
            if expected_size is not None and target.stat().st_size != expected_size:
                return False
            digest = hashlib.sha256()
            with open_binary(target) as stream:
                while chunk := stream.read(self._chunk_size):
                    digest.update(chunk)
        except (OSError, ArtifactCorrupted):
            return False
        return digest.hexdigest() == content_hash

    def _prepare_layout(self) -> None:
        ensure_private_directory(self._data_dir)
        ensure_private_directory(self._cache_dir)
        root = ensure_within(self._data_dir, self._root)
        ensure_private_directory(root)
        tmp_dir = ensure_within(self._cache_dir, self._tmp_dir)
        ensure_private_directory(tmp_dir)

    def _safe_path(self, hash: ContentHash) -> Path:
        return ensure_within(self._data_dir, self.path_for(hash))

    def _prepare_target_parent(self, target: Path) -> None:
        # Each fan-out directory is created explicitly so parents do not keep
        # the process umask's wider default mode.
        ensure_private_directory(target.parent.parent)
        ensure_private_directory(target.parent)

    @staticmethod
    def _object_present(path: Path) -> bool:
        return path.exists() or path.is_symlink()
