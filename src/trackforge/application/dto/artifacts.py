"""Pydantic DTOs for artifact manifests.

These models are the persistence schema for artifacts.  They live in the
application layer because the domain deliberately stays free of pydantic.
Unknown fields are rejected so a typo cannot silently change the on-disk
schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trackforge.application.ports.storage import is_sha256_hex
from trackforge.application.services.canonical_json import canonical_json
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.capabilities import ArtifactType


def _require_non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO 8601 UTC string")
    if not value.endswith("Z"):
        raise ValueError(f"{field} must end with 'Z'")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{field} must be a valid ISO 8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value


def _validate_uuid(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("artifact_id must be a UUID string")
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("artifact_id must be a UUID string") from None
    return value


def _validate_hash_field(value: object, field: str) -> str:
    if value == "":
        # The store fills params_hash/payload_hash before writing the manifest.
        return ""
    if not is_sha256_hex(value):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return str(value)


class ArtifactProducer(BaseModel):
    """The capability or external tool that produced an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)

    @field_validator("id", "version")
    @classmethod
    def _validate_text(cls, value: object) -> str:
        return _require_non_empty(value, "producer field")


class ArtifactInput(BaseModel):
    """One provenance input: an artifact type and a content/artifact hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    hash: str = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: object) -> str:
        return _require_non_empty(value, "input type")

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, value: object) -> str:
        return _require_non_empty(value, "input hash")


class ArtifactManifest(BaseModel):
    """The stable schema shared by every cached artifact type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    type: str
    schema_version: int = Field(ge=1)
    producer: ArtifactProducer
    inputs: list[ArtifactInput] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    params_hash: str = ""
    payload_hash: str = ""
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    expires_at: str | None = None
    license: str | None = None

    @field_validator("artifact_id")
    @classmethod
    def _validate_artifact_id(cls, value: object) -> str:
        return _validate_uuid(value)

    @field_validator("type")
    @classmethod
    def _validate_artifact_type(cls, value: object) -> str:
        text = _require_non_empty(value, "type")
        try:
            ArtifactType.parse(text)
        except DomainValidationError as exc:
            raise ValueError(str(exc)) from None
        return text

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: object) -> str:
        return _validate_timestamp(value, "created_at")

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_timestamp(value, "expires_at")

    @field_validator("params_hash", "payload_hash")
    @classmethod
    def _validate_hashes(cls, value: object, info: Any) -> str:
        return _validate_hash_field(value, str(info.field_name))

    @field_validator("license")
    @classmethod
    def _validate_license(cls, value: object) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, "license")

    @model_validator(mode="after")
    def _validate_json_fields(self) -> ArtifactManifest:
        try:
            canonical_json(self.params)
            canonical_json(self.warnings)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"params and warnings must be JSON-safe: {exc}") from None
        return self


__all__ = [
    "ArtifactInput",
    "ArtifactManifest",
    "ArtifactProducer",
]
