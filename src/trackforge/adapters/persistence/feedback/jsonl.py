"""Append-only JSONL feedback journal and correction snapshot files.

Layout (relative to ``data_dir``)::

    feedback/events.jsonl
    feedback/corrections/<correction-id>.json

Events are canonical JSON (sorted keys, compact separators), one object per
line, UTF-8, and fsynced.  Corrections are written atomically.  Nothing is
ever rewritten or deleted; a duplicate correction id is an error rather than
a silent overwrite.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trackforge.application.ports.feedback import FeedbackConsistency
from trackforge.application.services.canonical_json import canonical_json
from trackforge.application.services.context_snapshot import redact_secrets
from trackforge.domain.errors import (
    DuplicateCorrection,
    FeedbackStorageError,
    InvalidCatalogData,
)
from trackforge.domain.model._validation import (
    coerce_enum,
    validate_field_value,
)
from trackforge.domain.model.feedback import (
    Correction,
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
    ReasonCode,
)
from trackforge.domain.model.ids import (
    ClaimId,
    CorrectionId,
    DecisionId,
    FeedbackEventId,
    ItemId,
    ReviewSessionId,
)

EVENT_SCHEMA_VERSION = 1
EVENT_FIELDS = (
    "schema_version",
    "id",
    "kind",
    "item_id",
    "field",
    "system_value",
    "user_value",
    "scope",
    "reason",
    "reason_code",
    "reason_text",
    "context_snapshot",
    "created_at",
    "system_decision_id",
    "user_claim_id",
    "review_session_id",
    "applied",
)
CORRECTION_FIELDS = (
    "schema_version",
    "id",
    "item_id",
    "field",
    "system_decision_id",
    "system_value",
    "user_value",
    "user_claim_id",
    "scope",
    "reason_code",
    "reason_text",
    "context_snapshot",
    "created_at",
    "applied",
    "review_session_id",
)


def _iso_z(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidCatalogData(
            f"feedback {field} must be ISO 8601 text",
            context={"field": field, "value": value},
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidCatalogData(
            f"feedback {field} is not ISO 8601",
            context={"field": field, "value": value},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidCatalogData("feedback text field must be a string or null")
    return value


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidCatalogData("feedback id field must be a string or null")
    return value


def _reason_value(value: ReasonCode | str) -> str:
    return value.value if isinstance(value, ReasonCode) else str(value)


def _json_value(value: object) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise InvalidCatalogData(
            "feedback value is not JSON-serializable",
            context={"error": str(exc)},
        ) from exc


def event_document(event: FeedbackEvent, *, applied: bool = True) -> dict[str, Any]:
    """Return the canonical JSONL document for one event."""

    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "id": str(event.id),
        "kind": event.kind.value,
        "item_id": str(event.item_id),
        "field": event.field,
        "system_value": event.system_value,
        "user_value": event.user_value,
        "scope": event.scope.value if event.scope is not None else None,
        "reason": event.reason,
        "reason_code": event.reason_code.value if event.reason_code is not None else None,
        "reason_text": event.reason,
        "context_snapshot": (
            redact_secrets(event.context_snapshot) if event.context_snapshot is not None else None
        ),
        "created_at": _iso_z(event.created_at),
        "system_decision_id": (
            str(event.system_decision_id) if event.system_decision_id is not None else None
        ),
        "user_claim_id": str(event.user_claim_id) if event.user_claim_id is not None else None,
        "review_session_id": (
            str(event.review_session_id) if event.review_session_id is not None else None
        ),
        "applied": bool(applied),
    }


def correction_document(correction: Correction) -> dict[str, Any]:
    """Return the canonical on-disk document for one correction."""

    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "id": str(correction.id),
        "item_id": str(correction.item_id),
        "field": correction.field,
        "system_decision_id": str(correction.system_decision_id),
        "system_value": correction.system_value,
        "user_value": correction.user_value,
        "user_claim_id": (
            str(correction.user_claim_id) if correction.user_claim_id is not None else None
        ),
        "scope": correction.scope.value,
        "reason_code": _reason_value(correction.reason_code),
        "reason_text": correction.reason_text,
        "context_snapshot": redact_secrets(correction.context_snapshot),
        "created_at": _iso_z(correction.created_at),
        "applied": bool(correction.applied),
        "review_session_id": (
            str(correction.review_session_id) if correction.review_session_id is not None else None
        ),
    }


def event_from_document(document: Mapping[str, Any]) -> FeedbackEvent:
    """Rebuild a domain event from a JSONL document."""

    try:
        kind = FeedbackKind(document["kind"])
    except (KeyError, ValueError) as exc:
        raise InvalidCatalogData(
            "feedback event has an invalid kind",
            context={"kind": document.get("kind")},
        ) from exc
    reason_code = document.get("reason_code")
    context = document.get("context_snapshot")
    if context is not None and not isinstance(context, Mapping):
        raise InvalidCatalogData("feedback context_snapshot must be a JSON object")
    return FeedbackEvent(
        id=FeedbackEventId(str(document["id"])),
        kind=kind,
        item_id=ItemId(str(document["item_id"])),
        field=_optional_text(document.get("field")),
        system_value=validate_field_value(document.get("system_value"), "system_value"),
        user_value=validate_field_value(document.get("user_value"), "user_value"),
        scope=(
            coerce_enum(document.get("scope"), CorrectionScope, "scope")
            if document.get("scope") is not None
            else None
        ),
        reason=_optional_text(document.get("reason_text") or document.get("reason")),
        reason_code=(
            coerce_enum(reason_code, ReasonCode, "reason_code") if reason_code is not None else None
        ),
        context_snapshot=dict(context) if isinstance(context, Mapping) else None,
        created_at=_parse_time(document.get("created_at"), field="created_at"),
        system_decision_id=(
            DecisionId(str(document["system_decision_id"]))
            if document.get("system_decision_id") is not None
            else None
        ),
        user_claim_id=(
            ClaimId(str(document["user_claim_id"]))
            if document.get("user_claim_id") is not None
            else None
        ),
        review_session_id=(
            ReviewSessionId(str(document["review_session_id"]))
            if document.get("review_session_id") is not None
            else None
        ),
    )


def correction_from_document(document: Mapping[str, Any]) -> Correction:
    """Rebuild a domain correction from a correction snapshot file."""

    context = document.get("context_snapshot")
    if not isinstance(context, Mapping):
        raise InvalidCatalogData("correction context_snapshot must be a JSON object")
    reason_code = document.get("reason_code", "")
    return Correction(
        id=CorrectionId(str(document["id"])),
        item_id=ItemId(str(document["item_id"])),
        field=str(document["field"]),
        system_decision_id=DecisionId(str(document["system_decision_id"])),
        system_value=validate_field_value(document.get("system_value"), "system_value"),
        user_value=validate_field_value(document.get("user_value"), "user_value"),
        user_claim_id=(
            ClaimId(str(document["user_claim_id"]))
            if document.get("user_claim_id") is not None
            else None
        ),
        scope=CorrectionScope(str(document.get("scope", CorrectionScope.ITEM.value))),
        reason_code=(
            coerce_enum(reason_code, ReasonCode, "reason_code")
            if reason_code not in (None, "")
            else ReasonCode.OTHER
        ),
        reason_text=_optional_text(document.get("reason_text")),
        context_snapshot=dict(context),
        created_at=_parse_time(document.get("created_at"), field="created_at"),
        applied=bool(document.get("applied", False)),
        review_session_id=(
            ReviewSessionId(str(document["review_session_id"]))
            if document.get("review_session_id") is not None
            else None
        ),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    documents: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    document = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise FeedbackStorageError(
                        "feedback JSONL contains invalid JSON",
                        context={"path": str(path), "line": line_number},
                    ) from exc
                if not isinstance(document, dict):
                    raise FeedbackStorageError(
                        "feedback JSONL line must be a JSON object",
                        context={"path": str(path), "line": line_number},
                    )
                documents.append(document)
    except FeedbackStorageError:
        raise
    except OSError as exc:
        raise FeedbackStorageError(
            "cannot read feedback JSONL",
            context={"path": str(path), "error": str(exc)},
        ) from exc
    return documents


def _match_filters(
    document: Mapping[str, Any],
    *,
    item_id: ItemId | None,
    field: str | None,
    since: datetime | None,
) -> bool:
    if item_id is not None and str(document.get("item_id")) != str(item_id):
        return False
    if field is not None and document.get("field") != field:
        return False
    if since is not None:
        created = document.get("created_at")
        if not isinstance(created, str):
            return False
        if _parse_time(created, field="created_at") < since:
            return False
    return True


class JsonlFeedbackJournal:
    """Concrete append-only feedback journal rooted at ``data_dir/feedback``."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        data_dir: Path | str | None = None,
        fsync: bool = True,
    ) -> None:
        if root is None:
            root = data_dir
        if root is None:
            raise FeedbackStorageError("feedback journal requires a root directory")
        path = Path(root).expanduser()
        self._feedback_dir = path if path.name == "feedback" else path / "feedback"
        self._events_path = self._feedback_dir / "events.jsonl"
        self._corrections_dir = self._feedback_dir / "corrections"
        self._fsync = fsync

    @property
    def feedback_dir(self) -> Path:
        return self._feedback_dir

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def corrections_dir(self) -> Path:
        return self._corrections_dir

    def ensure_ready(self) -> None:
        """Create private feedback directories without touching existing data."""

        try:
            self._feedback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._corrections_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._feedback_dir, 0o700)
            os.chmod(self._corrections_dir, 0o700)
        except OSError as exc:
            raise FeedbackStorageError(
                "cannot create feedback directories",
                context={"path": str(self._feedback_dir), "error": str(exc)},
            ) from exc

    # ------------------------------------------------------------------
    # Synchronous core (also convenient for unit tests)
    # ------------------------------------------------------------------
    def append_event_sync(
        self,
        event: FeedbackEvent,
        *,
        correction: Correction | None = None,
        applied: bool = True,
    ) -> None:
        self.ensure_ready()
        if correction is not None:
            self.write_correction_sync(correction)
        document = event_document(event, applied=applied)
        payload = canonical_json(document) + b"\n"
        try:
            descriptor = os.open(
                self._events_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as exc:
            raise FeedbackStorageError(
                "cannot open feedback JSONL for append",
                context={"path": str(self._events_path), "error": str(exc)},
            ) from exc
        try:
            os.chmod(self._events_path, 0o600)
            _write_all(descriptor, payload)
            if self._fsync:
                os.fsync(descriptor)
        except OSError as exc:
            raise FeedbackStorageError(
                "cannot append to feedback JSONL; the event was not recorded",
                context={
                    "path": str(self._events_path),
                    "event_id": str(event.id),
                    "error": str(exc),
                },
            ) from exc
        finally:
            os.close(descriptor)

    def append(
        self,
        event: FeedbackEvent,
        *,
        correction: Correction | None = None,
        applied: bool = True,
    ) -> None:
        """Synchronous convenience alias used by simple scripts."""

        self.append_event_sync(event, correction=correction, applied=applied)

    def record(
        self,
        event: FeedbackEvent,
        *,
        correction: Correction | None = None,
        applied: bool = True,
    ) -> None:
        """Synchronous convenience alias with review-oriented naming."""

        self.append_event_sync(event, correction=correction, applied=applied)

    def write_correction_sync(self, correction: Correction) -> None:
        self.ensure_ready()
        target = self._corrections_dir / f"{correction.id}.json"
        if target.exists():
            raise DuplicateCorrection(
                "correction id already exists in the feedback journal",
                user_action="Use a new correction id; feedback history is append-only.",
                context={"correction_id": str(correction.id), "path": str(target)},
            )
        payload = canonical_json(correction_document(correction)) + b"\n"
        temp = self._corrections_dir / f".{correction.id}.tmp"
        try:
            descriptor = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                _write_all(descriptor, payload)
                if self._fsync:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temp, target)
            if self._fsync:
                _fsync_directory(self._corrections_dir)
        except DuplicateCorrection:
            raise
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise FeedbackStorageError(
                "cannot write correction snapshot",
                context={"path": str(target), "error": str(exc)},
            ) from exc

    def read_event_documents_sync(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return [
            document
            for document in _read_jsonl(self._events_path)
            if _match_filters(document, item_id=item_id, field=field, since=since)
        ]

    def read_events_sync(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[FeedbackEvent]:
        return [
            event_from_document(document)
            for document in self.read_event_documents_sync(
                item_id=item_id,
                field=field,
                since=since,
            )
        ]

    def get_correction_sync(self, correction_id: CorrectionId | str) -> Correction | None:
        target = self._corrections_dir / f"{correction_id}.json"
        if not target.exists():
            return None
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeedbackStorageError(
                "cannot read correction snapshot",
                context={"path": str(target), "error": str(exc)},
            ) from exc
        if not isinstance(document, dict):
            raise FeedbackStorageError(
                "correction snapshot must be a JSON object",
                context={"path": str(target)},
            )
        return correction_from_document(document)

    def list_corrections_sync(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
    ) -> list[Correction]:
        if not self._corrections_dir.exists():
            return []
        corrections: list[Correction] = []
        for path in sorted(self._corrections_dir.glob("*.json")):
            correction = self.get_correction_sync(path.stem)
            if correction is None:
                continue
            if item_id is not None and correction.item_id != item_id:
                continue
            if field is not None and correction.field != field:
                continue
            corrections.append(correction)
        return corrections

    def consistency_sync(self, events: Sequence[FeedbackEvent]) -> FeedbackConsistency:
        documents = _read_jsonl(self._events_path)
        jsonl_ids = [str(document.get("id")) for document in documents]
        duplicates = tuple(
            sorted({event_id for event_id in jsonl_ids if jsonl_ids.count(event_id) > 1})
        )
        jsonl_set = set(jsonl_ids)
        db_set = {str(event.id) for event in events}
        warnings: list[str] = []
        missing_in_jsonl = tuple(sorted(db_set - jsonl_set))
        missing_in_db = tuple(sorted(jsonl_set - db_set))
        if missing_in_jsonl:
            warnings.append(f"{len(missing_in_jsonl)} feedback event(s) exist only in SQLite")
        if missing_in_db:
            warnings.append(f"{len(missing_in_db)} feedback event(s) exist only in JSONL")
        if duplicates:
            warnings.append(f"{len(duplicates)} duplicate feedback id(s) in JSONL")
        return FeedbackConsistency(
            jsonl_events=len(jsonl_ids),
            db_events=len(events),
            missing_in_jsonl=missing_in_jsonl,
            missing_in_db=missing_in_db,
            duplicate_jsonl_ids=duplicates,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------
    # Async wrappers used by application services
    # ------------------------------------------------------------------
    async def append_event(
        self,
        event: FeedbackEvent,
        *,
        correction: Correction | None = None,
        applied: bool = True,
    ) -> None:
        await asyncio.to_thread(
            self.append_event_sync,
            event,
            correction=correction,
            applied=applied,
        )

    async def write_correction(self, correction: Correction) -> None:
        await asyncio.to_thread(self.write_correction_sync, correction)

    async def append_correction(self, correction: Correction) -> None:
        await self.write_correction(correction)

    async def save_correction(self, correction: Correction) -> None:
        await self.write_correction(correction)

    async def read_event_documents(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.read_event_documents_sync,
            item_id=item_id,
            field=field,
            since=since,
        )

    async def read_events(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[FeedbackEvent]:
        return await asyncio.to_thread(
            self.read_events_sync,
            item_id=item_id,
            field=field,
            since=since,
        )

    async def get_correction(self, correction_id: CorrectionId | str) -> Correction | None:
        return await asyncio.to_thread(self.get_correction_sync, correction_id)

    async def list_corrections(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
    ) -> list[Correction]:
        return await asyncio.to_thread(
            self.list_corrections_sync,
            item_id=item_id,
            field=field,
        )

    async def consistency(self, events: list[FeedbackEvent]) -> FeedbackConsistency:
        return await asyncio.to_thread(self.consistency_sync, events)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - defensive
            raise OSError("short write")
        view = view[written:]


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


# Compatibility aliases used by callers/tests that prefer descriptive names.
JsonlFeedbackStore = JsonlFeedbackJournal
FeedbackJsonlStore = JsonlFeedbackJournal
JsonlFeedbackWriter = JsonlFeedbackJournal
FeedbackJsonlWriter = JsonlFeedbackJournal


__all__ = [
    "CORRECTION_FIELDS",
    "EVENT_FIELDS",
    "EVENT_SCHEMA_VERSION",
    "FeedbackJsonlStore",
    "FeedbackJsonlWriter",
    "JsonlFeedbackJournal",
    "JsonlFeedbackStore",
    "JsonlFeedbackWriter",
    "correction_document",
    "correction_from_document",
    "event_document",
    "event_from_document",
]
