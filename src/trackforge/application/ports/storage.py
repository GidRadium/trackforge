"""Ports for content-addressed bytes and cached artifacts.

The storage layer is intentionally free of catalogue or pipeline policy: it
stores immutable bytes by hash and typed artifacts by caller-provided cache
key.  Concrete filesystem implementations live in
:mod:`trackforge.adapters.persistence.filesystem`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, NewType, Protocol, runtime_checkable

from trackforge.domain.errors import (
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactNotFound,
    InvalidStorageKey,
    PathEscapeError,
    StorageError,
)

if TYPE_CHECKING:
    from trackforge.application.dto.artifacts import ArtifactManifest

ContentHash = NewType("ContentHash", str)
CacheKey = NewType("CacheKey", str)
PayloadFormat = Literal["json", "bin"]

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def is_sha256_hex(value: object) -> bool:
    """Return whether ``value`` is a lowercase, 64-character SHA-256 hex string."""

    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def require_content_hash(value: str) -> ContentHash:
    """Validate and brand a content hash."""

    if not is_sha256_hex(value):
        raise InvalidStorageKey(
            "content hash must be 64 lowercase hexadecimal characters",
            context={"value": str(value)},
        )
    return ContentHash(value)


def require_cache_key(value: str) -> CacheKey:
    """Validate and brand an artifact cache key.

    A cache key is opaque data, never a path: separators, ``..``, empty strings,
    and any other non-SHA-256 text are rejected.
    """

    if not is_sha256_hex(value):
        raise InvalidStorageKey(
            "cache_key must be 64 lowercase hexadecimal characters",
            context={"value": str(value)},
        )
    return CacheKey(value)


def validate_payload_format(value: str) -> PayloadFormat:
    """Validate a payload format at a runtime boundary."""

    if value == "json":
        return "json"
    if value == "bin":
        return "bin"
    raise InvalidStorageKey(
        "payload_format must be 'json' or 'bin'",
        context={"value": str(value)},
    )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A located artifact; paths always point inside the artifact root."""

    cache_key: str
    manifest_path: Path
    payload_path: Path
    payload_hash: str


@runtime_checkable
class ContentAddressedStore(Protocol):
    """Immutable byte storage addressed by the SHA-256 of its contents."""

    async def put_bytes(self, data: bytes) -> ContentHash:
        """Store bytes and return their content hash."""

    async def put_file(self, path: Path) -> ContentHash:
        """Store a file without loading it entirely into memory."""

    async def open(self, hash: ContentHash) -> BinaryIO:
        """Open stored bytes for reading."""

    async def exists(self, hash: ContentHash) -> bool:
        """Return whether a valid-looking object exists for ``hash``."""

    async def verify(self, hash: ContentHash, expected_size: int | None = None) -> bool:
        """Return whether stored bytes match their hash and optional size."""

    def path_for(self, hash: ContentHash) -> Path:
        """Return the deterministic filesystem path for ``hash``."""


@runtime_checkable
class ArtifactStore(Protocol):
    """A cache of manifests and payloads keyed by a caller-provided cache key."""

    async def put(
        self,
        cache_key: str,
        manifest: ArtifactManifest,
        payload: bytes | Path,
        payload_format: PayloadFormat = "json",
    ) -> ArtifactRef:
        """Store an artifact or return the existing equivalent one."""

    async def find(self, cache_key: str) -> ArtifactRef | None:
        """Locate an artifact, returning ``None`` when it does not exist."""

    async def load(self, ref: ArtifactRef) -> tuple[ArtifactManifest, bytes]:
        """Load and verify an artifact's manifest and payload."""

    async def verify(self, ref: ArtifactRef) -> bool:
        """Verify an artifact, raising :class:`ArtifactCorrupted` on mismatch."""

    async def open_payload(self, ref: ArtifactRef) -> BinaryIO:
        """Open an artifact payload for streaming reads."""


__all__ = [
    "ArtifactConflict",
    "ArtifactCorrupted",
    "ArtifactNotFound",
    "ArtifactRef",
    "ArtifactStore",
    "CacheKey",
    "ContentAddressedStore",
    "ContentHash",
    "InvalidStorageKey",
    "PathEscapeError",
    "PayloadFormat",
    "StorageError",
    "is_sha256_hex",
    "require_cache_key",
    "require_content_hash",
    "validate_payload_format",
]
