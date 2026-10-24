"""Build privacy-safe context snapshots for review corrections.

A correction must remain useful after the source artifacts disappear, so the
snapshot copies the decision evidence (claims, alternatives, policy and model
versions) at correction time.  It deliberately never copies audio bytes,
cookies, or credentials.  Callers can pass arbitrary ``extra`` metadata; the
same redaction rules apply to it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

from trackforge.application.services.canonical_json import canonical_json
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import FieldValue, validate_field_value
from trackforge.domain.model.claims import Claim
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.item import Item

SNAPSHOT_SCHEMA_VERSION = 1
MAX_TEXT_LENGTH = 4_000
MAX_CLAIMS = 500

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
_AUDIO_KEY_PARTS = (
    "audio",
    "raw_audio",
)

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|cookie|session_id)\b"
        r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
    ),
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    # Review-session identifiers are audit metadata, not credentials.
    if normalized in {"review_session_id", "review_session_ids"}:
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS) or any(
        part in normalized for part in _AUDIO_KEY_PARTS
    )


def redact_secrets(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> Any:
    """Return a JSON-safe copy with credentials/audio removed.

    Redaction is intentionally conservative: sensitive *keys* are replaced with
    ``"***"`` and common inline secret shapes (``token=...``, ``Bearer ...``,
    ``sk-...``) are masked even inside free text.  Ordinary metadata such as a
    song title is left untouched.
    """

    if depth > 20:
        return "<truncated>"
    if seen is None:
        seen = set()

    if isinstance(value, Enum):
        return redact_secrets(value.value, depth=depth + 1, seen=seen)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, Mapping):
        identifier = id(value)
        if identifier in seen:
            return "<circular>"
        seen.add(identifier)
        try:
            result: dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                key = raw_key if isinstance(raw_key, str) else str(raw_key)
                if _is_sensitive_key(key):
                    result[key] = "***"
                else:
                    result[key] = redact_secrets(
                        raw_value,
                        depth=depth + 1,
                        seen=seen,
                    )
            return result
        finally:
            seen.discard(identifier)

    if isinstance(value, (list, tuple, set, frozenset)):
        identifier = id(value)
        if identifier in seen:
            return "<circular>"
        seen.add(identifier)
        try:
            return [redact_secrets(item, depth=depth + 1, seen=seen) for item in value]
        finally:
            seen.discard(identifier)

    if is_dataclass(value) and not isinstance(value, type):
        return redact_secrets(asdict(value), depth=depth + 1, seen=seen)

    text = str(value)
    return _redact_text(text)


def _redact_text(text: str) -> str:
    if len(text) > MAX_TEXT_LENGTH:
        text = f"{text[:MAX_TEXT_LENGTH]}…"
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda match: f"{match.group(1)}=***", text)
        else:
            text = pattern.sub("***", text)
    return text


_ClaimLike = Claim | Mapping[str, Any]


def _claim_identity(claim: _ClaimLike) -> str:
    if isinstance(claim, Mapping):
        return str(claim.get("id"))
    return str(claim.id)


def _claim_attribute(claim: _ClaimLike, name: str, default: Any = None) -> Any:
    if isinstance(claim, Mapping):
        return claim.get(name, default)
    return getattr(claim, name, default)


def _claim_document(claim: _ClaimLike) -> dict[str, Any]:
    if isinstance(claim, Mapping):
        derived_from = claim.get("derived_from") or ()
        return {
            "id": _claim_identity(claim),
            "field": claim.get("field"),
            "value": claim.get("value"),
            "source": claim.get("source"),
            "method": claim.get("method"),
            "confidence": float(claim.get("confidence") or 0.0),
            "status": str(claim.get("status") or "proposed"),
            "role": claim.get("role"),
            "derived_from": [str(item) for item in derived_from],
        }
    return {
        "id": str(claim.id),
        "field": claim.field,
        "value": claim.value,
        "source": claim.source,
        "method": claim.method,
        "confidence": float(claim.confidence),
        "status": claim.status.value,
        "role": claim.role,
        "derived_from": [str(item) for item in claim.derived_from],
    }


def _redacted_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_secrets(value)
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def _source_ref_document(source_ref: object | None) -> dict[str, Any] | None:
    if source_ref is None:
        return None
    if isinstance(source_ref, Mapping):
        return _redacted_mapping(source_ref)
    if is_dataclass(source_ref) and not isinstance(source_ref, type):
        document = asdict(source_ref)
        document["type"] = type(source_ref).__name__
        return _redacted_mapping(document)
    return {"type": type(source_ref).__name__, "value": redact_secrets(str(source_ref))}


def build_context_snapshot(
    *,
    item: Item | None = None,
    item_id: object | None = None,
    field: str,
    system_value: FieldValue,
    user_value: FieldValue,
    decision_record: DecisionRecord | None = None,
    decision: Decision | None = None,
    claims: Sequence[_ClaimLike] = (),
    alternatives: Sequence[_ClaimLike] = (),
    policy_versions: Sequence[str] = (),
    model: str | None = None,
    prompt_version: str | None = None,
    source_ref: object | None = None,
    review_session_id: object | None = None,
    extra: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the documented correction snapshot.

    ``claims`` should include the new user claim when one is created.  The
    chosen claim's source/method are duplicated at the top level so statistics
    can group corrections by the system evidence that was corrected.
    """

    if not isinstance(field, str) or not field.strip():
        raise DomainValidationError("context snapshot requires a field")
    system = validate_field_value(system_value, "system_value")
    user = validate_field_value(user_value, "user_value")

    resolved_item_id = item.id if item is not None else item_id
    if resolved_item_id is None:
        raise DomainValidationError("context snapshot requires an item id")

    chosen = None
    if decision is not None and decision.chosen_claim_id is not None:
        chosen = next(
            (claim for claim in claims if _claim_identity(claim) == str(decision.chosen_claim_id)),
            None,
        )

    records = list(claims)[:MAX_CLAIMS]
    known_ids = {_claim_identity(claim) for claim in records}
    alternative_documents = [
        {
            **_claim_document(claim),
            "reason": "not selected by the active policy",
        }
        for claim in alternatives
        if _claim_identity(claim) in known_ids or len(records) < MAX_CLAIMS
    ]
    # Preserve stable ordering: alternatives follow the claim ordering used by
    # the decision, not the order they happened to be passed to this helper.
    alternative_documents.sort(key=lambda doc: str(doc["id"]))

    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "item_id": str(resolved_item_id),
        "field": field,
        "system_value": system,
        "user_value": user,
        "claims": [_claim_document(claim) for claim in records],
        "alternatives": alternative_documents,
        "policy_versions": [str(value) for value in policy_versions],
        "model": model,
        "prompt": prompt_version,
        "prompt_version": prompt_version,
        "decision_record_id": (str(decision_record.id) if decision_record is not None else None),
        "decision_id": str(decision.id) if decision is not None and decision.id else None,
        "chosen_claim_id": (
            str(decision.chosen_claim_id)
            if decision is not None and decision.chosen_claim_id is not None
            else None
        ),
        "system_source": None if chosen is None else _claim_attribute(chosen, "source"),
        "system_method": None if chosen is None else _claim_attribute(chosen, "method"),
        "mode": decision.mode.value if decision is not None else None,
        "locked": bool(decision.locked) if decision is not None else False,
        "source_ref": _source_ref_document(source_ref),
        "review_session_id": (str(review_session_id) if review_session_id is not None else None),
        "created_at": created_at.isoformat() if created_at is not None else None,
    }
    if extra:
        snapshot.update(_redacted_mapping(extra))
    return _redacted_mapping(snapshot)


def snapshot_is_json_safe(snapshot: Mapping[str, Any]) -> bool:
    """Return whether the snapshot can be persisted as canonical JSON."""

    try:
        canonical_json(dict(snapshot))
    except (TypeError, ValueError):
        return False
    return True


class ContextSnapshotBuilder:
    """Small object wrapper for callers that prefer an injectable service."""

    def build(self, **kwargs: Any) -> dict[str, Any]:
        return build_context_snapshot(**kwargs)


# Compatibility aliases used by tests and future callers.
ContextSnapshotService = ContextSnapshotBuilder
ContextSnapshot = build_context_snapshot


__all__ = [
    "ContextSnapshot",
    "ContextSnapshotBuilder",
    "ContextSnapshotService",
    "MAX_CLAIMS",
    "SNAPSHOT_SCHEMA_VERSION",
    "build_context_snapshot",
    "redact_secrets",
    "snapshot_is_json_safe",
]
