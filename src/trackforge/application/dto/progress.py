"""Transport-neutral progress and handler-report DTOs.

These values are observability only: they never replace SQLite/catalog state.
The CLI, the Python API, and a future GUI/SSE endpoint consume the same
snapshot shape.  Labels and summaries are sanitized and truncated here so no
consumer has to re-implement the policy.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from trackforge.application.services.canonical_json import canonical_json
from trackforge.domain.model._validation import (
    coerce_enum,
    ensure_datetime,
    optional_text,
    require_text,
    utc_now,
)

PUBLIC_STAGES: tuple[str, ...] = (
    "plan",
    "acquire",
    "observe",
    "interpret",
    "resolve",
    "render",
    "publish",
)
STAGE_TOTAL = len(PUBLIC_STAGES)
STAGE_INDEX: dict[str, int] = {stage: index for index, stage in enumerate(PUBLIC_STAGES, start=1)}

MAX_LABEL_GRAPHEMES = 80
MAX_SUMMARY_CHARS = 200
MAX_DETAIL_STRING = 500

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://[^\s]+")
_FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "cookie",
        "cookies",
        "description",
        "lyrics",
        "password",
        "query",
        "query_string",
        "request",
        "request_body",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }
)


class ProgressStatus(StrEnum):
    """Lifecycle of one selected collection item."""

    QUEUED = "queued"
    RUNNING = "running"
    CACHED = "cached"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_PROGRESS


class CollectionProgressStatus(StrEnum):
    """Aggregate lifecycle of one progress board scope."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HandlerStatus(StrEnum):
    """Final outcome of one capability invocation."""

    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    CACHED = "cached"
    SKIPPED = "skipped"
    FAILED = "failed"


_TERMINAL_PROGRESS = frozenset(
    {
        ProgressStatus.CACHED,
        ProgressStatus.DONE,
        ProgressStatus.FAILED,
        ProgressStatus.SKIPPED,
    }
)


def stage_index(stage: str) -> int:
    """Return the public 1-based stage index, or raise for unknown stages."""

    key = str(stage).strip().casefold()
    try:
        return STAGE_INDEX[key]
    except KeyError as exc:
        raise ValueError(f"unknown public stage: {stage!r}") from exc


def sanitize_text(value: object, *, limit: int | None = None) -> str:
    """Collapse controls/whitespace and redact URL query secrets."""

    text = str(value)
    text = _CONTROL_RE.sub("", text).replace("\n", " ").replace("\r", " ")
    text = _WHITESPACE_RE.sub(" ", text).strip()

    def redact_url(match: re.Match[str]) -> str:
        from trackforge.application.services.sidecar import sanitize_source_url

        return sanitize_source_url(match.group(0)) or ""

    text = _URL_RE.sub(redact_url, text)
    if limit is not None and limit > 0:
        text = truncate_graphemes(text, limit)
    return text


def truncate_graphemes(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` grapheme clusters."""

    if limit <= 0:
        return ""
    clusters = list(_graphemes(text))
    if len(clusters) <= limit:
        return text
    if limit == 1:
        return "…"
    return "".join(clusters[: limit - 1]) + "…"


def _graphemes(text: str) -> list[str]:
    """Small, dependency-free grapheme cluster splitter.

    It handles combining marks, variation selectors, emoji modifiers, ZWJ
    sequences, and regional-indicator pairs; that is sufficient for labels.
    """

    clusters: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        cluster = text[index]
        index += 1
        if (
            _is_regional_indicator(cluster)
            and index < length
            and _is_regional_indicator(text[index])
        ):
            cluster += text[index]
            index += 1
        while index < length:
            char = text[index]
            if char == "\u200d" and index + 1 < length:
                cluster += char + text[index + 1]
                index += 2
                continue
            if _is_combining(char) or _is_variation_selector(char) or _is_emoji_modifier(char):
                cluster += char
                index += 1
                continue
            break
        clusters.append(cluster)
    return clusters


def _is_combining(char: str) -> bool:
    return unicodedata.combining(char) != 0 or unicodedata.category(char) in {"Mn", "Mc", "Me"}


def _is_variation_selector(char: str) -> bool:
    return "\ufe00" <= char <= "\ufe0f" or "\U000e0100" <= char <= "\U000e01ef"


def _is_emoji_modifier(char: str) -> bool:
    return "\U0001f3fb" <= char <= "\U0001f3ff"


def _is_regional_indicator(char: str) -> bool:
    return "\U0001f1e6" <= char <= "\U0001f1ff"


def sanitize_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a canonical-JSON-safe details mapping without private payloads."""

    if value is None:
        return {}
    sanitized = _sanitize_detail_value(dict(value), depth=0)
    if not isinstance(sanitized, Mapping):
        return {}
    try:
        encoded = canonical_json(sanitized)
    except (TypeError, ValueError):
        return {}
    import json

    decoded = json.loads(encoded)
    return decoded if isinstance(decoded, dict) else {}


def _sanitize_detail_value(value: Any, *, depth: int) -> Any:
    if depth > 6:
        return "…"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return None
        return value
    if isinstance(value, str):
        return sanitize_text(value, limit=MAX_DETAIL_STRING)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = sanitize_text(raw_key, limit=80)
            if not key or key.casefold() in _FORBIDDEN_DETAIL_KEYS:
                continue
            result[key] = _sanitize_detail_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _sanitize_detail_value(item, depth=depth + 1)
            for item in value
            if not isinstance(item, (bytes, bytearray, memoryview))
        ]
    return sanitize_text(value, limit=MAX_DETAIL_STRING)


def _text_tuple(value: Sequence[object]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raw: Sequence[object] = (value,)
    else:
        raw = tuple(value)
    result: list[str] = []
    for item in raw:
        text = sanitize_text(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ProgressContext:
    """Collection/item identity bound to one item's pipeline run."""

    scope_id: str
    collection_id: str
    item_id: str | None
    position: int
    total: int
    label: str
    stage: str = "plan"
    stage_index: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", require_text(self.scope_id, "scope_id"))
        object.__setattr__(
            self,
            "collection_id",
            require_text(self.collection_id, "collection_id"),
        )
        if self.item_id is not None:
            object.__setattr__(self, "item_id", require_text(self.item_id, "item_id"))
        for name in ("position", "total"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        label = sanitize_text(self.label, limit=MAX_LABEL_GRAPHEMES)
        object.__setattr__(self, "label", label or f"item {self.position}")
        normalized_stage = str(self.stage).strip().casefold()
        if normalized_stage not in STAGE_INDEX:
            raise ValueError(f"unknown public stage: {self.stage!r}")
        index = int(self.stage_index)
        if index != STAGE_INDEX[normalized_stage]:
            raise ValueError(f"stage_index {index} does not match stage {normalized_stage!r}")
        object.__setattr__(self, "stage", normalized_stage)
        object.__setattr__(self, "stage_index", index)

    def queued(self) -> ItemProgress:
        return ItemProgress(
            scope_id=self.scope_id,
            collection_id=self.collection_id,
            item_id=self.item_id,
            position=self.position,
            total=self.total,
            label=self.label,
            stage=self.stage,
            stage_index=self.stage_index,
            status=ProgressStatus.QUEUED,
        )


@dataclass(frozen=True, slots=True)
class ItemProgress:
    """Latest observable state of one selected item (never a history log)."""

    scope_id: str
    collection_id: str
    item_id: str | None
    position: int
    total: int
    label: str
    stage: str
    stage_index: int
    stage_total: int = STAGE_TOTAL
    status: ProgressStatus = ProgressStatus.QUEUED
    percent: float | None = None
    message: str | None = None
    elapsed_ms: int = 0
    output_path: str | None = None
    warnings: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", require_text(self.scope_id, "scope_id"))
        object.__setattr__(
            self,
            "collection_id",
            require_text(self.collection_id, "collection_id"),
        )
        if self.item_id is not None:
            object.__setattr__(self, "item_id", require_text(self.item_id, "item_id"))
        for name in ("position", "total"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        label = sanitize_text(self.label, limit=MAX_LABEL_GRAPHEMES)
        object.__setattr__(self, "label", label or f"item {self.position}")
        normalized_stage = str(self.stage).strip().casefold()
        if normalized_stage not in STAGE_INDEX:
            raise ValueError(f"unknown public stage: {self.stage!r}")
        index = int(self.stage_index)
        if index != STAGE_INDEX[normalized_stage]:
            raise ValueError(f"stage_index {index} does not match stage {normalized_stage!r}")
        if int(self.stage_total) != STAGE_TOTAL:
            raise ValueError(f"stage_total must be {STAGE_TOTAL}")
        object.__setattr__(self, "status", coerce_enum(self.status, ProgressStatus, "status"))
        if self.percent is not None:
            if isinstance(self.percent, bool) or not isinstance(self.percent, (int, float)):
                raise ValueError("percent must be a float or None")
            fraction = float(self.percent)
            if not 0.0 <= fraction <= 1.0:
                raise ValueError("percent must be between 0.0 and 1.0")
            object.__setattr__(self, "percent", fraction)
        message = optional_text(self.message, "message")
        object.__setattr__(
            self,
            "message",
            sanitize_text(message, limit=200) if message else None,
        )
        if isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int):
            raise ValueError("elapsed_ms must be an integer")
        if self.elapsed_ms < 0:
            raise ValueError("elapsed_ms must be non-negative")
        if self.output_path is not None:
            object.__setattr__(self, "output_path", str(self.output_path))
        object.__setattr__(self, "warnings", _text_tuple(self.warnings))
        object.__setattr__(self, "updated_at", ensure_datetime(self.updated_at, "updated_at"))

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    def percent_text(self) -> str:
        return f"{self.percent:.0%}" if self.percent is not None else "…"


@dataclass(frozen=True, slots=True)
class CollectionProgressSnapshot:
    """One immutable read of a board scope."""

    collection_id: str
    status: CollectionProgressStatus
    started_at: datetime | None
    updated_at: datetime
    completed: int
    failed: int
    skipped: int
    remaining: int
    items: tuple[ItemProgress, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "collection_id",
            require_text(self.collection_id, "collection_id"),
        )
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, CollectionProgressStatus, "status"),
        )
        if self.started_at is not None:
            object.__setattr__(
                self,
                "started_at",
                ensure_datetime(self.started_at, "started_at"),
            )
        object.__setattr__(self, "updated_at", ensure_datetime(self.updated_at, "updated_at"))
        for name in ("completed", "failed", "skipped", "remaining"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "items", tuple(sorted(self.items, key=lambda item: item.position)))

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def finished(self) -> bool:
        return self.status in {
            CollectionProgressStatus.COMPLETED,
            CollectionProgressStatus.PARTIAL,
            CollectionProgressStatus.FAILED,
            CollectionProgressStatus.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class HandlerReport:
    """Human- and machine-readable outcome of one capability/stage."""

    scope_id: str
    collection_id: str | None
    item_id: str | None
    position: int
    capability_id: str
    provider: str | None
    kind: str
    stage: str
    stage_index: int
    status: HandlerStatus
    started_at: datetime
    duration_ms: int
    attempts: int
    cache_hit: bool
    artifacts: tuple[str, ...]
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", require_text(self.scope_id, "scope_id"))
        if self.collection_id is not None:
            object.__setattr__(
                self,
                "collection_id",
                require_text(self.collection_id, "collection_id"),
            )
        if self.item_id is not None:
            object.__setattr__(self, "item_id", require_text(self.item_id, "item_id"))
        if isinstance(self.position, bool) or not isinstance(self.position, int):
            raise ValueError("position must be an integer")
        if self.position < 0:
            raise ValueError("position must be non-negative")
        object.__setattr__(
            self,
            "capability_id",
            require_text(self.capability_id, "capability_id"),
        )
        if self.provider is not None:
            object.__setattr__(self, "provider", require_text(self.provider, "provider"))
        object.__setattr__(self, "kind", require_text(self.kind, "kind"))
        normalized_stage = str(self.stage).strip().casefold()
        if normalized_stage not in STAGE_INDEX:
            raise ValueError(f"unknown public stage: {self.stage!r}")
        index = int(self.stage_index)
        if index != STAGE_INDEX[normalized_stage]:
            raise ValueError(f"stage_index {index} does not match stage {normalized_stage!r}")
        object.__setattr__(self, "stage", normalized_stage)
        object.__setattr__(self, "stage_index", index)
        object.__setattr__(self, "status", coerce_enum(self.status, HandlerStatus, "status"))
        object.__setattr__(self, "started_at", ensure_datetime(self.started_at, "started_at"))
        for name in ("duration_ms", "attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.cache_hit, bool):
            raise ValueError("cache_hit must be a boolean")
        if self.status is HandlerStatus.CACHED:
            object.__setattr__(self, "duration_ms", 0)
            object.__setattr__(self, "cache_hit", True)
        object.__setattr__(self, "artifacts", _text_tuple(self.artifacts))
        summary = sanitize_text(self.summary, limit=MAX_SUMMARY_CHARS)
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[: MAX_SUMMARY_CHARS - 1] + "…"
        object.__setattr__(self, "summary", summary or self.capability_id)
        object.__setattr__(self, "details", sanitize_details(self.details))
        object.__setattr__(self, "warnings", _text_tuple(self.warnings))
        object.__setattr__(self, "error", optional_text(self.error, "error"))

    def summary_text(self) -> str:
        return self.summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "collection_id": self.collection_id,
            "item_id": self.item_id,
            "position": self.position,
            "capability_id": self.capability_id,
            "provider": self.provider,
            "kind": self.kind,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "cache_hit": self.cache_hit,
            "artifacts": list(self.artifacts),
            "summary": self.summary,
            "details": dict(self.details),
            "warnings": list(self.warnings),
            "error": self.error,
        }


__all__ = [
    "CollectionProgressSnapshot",
    "CollectionProgressStatus",
    "HandlerReport",
    "HandlerStatus",
    "ItemProgress",
    "MAX_LABEL_GRAPHEMES",
    "MAX_SUMMARY_CHARS",
    "PUBLIC_STAGES",
    "ProgressContext",
    "ProgressStatus",
    "STAGE_INDEX",
    "STAGE_TOTAL",
    "sanitize_details",
    "sanitize_text",
    "stage_index",
    "truncate_graphemes",
]
