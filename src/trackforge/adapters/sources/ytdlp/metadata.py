"""Normalize a raw yt-dlp ``info.json`` snapshot.

Two artifacts are produced for every URL acquisition:

``source.snapshot.v1``
    The raw extractor document (sensitive keys removed) kept for audit and
    re-interpretation.

``source.metadata.v1``
    A small, stable subset consumed by the interpretation pipeline.

The observer is deterministic and side-effect free.  It only reads an already
stored snapshot artifact; it never invokes the network.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from trackforge.adapters.http.url_safety import sanitize_url
from trackforge.application.dto.observations import (
    SourceChapter,
    SourceMetadataPayload,
    SourceThumbnail,
)
from trackforge.application.ports.execution import ArtifactDraft, StepContext, StepResult
from trackforge.application.ports.observations import (
    SOURCE_METADATA_ARTIFACT,
    SOURCE_SNAPSHOT_ARTIFACT,
)
from trackforge.domain.errors import DomainValidationError, ExtractorBroken
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)
_SOURCE_METADATA_PRODUCER_ID = "ytdlp"
_SOURCE_METADATA_PRODUCER_VERSION = "1"
_SOURCE_METADATA_CAPABILITY_ID = "source_metadata"


def sanitize_snapshot(value: Any) -> Any:
    """Return a deep copy with cookie/token/credential keys removed."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                continue
            result[key] = sanitize_snapshot(raw_value)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_snapshot(item) for item in value]
    return value


def build_source_metadata(info: Mapping[str, Any]) -> SourceMetadataPayload:
    """Build the normalized ``source.metadata.v1`` model from raw info."""

    external_id = _first_text(info.get("id"), info.get("display_id"))
    if external_id is None:
        source_url = _first_text(
            info.get("webpage_url"),
            info.get("original_url"),
            info.get("url"),
        )
        if source_url is None:
            raise ExtractorBroken("yt-dlp metadata is missing an id")
        external_id = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:24]
    title = _first_text(info.get("title"), info.get("fulltitle"), info.get("track"))
    if title is None:
        title = external_id
    upload_date = _first_text(info.get("upload_date"))
    release_year = _integer(info.get("release_year"))
    if release_year is None and upload_date is not None and len(upload_date) >= 4:
        release_year = _integer(upload_date[:4])
    return SourceMetadataPayload(
        external_id=external_id,
        extractor=_first_text(info.get("extractor"), info.get("extractor_key")),
        title=title,
        uploader=_first_text(info.get("uploader"), info.get("channel"), info.get("creator")),
        artist=_first_text(info.get("artist"), info.get("creator")),
        album=_first_text(info.get("album")),
        track=_first_text(info.get("track")),
        release_year=release_year,
        language=_first_text(info.get("language")),
        description=_first_text(info.get("description")),
        upload_date=upload_date,
        tags=_text_list(info.get("tags")),
        duration=_number(info.get("duration")),
        duration_seconds=_number(info.get("duration")),
        thumbnails=_thumbnails(info.get("thumbnails")),
        chapters=_chapters(info.get("chapters")),
        webpage_url=sanitize_url(_first_text(info.get("webpage_url"))),
    )


class SourceMetadataObserver:
    """Turn the stored raw snapshot into the normalized source.metadata.v1."""

    manifest = CapabilityManifest(
        id=_SOURCE_METADATA_CAPABILITY_ID,
        version=_SOURCE_METADATA_PRODUCER_VERSION,
        kind=CapabilityKind.OBSERVER,
        requires=(SOURCE_SNAPSHOT_ARTIFACT,),
        provides=(SOURCE_METADATA_ARTIFACT,),
        cost=Cost(cpu_seconds=0.01, memory_mb=8.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Normalize the immutable yt-dlp source snapshot.",
    )

    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.inputs.get(SOURCE_SNAPSHOT_ARTIFACT)
        if step is None:
            raise DomainValidationError(
                "source_metadata requires a source.snapshot.v1 input",
                user_action="Acquire the URL again so a snapshot exists.",
            )
        try:
            document = json.loads(step.payload)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ExtractorBroken(
                "source snapshot is not valid JSON",
                context={"artifact_id": step.artifact_id},
            ) from exc
        if not isinstance(document, Mapping):
            raise ExtractorBroken(
                "source snapshot is not a JSON object",
                context={"artifact_id": step.artifact_id},
            )
        payload = build_source_metadata(document)
        return StepResult(
            artifacts=(
                ArtifactDraft(
                    type=SOURCE_METADATA_ARTIFACT,
                    payload=payload.model_dump(mode="json"),
                ),
            )
        )


def _first_text(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _text_list(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in result:
            result.append(item.strip())
    return result


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _thumbnails(value: object) -> list[SourceThumbnail]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[SourceThumbnail] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        url = _first_text(item.get("url"))
        if url is None:
            continue
        result.append(
            SourceThumbnail(
                url=url,
                width=_integer(item.get("width")),
                height=_integer(item.get("height")),
            )
        )
    return result


def _chapters(value: object) -> list[SourceChapter]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[SourceChapter] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            SourceChapter(
                start_seconds=_number(item.get("start_time")),
                end_seconds=_number(item.get("end_time")),
                title=_first_text(item.get("title")),
            )
        )
    return result


normalize_source_metadata = build_source_metadata

__all__ = [
    "SourceMetadataObserver",
    "build_source_metadata",
    "normalize_source_metadata",
    "sanitize_snapshot",
]
