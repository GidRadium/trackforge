"""Build the portable ``trackforge.json`` sidecar and bundle hash.

The sidecar is a snapshot, not a replacement for the catalog.  Its
``trackforge.bundle_hash`` covers every sibling artifact plus a normalized
(``bundle_hash``-free) copy of the sidecar itself.  Normalizing the sidecar
before hashing keeps the hash stable without creating a self-referential
digest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import DomainValidationError

SIDECAR_SCHEMA_VERSION = 1
SIDECAR_FILENAME = "trackforge.json"
BUNDLE_HASH_PLACEHOLDER = ""
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s]+")
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
    }
)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,150}$")


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def sanitize_source_url(url: str | None) -> str | None:
    """Remove credentials/tokens but keep public YouTube video/playlist IDs.

    Source pages sometimes carry signed query parameters or tokens.  They are
    never needed in a portable sidecar, while ``watch?v=...`` and
    ``playlist?list=...`` identify the actual source.
    """

    if url is None:
        return None
    text = url.strip()
    if not text:
        return None
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    host = (parts.hostname or "").casefold()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    path = parts.path.rstrip("/") or parts.path
    allowed = {"v"} if path == "/watch" else {"list"} if path == "/playlist" else set()
    kept: list[tuple[str, str]] = []
    if host in _YOUTUBE_HOSTS and allowed:
        for key, value in parse_qsl(parts.query, keep_blank_values=False):
            if key in allowed and _YOUTUBE_ID_RE.fullmatch(value):
                kept.append((key, value))
    return urlunsplit((parts.scheme, host, parts.path, urlencode(kept), ""))


@dataclass(frozen=True, slots=True)
class SidecarSource:
    """Portable source reference; secret query parameters are stripped."""

    url: str | None = None
    extractor: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        url = sanitize_source_url(self.url)
        source_id = _optional(self.source_id)
        url = _canonical_youtube_url(url, source_id)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "extractor", _optional(self.extractor))
        object.__setattr__(self, "source_id", source_id)

    def document(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "extractor": self.extractor,
            "source_id": self.source_id,
        }


def _canonical_youtube_url(url: str | None, source_id: str | None) -> str | None:
    """Restore ``watch?v=<id>`` for older sidecars whose URL lost its query."""

    if url is None or not source_id:
        return url
    from urllib.parse import parse_qs, quote, urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if (parts.hostname or "").casefold() not in _YOUTUBE_HOSTS or parts.path != "/watch":
        return url
    if parse_qs(parts.query).get("v"):
        return url
    if not _YOUTUBE_ID_RE.fullmatch(source_id):
        return url
    return f"{parts.scheme}://{parts.netloc}/watch?v={quote(source_id)}"


@dataclass(frozen=True, slots=True)
class SidecarIdentity:
    """Identity context available without network providers in v1."""

    kind: str | None = None
    derivative: tuple[str, ...] = ()
    recording_mbid: str | None = None
    release_mbid: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _optional(self.kind))
        object.__setattr__(self, "derivative", tuple(_dedupe(self.derivative)))
        object.__setattr__(self, "recording_mbid", _optional(self.recording_mbid))
        object.__setattr__(self, "release_mbid", _optional(self.release_mbid))

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "derivative": list(self.derivative),
            "recording_mbid": self.recording_mbid,
            "release_mbid": self.release_mbid,
        }


@dataclass(frozen=True, slots=True)
class SidecarArtifacts:
    """References to artifacts that produced the decisions."""

    probe: str | None = None
    features: str | None = None
    lyrics: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "probe", _optional(self.probe))
        object.__setattr__(self, "features", _optional(self.features))
        object.__setattr__(self, "lyrics", _optional(self.lyrics))

    def document(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "features": self.features,
            "lyrics": self.lyrics,
        }


@dataclass(frozen=True, slots=True)
class SidecarCollection:
    """Portable collection context and the tag-basis decision."""

    id: str
    title: str
    kind: str
    position: int
    tag_basis: str
    owner: str | None = None
    alternatives: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "title", "kind", "tag_basis"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"collection {name} must be a non-empty string")
        if isinstance(self.position, bool) or not isinstance(self.position, int):
            raise DomainValidationError("collection position must be an integer")
        if self.position < 1:
            raise DomainValidationError("collection position must be positive")
        object.__setattr__(self, "owner", _optional(self.owner))
        object.__setattr__(
            self,
            "alternatives",
            _json_mapping(_without_url_secrets(self.alternatives)),
        )
        object.__setattr__(
            self,
            "context",
            _json_mapping(_without_url_secrets(self.context)),
        )

    def document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "owner": self.owner,
            "position": self.position,
            "tag_basis": self.tag_basis,
            "alternatives": dict(self.alternatives),
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class SidecarData:
    """Everything needed to serialize one sidecar."""

    item_id: str
    content_hash: str
    decision_record_id: str
    created_at: datetime
    source: SidecarSource = field(default_factory=SidecarSource)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    identity: SidecarIdentity = field(default_factory=SidecarIdentity)
    artifacts: SidecarArtifacts = field(default_factory=SidecarArtifacts)
    warnings: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    collection: SidecarCollection | None = None
    tag_basis: str | None = None
    tag_alternatives: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("item_id", "content_hash", "decision_record_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"{name} must be a non-empty string")
        if not isinstance(self.created_at, datetime):
            raise DomainValidationError("created_at must be a datetime")
        if len(self.content_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.content_hash
        ):
            raise DomainValidationError("content_hash must be a SHA-256 digest")
        if self.collection is not None and not isinstance(self.collection, SidecarCollection):
            raise DomainValidationError("collection must be a SidecarCollection")
        object.__setattr__(self, "tag_basis", _optional(self.tag_basis))
        object.__setattr__(self, "tag_alternatives", _json_mapping(self.tag_alternatives))
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(_without_url_secrets(self.metadata)),
        )
        object.__setattr__(
            self,
            "warnings",
            tuple(_sanitize_text(value) for value in _dedupe(self.warnings)),
        )
        object.__setattr__(self, "unknowns", tuple(_dedupe(self.unknowns)))


def build_sidecar_document(data: SidecarData) -> dict[str, Any]:
    """Build a schema_version 1 sidecar with an empty bundle-hash placeholder."""

    document: dict[str, Any] = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "trackforge": {
            "item_id": data.item_id,
            "content_hash": data.content_hash,
            "decision_record_id": data.decision_record_id,
            "bundle_hash": BUNDLE_HASH_PLACEHOLDER,
            "created_at": _iso_z(data.created_at),
        },
        "source": data.source.document(),
        "metadata": dict(data.metadata),
        "identity": data.identity.document(),
        "artifacts": data.artifacts.document(),
        "collection": data.collection.document() if data.collection is not None else None,
        "tag_basis": data.tag_basis,
        "tag_alternatives": dict(data.tag_alternatives),
        "warnings": list(data.warnings),
        "unknowns": list(data.unknowns),
    }
    # Fail early on values JSON cannot represent; this also catches accidental
    # bytes or datetimes in a metadata snapshot.
    canonical_json(document)
    return document


def compute_bundle_hash(
    files: Sequence[tuple[str, str]],
    sidecar: Mapping[str, Any],
    *,
    sidecar_path: str = SIDECAR_FILENAME,
) -> str:
    """Hash sibling artifacts plus a normalized copy of the sidecar.

    ``files`` contains ``(relative_path, sha256)`` pairs for audio and optional
    cover/lyrics artifacts.  The sidecar's own ``bundle_hash`` field is forced
    to an empty string before hashing, so the final document can embed the
    resulting digest without a circular dependency.
    """

    normalized = _normalized_sidecar(sidecar)
    sidecar_hash = sha256_hex(canonical_json(normalized))
    payload = {
        "files": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(files, key=lambda item: item[0])
        ],
        "sidecar": {"path": sidecar_path, "sha256": sidecar_hash},
    }
    return sha256_hex(canonical_json(payload))


def finalize_sidecar(document: Mapping[str, Any], bundle_hash: str) -> dict[str, Any]:
    """Return a copy of ``document`` with the computed bundle hash attached."""

    if len(bundle_hash) != 64 or any(char not in "0123456789abcdef" for char in bundle_hash):
        raise DomainValidationError("bundle_hash must be a SHA-256 hex digest")
    result = copy.deepcopy(dict(document))
    trackforge = result.get("trackforge")
    if not isinstance(trackforge, dict):
        raise DomainValidationError("sidecar is missing the trackforge object")
    trackforge["bundle_hash"] = bundle_hash
    return result


def sidecar_bytes(document: Mapping[str, Any]) -> bytes:
    """Serialize a sidecar for writing (pretty UTF-8 JSON with a newline)."""

    canonical_json(document)
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )


def write_sidecar(path: Path, document: Mapping[str, Any]) -> bytes:
    """Atomically write the sidecar and return the exact bytes written."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = sidecar_bytes(document)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it fully."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_sidecar(document: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    trackforge = result.get("trackforge")
    if isinstance(trackforge, dict):
        trackforge["bundle_hash"] = BUNDLE_HASH_PLACEHOLDER
    return result


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError("sidecar values must be strings")
    text = value.strip()
    return _sanitize_text(text) or None if text else None


def _sanitize_text(value: str) -> str:
    text = str(value)

    def replace(match: re.Match[str]) -> str:
        return sanitize_source_url(match.group(0)) or ""

    return _URL_IN_TEXT_RE.sub(replace, text)


def _without_url_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {str(key): _without_url_secrets(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_url_secrets(item) for item in value]
    return value


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        serialized = canonical_json(dict(value))
        parsed = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(
            "sidecar metadata must be JSON-compatible",
            context={"error": str(exc)},
        ) from exc
    if not isinstance(parsed, dict):
        raise DomainValidationError("sidecar metadata must be a JSON object")
    return parsed


def _fsync_directory(path: Path) -> None:
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


__all__ = [
    "BUNDLE_HASH_PLACEHOLDER",
    "SIDECAR_FILENAME",
    "SIDECAR_SCHEMA_VERSION",
    "SidecarArtifacts",
    "SidecarCollection",
    "SidecarData",
    "SidecarIdentity",
    "SidecarSource",
    "build_sidecar_document",
    "compute_bundle_hash",
    "finalize_sidecar",
    "sanitize_source_url",
    "sha256_file",
    "sidecar_bytes",
    "write_sidecar",
]
