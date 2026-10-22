"""Filesystem artifact cache.

An artifact is a validated manifest plus exactly one payload:
``<data_dir>/artifacts/<cache_key>/manifest.json`` and ``payload.json`` or
``payload.bin``.  The manifest is written last, so its presence marks a
complete artifact.  Payload bytes are stored exactly as supplied; the store
never re-serializes JSON payloads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from pydantic import ValidationError

from trackforge.adapters.persistence.filesystem.paths import (
    PathEscapeError,
    atomic_write_bytes,
    commit_temp,
    ensure_private_directory,
    ensure_within,
    open_binary,
    open_temp_file,
    seal_temp,
    stage_bytes,
)
from trackforge.application.dto.artifacts import ArtifactManifest
from trackforge.application.ports.storage import (
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactRef,
    InvalidStorageKey,
    PayloadFormat,
    is_sha256_hex,
    require_cache_key,
    require_content_hash,
    validate_payload_format,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex

__all__ = ["FileSystemArtifactStore"]

_MANIFEST_NAME = "manifest.json"
_JSON_PAYLOAD_NAME = "payload.json"
_BIN_PAYLOAD_NAME = "payload.bin"
_PAYLOAD_NAMES: dict[PayloadFormat, str] = {
    "json": _JSON_PAYLOAD_NAME,
    "bin": _BIN_PAYLOAD_NAME,
}
_ALLOWED_PAYLOAD_NAMES = frozenset(_PAYLOAD_NAMES.values())
_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "type",
        "schema_version",
        "producer",
        "inputs",
        "params",
        "params_hash",
        "payload_hash",
        "warnings",
        "created_at",
        "expires_at",
        "license",
    }
)
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _StagedPayload:
    path: Path
    payload_hash: str


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _parse_json(data: bytes, *, context: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{context} is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{context} is not valid JSON: {exc}") from exc


def _validate_json_payload(data: bytes) -> None:
    _parse_json(data, context="payload")


class FileSystemArtifactStore:
    """A filesystem cache of manifests and immutable payload bytes."""

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
        self._root = self._data_dir / "artifacts"
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

    async def put(
        self,
        cache_key: str,
        manifest: ArtifactManifest | Mapping[str, Any],
        payload: bytes | Path,
        payload_format: PayloadFormat = "json",
    ) -> ArtifactRef:
        """Store an artifact, or return the existing equivalent entry.

        Reusing a cache key with a different payload is an explicit conflict;
        existing bytes are never overwritten in that case.
        """

        key = require_cache_key(cache_key)
        fmt = validate_payload_format(payload_format)
        manifest_model = self._coerce_manifest(manifest)

        try:
            params_hash = sha256_hex(canonical_json(manifest_model.params))
        except (TypeError, ValueError) as exc:
            raise ArtifactCorrupted(
                "manifest params are not JSON-safe",
                context={"cache_key": key},
            ) from exc
        if manifest_model.params_hash and manifest_model.params_hash != params_hash:
            raise ArtifactCorrupted(
                "manifest params_hash does not match canonical params",
                context={"cache_key": key},
            )

        staged = self._stage_payload(payload, fmt)
        try:
            if manifest_model.payload_hash and manifest_model.payload_hash != staged.payload_hash:
                raise ArtifactCorrupted(
                    "manifest payload_hash does not match payload bytes",
                    context={
                        "cache_key": key,
                        "manifest_payload_hash": manifest_model.payload_hash,
                        "actual_payload_hash": staged.payload_hash,
                    },
                )

            self._prepare_layout()
            existing = await self.find(key)
            if existing is not None:
                if existing.payload_path.name != _PAYLOAD_NAMES[fmt]:
                    raise ArtifactConflict(
                        "cache_key exists with a different payload format",
                        context={
                            "cache_key": key,
                            "existing_payload": existing.payload_path.name,
                            "requested_payload": _PAYLOAD_NAMES[fmt],
                        },
                    )
                if existing.payload_hash != staged.payload_hash:
                    raise ArtifactConflict(
                        "cache_key exists with a different payload",
                        context={
                            "cache_key": key,
                            "existing_payload_hash": existing.payload_hash,
                            "requested_payload_hash": staged.payload_hash,
                        },
                    )
                await self.verify(existing)
                return existing

            stored_manifest = manifest_model.model_copy(
                update={
                    "params_hash": params_hash,
                    "payload_hash": staged.payload_hash,
                }
            )
            manifest_bytes = canonical_json(stored_manifest.model_dump(mode="json")) + b"\n"

            artifact_dir = self._artifact_dir(key)
            ensure_private_directory(artifact_dir)
            # There is no manifest yet, so any existing payload is an orphan
            # from an interrupted write.  Remove the other format to keep the
            # invariant "exactly one payload per artifact directory".
            for name in _PAYLOAD_NAMES.values():
                if name != _PAYLOAD_NAMES[fmt]:
                    (artifact_dir / name).unlink(missing_ok=True)
            payload_path = artifact_dir / _PAYLOAD_NAMES[fmt]
            commit_temp(staged.path, payload_path, mode=0o644)
            manifest_path = artifact_dir / _MANIFEST_NAME
            atomic_write_bytes(manifest_path, manifest_bytes, tmp_dir=self._tmp_dir, mode=0o644)
            return ArtifactRef(
                cache_key=key,
                manifest_path=manifest_path,
                payload_path=payload_path,
                payload_hash=staged.payload_hash,
            )
        finally:
            staged.path.unlink(missing_ok=True)

    async def find(self, cache_key: str) -> ArtifactRef | None:
        """Locate an artifact, returning ``None`` when no manifest is present."""

        key = require_cache_key(cache_key)
        artifact_dir = self._artifact_dir(key)

        if artifact_dir.is_symlink():
            raise ArtifactCorrupted(
                "artifact entry must not be a symlink",
                context={"cache_key": key, "path": str(artifact_dir)},
            )
        if not artifact_dir.exists():
            return None
        if not artifact_dir.is_dir():
            raise ArtifactCorrupted(
                "artifact entry is not a directory",
                context={"cache_key": key, "path": str(artifact_dir)},
            )

        manifest_path = artifact_dir / _MANIFEST_NAME
        if manifest_path.is_symlink():
            raise ArtifactCorrupted(
                "artifact manifest must not be a symlink",
                context={"cache_key": key, "path": str(manifest_path)},
            )
        if not manifest_path.exists():
            # A payload without a manifest is an interrupted write, not a cache hit.
            return None

        manifest = self._read_manifest(manifest_path, cache_key=key)
        payload_path = self._select_payload(artifact_dir, cache_key=key)
        return ArtifactRef(
            cache_key=key,
            manifest_path=manifest_path,
            payload_path=payload_path,
            payload_hash=manifest.payload_hash,
        )

    async def load(self, ref: ArtifactRef) -> tuple[ArtifactManifest, bytes]:
        """Load a manifest and payload, verifying schema, hashes, and size."""

        key, manifest_path, payload_path = self._validate_ref(ref)
        manifest = self._read_manifest(manifest_path, cache_key=key)
        if manifest.payload_hash != ref.payload_hash:
            raise ArtifactCorrupted(
                "artifact reference payload hash does not match manifest",
                context={"cache_key": key},
            )

        if payload_path.is_symlink() or not payload_path.is_file():
            raise ArtifactCorrupted(
                "artifact payload is missing or not a regular file",
                context={"cache_key": key, "path": str(payload_path)},
            )
        try:
            stat_result = payload_path.stat()
            with open_binary(payload_path) as stream:
                payload = stream.read()
        except PathEscapeError:
            raise
        except OSError as exc:
            raise ArtifactCorrupted(
                "artifact payload cannot be read",
                context={"cache_key": key, "path": str(payload_path)},
            ) from exc

        if len(payload) != stat_result.st_size:
            raise ArtifactCorrupted(
                "artifact payload changed while it was being read",
                context={"cache_key": key},
            )
        if sha256_hex(payload) != manifest.payload_hash:
            raise ArtifactCorrupted(
                "artifact payload hash does not match manifest",
                context={"cache_key": key},
            )
        if payload_path.name == _JSON_PAYLOAD_NAME:
            try:
                _validate_json_payload(payload)
            except ValueError as exc:
                raise ArtifactCorrupted(
                    "artifact payload is not valid JSON",
                    context={"cache_key": key},
                ) from exc
        return manifest, payload

    async def verify(self, ref: ArtifactRef) -> bool:
        """Verify an artifact, raising :class:`ArtifactCorrupted` on mismatch."""

        await self.load(ref)
        return True

    async def open_payload(self, ref: ArtifactRef) -> BinaryIO:
        """Open a verified reference's payload for streaming without hashing it."""

        key, manifest_path, payload_path = self._validate_ref(ref)
        manifest = self._read_manifest(manifest_path, cache_key=key)
        if manifest.payload_hash != ref.payload_hash:
            raise ArtifactCorrupted(
                "artifact reference payload hash does not match manifest",
                context={"cache_key": key},
            )
        if payload_path.is_symlink() or not payload_path.is_file():
            raise ArtifactCorrupted(
                "artifact payload is missing or not a regular file",
                context={"cache_key": key, "path": str(payload_path)},
            )
        try:
            return open_binary(payload_path)
        except FileNotFoundError as exc:
            raise ArtifactCorrupted(
                "artifact payload is missing",
                context={"cache_key": key, "path": str(payload_path)},
            ) from exc

    def _stage_payload(
        self, payload: bytes | Path, payload_format: PayloadFormat
    ) -> _StagedPayload:
        self._prepare_layout()

        if isinstance(payload, Path):
            if payload_format == "json":
                data = payload.read_bytes()
                _validate_json_payload(data)
                return _StagedPayload(
                    path=stage_bytes(data, self._tmp_dir, prefix="artifact-"),
                    payload_hash=sha256_hex(data),
                )
            return self._stage_binary_file(payload)

        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes or a Path")
        data = bytes(payload)
        if payload_format == "json":
            _validate_json_payload(data)
        return _StagedPayload(
            path=stage_bytes(data, self._tmp_dir, prefix="artifact-"),
            payload_hash=sha256_hex(data),
        )

    def _stage_binary_file(self, payload: Path) -> _StagedPayload:
        stream, temp = open_temp_file(self._tmp_dir, prefix="artifact-")
        digest = hashlib.sha256()
        try:
            with payload.open("rb") as source:
                while chunk := source.read(self._chunk_size):
                    digest.update(chunk)
                    stream.write(chunk)
            seal_temp(stream, mode=0o644)
        except BaseException:
            try:
                stream.close()
            except OSError:
                pass
            temp.unlink(missing_ok=True)
            raise
        return _StagedPayload(path=temp, payload_hash=digest.hexdigest())

    def _read_manifest(self, manifest_path: Path, *, cache_key: str) -> ArtifactManifest:
        try:
            stat_result = manifest_path.stat()
            if stat_result.st_size > _MAX_MANIFEST_BYTES:
                raise ArtifactCorrupted(
                    "artifact manifest is unreasonably large",
                    context={"cache_key": cache_key, "path": str(manifest_path)},
                )
            with open_binary(manifest_path) as stream:
                data = stream.read()
        except (ArtifactCorrupted, PathEscapeError):
            raise
        except OSError as exc:
            raise ArtifactCorrupted(
                "artifact manifest cannot be read",
                context={"cache_key": cache_key, "path": str(manifest_path)},
            ) from exc

        try:
            raw = _parse_json(data, context="manifest")
        except ValueError as exc:
            raise ArtifactCorrupted(
                "artifact manifest is not valid JSON",
                context={"cache_key": cache_key, "path": str(manifest_path)},
            ) from exc
        if not isinstance(raw, dict):
            raise ArtifactCorrupted(
                "artifact manifest must be a JSON object",
                context={"cache_key": cache_key},
            )
        missing = sorted(_REQUIRED_MANIFEST_FIELDS - raw.keys())
        if missing:
            raise ArtifactCorrupted(
                "artifact manifest is missing required fields",
                context={"cache_key": cache_key, "missing": missing},
            )

        try:
            manifest = ArtifactManifest.model_validate(raw)
        except ValidationError as exc:
            raise ArtifactCorrupted(
                "artifact manifest does not match the schema",
                context={"cache_key": cache_key, "error": str(exc)},
            ) from exc

        try:
            require_content_hash(manifest.payload_hash)
            require_content_hash(manifest.params_hash)
        except InvalidStorageKey as exc:
            raise ArtifactCorrupted(
                "artifact manifest contains an invalid hash",
                context={"cache_key": cache_key},
            ) from exc

        try:
            actual_params_hash = sha256_hex(canonical_json(manifest.params))
        except (TypeError, ValueError) as exc:
            raise ArtifactCorrupted(
                "artifact manifest params are not JSON-safe",
                context={"cache_key": cache_key},
            ) from exc
        if manifest.params_hash != actual_params_hash:
            raise ArtifactCorrupted(
                "artifact manifest params_hash does not match params",
                context={"cache_key": cache_key},
            )
        return manifest

    def _select_payload(self, artifact_dir: Path, *, cache_key: str) -> Path:
        candidates: list[Path] = []
        for name in _PAYLOAD_NAMES.values():
            candidate = artifact_dir / name
            if candidate.exists() or candidate.is_symlink():
                candidates.append(candidate)

        if len(candidates) != 1:
            raise ArtifactCorrupted(
                "artifact must contain exactly one payload",
                context={"cache_key": cache_key, "payloads": [p.name for p in candidates]},
            )
        payload_path = candidates[0]
        if payload_path.is_symlink() or not payload_path.is_file():
            raise ArtifactCorrupted(
                "artifact payload is not a safe regular file",
                context={"cache_key": cache_key, "path": str(payload_path)},
            )
        return payload_path

    def _validate_ref(self, ref: ArtifactRef) -> tuple[str, Path, Path]:
        key = require_cache_key(ref.cache_key)
        if not is_sha256_hex(ref.payload_hash):
            raise ArtifactCorrupted(
                "artifact reference contains an invalid payload hash",
                context={"cache_key": key},
            )

        expected_dir = self._artifact_dir(key)
        manifest_path = ensure_within(self._data_dir, ref.manifest_path)
        payload_path = ensure_within(self._data_dir, ref.payload_path)

        if manifest_path != expected_dir / _MANIFEST_NAME:
            raise ArtifactCorrupted(
                "artifact reference points at the wrong manifest",
                context={"cache_key": key, "path": str(manifest_path)},
            )
        if payload_path.parent != expected_dir or payload_path.name not in _ALLOWED_PAYLOAD_NAMES:
            raise ArtifactCorrupted(
                "artifact reference points at the wrong payload",
                context={"cache_key": key, "path": str(payload_path)},
            )
        return key, manifest_path, payload_path

    @staticmethod
    def _coerce_manifest(
        manifest: ArtifactManifest | Mapping[str, Any],
    ) -> ArtifactManifest:
        if isinstance(manifest, ArtifactManifest):
            return manifest
        if isinstance(manifest, Mapping):
            return ArtifactManifest.model_validate(manifest)
        raise TypeError("manifest must be an ArtifactManifest or mapping")

    def _artifact_dir(self, cache_key: str) -> Path:
        return ensure_within(self._data_dir, self._root / cache_key)

    def _prepare_layout(self) -> None:
        ensure_private_directory(self._data_dir)
        ensure_private_directory(self._cache_dir)
        root = ensure_within(self._data_dir, self._root)
        ensure_private_directory(root)
        tmp_dir = ensure_within(self._cache_dir, self._tmp_dir)
        ensure_private_directory(tmp_dir)
