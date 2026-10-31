"""JSON serialization and redaction for the HTTP transport.

DTOs stay the canonical contract; this module only converts frozen dataclasses
to JSON-compatible values, keeps source references round-trippable, and
scrubs values whose keys look like credentials.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, cast

from fastapi.encoders import jsonable_encoder

from trackforge.application.dto.decisions import ExplanationView
from trackforge.application.dto.plan import PlanStepView, PlanView
from trackforge.application.dto.results import DoctorReport, RunResult, StatusView
from trackforge.application.dto.views import DecisionView, ItemView, LibraryEntryView, ReviewView
from trackforge.application.ports.models import ModelInfo
from trackforge.application.services.sidecar import sanitize_source_url
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.capabilities import Cost
from trackforge.domain.model.ids import CandidateId, new_uuid7
from trackforge.domain.model.source import (
    Candidate,
    CollectionKind,
    CollectionRef,
    Format,
    LocalPath,
    SourceRef,
    TextQuery,
    Thumbnail,
    Url,
)
from trackforge.pipeline.events import Event

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "session_id",
    "token",
)
_SAFE_SENSITIVE_KEYS = frozenset({"token_path", "token_file", "api_key_env"})
_URL_RE = re.compile(r"https?://[^\s\"']+")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|cookie|password|passwd|secret)\b"
    r"\s*[:=]\s*[^\s,;]+"
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _SAFE_SENSITIVE_KEYS:
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any, *, secret: str | None = None) -> Any:
    """Recursively remove credential-shaped values and an optional token."""

    if isinstance(value, str):
        return _redact_text(value, secret)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            result[key] = "***" if _is_sensitive_key(key) else redact(raw_value, secret=secret)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, secret=secret) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _redact_text(text: str, secret: str | None) -> str:
    if secret and secret in text:
        text = text.replace(secret, "***")
    text = _BEARER_RE.sub(r"\1 ***", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1=***", text)
    text = _URL_RE.sub(lambda match: sanitize_source_url(match.group(0)) or "", text)
    return text


def jsonable(value: Any, *, secret: str | None = None) -> Any:
    """Convert a DTO/event to JSON-compatible, redacted values."""

    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    return redact(jsonable_encoder(value), secret=secret)


def source_to_payload(source: SourceRef) -> dict[str, Any]:
    """Serialize a source reference without losing its variant."""

    if isinstance(source, LocalPath):
        return {"type": "path", "path": source.path}
    if isinstance(source, Url):
        # Strip signed query parameters/tokens before echoing the source.
        sanitized = sanitize_source_url(source.url) or source.url.split("?", 1)[0]
        return {"type": "url", "url": sanitized, "site_hint": source.site_hint}
    if isinstance(source, TextQuery):
        return {"type": "text", "text": source.text, "locale": source.locale}
    if isinstance(source, CollectionRef):
        return {
            "type": "collection",
            "kind": source.kind.value,
            "source": source_to_payload(source.source),
            "external_id": source.external_id,
        }
    raise DomainValidationError(
        "unsupported source reference",
        context={"source_type": type(source).__name__},
    )


def source_from_payload(value: Any) -> SourceRef:
    """Parse a string or source object accepted by the request models."""

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise DomainValidationError("source must not be empty")
        if text.startswith(("http://", "https://")):
            return Url(text)
        return LocalPath(text)
    if isinstance(value, Mapping):
        raw_type = value.get("type")
        raw_kind = str(value.get("kind") or "").strip().lower()
        collection_kinds = {item.value for item in CollectionKind}
        if not raw_type and raw_kind in collection_kinds and "source" in value:
            kind = "collection"
        else:
            kind = str(raw_type or raw_kind or "").strip().lower()
        if kind == "url" or (not kind and "url" in value):
            url = value.get("url")
            if not isinstance(url, str):
                raise DomainValidationError("source.url is required")
            return Url(url, site_hint=_optional_text(value.get("site_hint")))
        if kind == "path" or (not kind and "path" in value):
            path = value.get("path")
            if not isinstance(path, str):
                raise DomainValidationError("source.path is required")
            return LocalPath(path)
        if kind == "text" or (not kind and "text" in value):
            text_value = value.get("text")
            if not isinstance(text_value, str):
                raise DomainValidationError("source.text is required")
            return TextQuery(text_value, locale=_optional_text(value.get("locale")))
        if kind == "collection":
            inner = value.get("source")
            if inner is None:
                raise DomainValidationError("collection source is required")
            try:
                kind_value = value.get("kind") or value.get("collection_kind")
                collection_kind = CollectionKind(str(kind_value))
            except ValueError:
                raise DomainValidationError(
                    "unknown collection kind",
                    context={"kind": value.get("kind")},
                ) from None
            return CollectionRef(
                kind=collection_kind,
                source=source_from_payload(inner),
                external_id=_optional_text(value.get("external_id")),
            )
    raise DomainValidationError(
        "source must be a string or a {type, path|url|text} object",
        context={"source_type": type(value).__name__},
    )


def plan_to_payload(plan: PlanView) -> dict[str, Any]:
    """Serialize a ``PlanView`` with stable field names."""

    return {
        "type": "plan",
        "source": source_to_payload(plan.source),
        "candidates": [_candidate_to_payload(candidate) for candidate in plan.candidates],
        "steps": [_step_to_payload(step) for step in plan.steps],
        "estimated_cost": _cost_to_payload(plan.estimated_cost),
        "warnings": list(plan.warnings),
        "blocked_reason": plan.blocked_reason,
        "resolver_id": plan.resolver_id,
        "blocked": plan.blocked,
    }


def plan_result_to_payload(plan: PlanView | Any) -> dict[str, Any]:
    """Serialize a plan or collection-plan result."""

    if isinstance(plan, PlanView):
        return plan_to_payload(plan)
    payload = jsonable(plan)
    if isinstance(payload, dict):
        payload.setdefault("type", "collection_plan")
        return payload
    return {"type": "collection_plan", "items": payload}


def plan_from_payload(value: Mapping[str, Any]) -> PlanView:
    """Reconstruct a ``PlanView`` sent back by a client."""

    source_value = value.get("source")
    if source_value is None:
        raise DomainValidationError("plan.source is required")
    raw_candidates = value.get("candidates") or ()
    if isinstance(raw_candidates, (str, bytes)) or not isinstance(raw_candidates, Sequence):
        raise DomainValidationError("plan.candidates must be a list")
    candidates = tuple(_candidate_from_payload(item) for item in raw_candidates)
    raw_steps = value.get("steps") or ()
    if isinstance(raw_steps, (str, bytes)) or not isinstance(raw_steps, Sequence):
        raise DomainValidationError("plan.steps must be a list")
    steps = tuple(_step_from_payload(item) for item in raw_steps)
    return PlanView(
        source=source_from_payload(source_value),
        candidates=candidates,
        steps=steps,
        estimated_cost=_cost_from_payload(value.get("estimated_cost")),
        warnings=tuple(str(item) for item in (value.get("warnings") or ())),
        blocked_reason=_optional_text(value.get("blocked_reason")),
        resolver_id=_optional_text(value.get("resolver_id")),
    )


def candidate_to_payload(candidate: Candidate) -> dict[str, Any]:
    return _candidate_to_payload(candidate)


def run_result_payload(result: RunResult | Any) -> dict[str, Any]:
    """Serialize a run/collection result and add a derived convenience flag."""

    payload = cast(dict[str, Any], jsonable(result))
    if isinstance(result, RunResult):
        payload["succeeded"] = result.succeeded
    return payload


def status_payload(status: StatusView) -> dict[str, Any]:
    return cast(dict[str, Any], jsonable(status))


def item_payload(view: ItemView) -> dict[str, Any]:
    return cast(dict[str, Any], jsonable(view))


def decision_payload(view: DecisionView) -> dict[str, Any]:
    return cast(dict[str, Any], jsonable(view))


def review_payload(view: ReviewView) -> dict[str, Any]:
    return cast(dict[str, Any], jsonable(view))


def explanation_payload(view: ExplanationView) -> dict[str, Any]:
    return cast(dict[str, Any], jsonable(view))


def library_payload(entries: Sequence[LibraryEntryView]) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], jsonable(entry)) for entry in entries]


def doctor_payload(report: DoctorReport) -> dict[str, Any]:
    payload = cast(dict[str, Any], jsonable(report))
    payload["status"] = report.status.value
    payload["summary"] = report.summary
    payload["critical"] = report.critical
    return payload


def models_payload(models: Sequence[ModelInfo]) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], redact(model.as_dict())) for model in models]


def event_payload(event: Event) -> dict[str, Any]:
    return cast(dict[str, Any], jsonable(event))


def _candidate_to_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "id": str(candidate.id),
        "source_ref": source_to_payload(candidate.source_ref),
        "external_id": candidate.external_id,
        "title": candidate.title,
        "uploader": candidate.uploader,
        "duration_seconds": candidate.duration_seconds,
        "formats": [
            {
                "codec": item.codec,
                "bitrate": item.bitrate,
                "sample_rate": item.sample_rate,
                "channels": item.channels,
                "language": item.language,
                "size": item.size,
                # Signed media URLs must not be echoed to another client.
                "url": None,
                "drm": item.drm,
                "container": item.container,
                "format_id": item.format_id,
            }
            for item in candidate.formats
        ],
        "thumbnails": [
            {"url": item.url, "width": item.width, "height": item.height}
            for item in candidate.thumbnails
        ],
        "extra": redact(candidate.extra),
    }


def _candidate_from_payload(value: Any) -> Candidate:
    if not isinstance(value, Mapping):
        raise DomainValidationError("plan candidate must be an object")
    source_ref = source_from_payload(value.get("source_ref") or value.get("source"))
    raw_formats = value.get("formats") or ()
    formats = tuple(
        Format(
            codec=_optional_text(item.get("codec")),
            bitrate=_optional_int(item.get("bitrate")),
            sample_rate=_optional_int(item.get("sample_rate")),
            channels=_optional_int(item.get("channels")),
            language=_optional_text(item.get("language")),
            size=_optional_int(item.get("size")),
            url=_optional_text(item.get("url")),
            drm=bool(item.get("drm", False)),
            container=_optional_text(item.get("container")),
            format_id=_optional_text(item.get("format_id")),
        )
        for item in raw_formats
        if isinstance(item, Mapping)
    )
    raw_thumbnails = value.get("thumbnails") or ()
    thumbnails = tuple(
        Thumbnail(
            url=str(item.get("url") or ""),
            width=_optional_int(item.get("width")),
            height=_optional_int(item.get("height")),
        )
        for item in raw_thumbnails
        if isinstance(item, Mapping)
    )
    identifier = _optional_text(value.get("id"))
    return Candidate(
        id=CandidateId(identifier) if identifier else CandidateId(new_uuid7()),
        source_ref=source_ref,
        external_id=str(value.get("external_id") or ""),
        title=str(value.get("title") or ""),
        uploader=_optional_text(value.get("uploader")),
        duration_seconds=_optional_float(value.get("duration_seconds")),
        formats=formats,
        thumbnails=thumbnails,
        extra=dict(value.get("extra") or {}),
    )


def _step_to_payload(step: PlanStepView) -> dict[str, Any]:
    return {
        "node_id": step.node_id,
        "capability_id": step.capability_id,
        "description": step.description,
        "requires": list(step.requires),
        "provides": list(step.provides),
        "estimated_cost": _cost_to_payload(step.estimated_cost),
    }


def _step_from_payload(value: Any) -> PlanStepView:
    if not isinstance(value, Mapping):
        raise DomainValidationError("plan step must be an object")
    return PlanStepView(
        node_id=str(value.get("node_id") or ""),
        capability_id=str(value.get("capability_id") or ""),
        description=str(value.get("description") or ""),
        requires=tuple(str(item) for item in (value.get("requires") or ())),
        provides=tuple(str(item) for item in (value.get("provides") or ())),
        estimated_cost=_cost_from_payload(value.get("estimated_cost")),
    )


def _cost_to_payload(cost: Cost) -> dict[str, Any]:
    return {
        "cpu_seconds": cost.cpu_seconds,
        "memory_mb": cost.memory_mb,
        "network_calls": cost.network_calls,
        "money_usd": cost.money_usd,
    }


def _cost_from_payload(value: Any) -> Cost:
    if value is None:
        return Cost()
    if not isinstance(value, Mapping):
        raise DomainValidationError("estimated_cost must be an object")
    return Cost(
        cpu_seconds=_optional_float(value.get("cpu_seconds")) or 0.0,
        memory_mb=_optional_float(value.get("memory_mb")) or 0.0,
        network_calls=_optional_int(value.get("network_calls")) or 0,
        money_usd=_optional_float(value.get("money_usd")) or 0.0,
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "candidate_to_payload",
    "decision_payload",
    "doctor_payload",
    "event_payload",
    "explanation_payload",
    "item_payload",
    "jsonable",
    "library_payload",
    "models_payload",
    "plan_from_payload",
    "plan_result_to_payload",
    "plan_to_payload",
    "redact",
    "review_payload",
    "run_result_payload",
    "source_from_payload",
    "source_to_payload",
    "status_payload",
]
