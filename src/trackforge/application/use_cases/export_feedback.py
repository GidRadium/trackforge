"""Export feedback events as append-only JSONL or stable CSV.

The JSONL journal is the primary export source.  SQLite is merged in as an
index so an event written before the journal existed is not silently lost;
divergence is reported as a warning and history is never deleted.  Secrets are
redacted again at export time and audio bytes are never included.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trackforge.application.dto.feedback import FeedbackExportResult
from trackforge.application.ports.feedback import FeedbackJournal, NullFeedbackJournal
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.services.canonical_json import canonical_json
from trackforge.application.services.context_snapshot import redact_secrets
from trackforge.domain.errors import DomainValidationError, FeedbackStorageError
from trackforge.domain.model.feedback import FeedbackEvent
from trackforge.domain.model.ids import ItemId
from trackforge.domain.policies.normalization import canonical_field

CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "item_id",
    "field",
    "system_value",
    "user_value",
    "scope",
    "reason_code",
    "reason_text",
    "review_session_id",
    "system_decision_id",
    "user_claim_id",
    "created_at",
    "applied",
    "context_snapshot",
)
_SUPPORTED_FORMATS = frozenset({"jsonl", "csv"})


class ExportFeedback:
    """Read the journal/index and write one redacted export file."""

    def __init__(
        self,
        journal: FeedbackJournal | None = None,
        unit_of_work_factory: Callable[[], UnitOfWork] | None = None,
        *,
        data_dir: Path | str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._journal = journal or NullFeedbackJournal()
        self._uow_factory = unit_of_work_factory
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._clock = clock

    async def export(
        self,
        *,
        format: str = "jsonl",
        output: Path | str | None = None,
        item_id: ItemId | str | None = None,
        field: str | None = None,
        since: datetime | str | None = None,
    ) -> FeedbackExportResult:
        """Write a redacted export and return its path/counts."""

        target_format = str(format).strip().lower()
        if target_format not in _SUPPORTED_FORMATS:
            raise DomainValidationError(
                "unsupported feedback export format",
                user_action="Use --format jsonl or --format csv.",
                context={"format": format},
            )
        target_item = ItemId(str(item_id)) if item_id is not None else None
        target_field = (
            canonical_field(field.strip()) if isinstance(field, str) and field.strip() else None
        )
        target_since = _parse_since(since)

        warnings: list[str] = []
        jsonl_documents = await self._journal.read_event_documents(
            item_id=target_item,
            field=target_field,
            since=target_since,
        )
        db_events = await self._db_events(
            item_id=target_item,
            field=target_field,
            since=target_since,
            warnings=warnings,
        )
        documents = self._merge(
            jsonl_documents,
            db_events,
            warnings=warnings,
        )
        if target_item is None and target_field is None and target_since is None:
            try:
                consistency = await self._journal.consistency(db_events)
            except Exception:  # pragma: no cover - best effort
                consistency = None
            if consistency is not None:
                warnings.extend(consistency.warnings)
        corrections = await self._journal.list_corrections(
            item_id=target_item,
            field=target_field,
        )
        if target_since is not None:
            corrections = [
                correction for correction in corrections if correction.created_at >= target_since
            ]

        target_path = self._target_path(target_format, output)
        target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        redacted = [redact_secrets(document) for document in documents]
        if target_format == "jsonl":
            payload = b"".join(canonical_json(document) + b"\n" for document in redacted)
        else:
            payload = _csv_bytes(redacted)
        await asyncio.to_thread(_atomic_write, target_path, payload)

        return FeedbackExportResult(
            format=target_format,
            output_path=target_path,
            event_count=len(redacted),
            correction_count=len(corrections),
            jsonl_events=len(jsonl_documents),
            db_events=len(db_events),
            warnings=tuple(dict.fromkeys(warnings)),
            generated_at=self._clock(),
        )

    async def _db_events(
        self,
        *,
        item_id: ItemId | None,
        field: str | None,
        since: datetime | None,
        warnings: list[str],
    ) -> list[FeedbackEvent]:
        if self._uow_factory is None:
            return []
        try:
            async with self._uow_factory() as uow:
                list_all = getattr(uow.feedback, "list_all", None)
                if list_all is not None:
                    result = await list_all(
                        item_id=item_id,
                        field=field,
                        since=since,
                    )
                    return cast(list[FeedbackEvent], result)
                if item_id is not None:
                    return await uow.feedback.list_by_item(item_id)
        except DomainValidationError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            warnings.append(f"SQLite feedback index could not be read: {exc}")
        return []

    def _merge(
        self,
        jsonl_documents: Sequence[Mapping[str, Any]],
        db_events: Sequence[FeedbackEvent],
        *,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for document in jsonl_documents:
            event_id = str(document.get("id"))
            if event_id not in merged:
                order.append(event_id)
            merged[event_id] = dict(document)
        jsonl_ids = set(merged)
        db_ids: set[str] = set()
        for event in db_events:
            event_id = str(event.id)
            db_ids.add(event_id)
            if event_id in merged:
                continue
            order.append(event_id)
            merged[event_id] = _event_document(event)
        missing_in_jsonl = db_ids - jsonl_ids
        missing_in_db = jsonl_ids - db_ids
        if missing_in_jsonl:
            warnings.append(
                f"{len(missing_in_jsonl)} feedback event(s) exist only in SQLite; "
                "exported from the index"
            )
        if missing_in_db:
            warnings.append(
                f"{len(missing_in_db)} feedback event(s) exist only in JSONL; "
                "exported from the append-only journal"
            )
        result: list[dict[str, Any]] = []
        for event_id in order:
            result.append(dict(merged[event_id]))
        return result

    def _target_path(self, target_format: str, output: Path | str | None) -> Path:
        if output is not None:
            target = Path(output).expanduser()
            if target.exists() and target.is_dir():
                target = target / self._default_name(target_format)
        else:
            base = self._data_dir
            if base is None:
                events_path = getattr(self._journal, "events_path", None)
                if events_path is not None:
                    base = Path(events_path).parent.parent
            if base is None:
                base = Path.cwd()
            target = base / "feedback" / "exports" / self._default_name(target_format)
        self._guard_target(target)
        return target

    def _guard_target(self, target: Path) -> None:
        """Never let an export overwrite the append-only journal or snapshots."""

        events_path = getattr(self._journal, "events_path", None)
        if events_path is not None and target.resolve() == Path(events_path).resolve():
            raise DomainValidationError(
                "refusing to overwrite the append-only feedback journal",
                user_action="Choose a different --output path.",
                context={"path": str(target)},
            )
        corrections_dir = getattr(self._journal, "corrections_dir", None)
        if corrections_dir is not None:
            try:
                target.resolve().relative_to(Path(corrections_dir).resolve())
            except ValueError:
                pass
            else:
                raise DomainValidationError(
                    "refusing to write an export inside the correction snapshots directory",
                    user_action="Choose a different --output path.",
                    context={"path": str(target)},
                )

    def _default_name(self, target_format: str) -> str:
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"feedback-{stamp}.{target_format}"

    async def run(self, **kwargs: Any) -> FeedbackExportResult:
        """Alias for callers that name use-case entry points ``run``."""

        return await self.export(**kwargs)


def _event_document(event: FeedbackEvent) -> dict[str, Any]:
    created = event.created_at
    created = created if created.tzinfo is not None else created.replace(tzinfo=UTC)
    return {
        "schema_version": 1,
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
        "context_snapshot": event.context_snapshot,
        "created_at": created.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "system_decision_id": (
            str(event.system_decision_id) if event.system_decision_id is not None else None
        ),
        "user_claim_id": str(event.user_claim_id) if event.user_claim_id is not None else None,
        "review_session_id": (
            str(event.review_session_id) if event.review_session_id is not None else None
        ),
        "applied": True,
    }


def _parse_since(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainValidationError(
            "since must be an ISO date or timestamp",
            user_action="Use YYYY-MM-DD or an ISO timestamp.",
            context={"since": text},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return canonical_json(value).decode("utf-8")


def _csv_bytes(documents: Sequence[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for document in documents:
        writer.writerow({column: _cell(document.get(column)) for column in CSV_COLUMNS})
    return buffer.getvalue().encode("utf-8")


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = target.parent / f".{target.name}.tmp"
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise FeedbackStorageError(
            "cannot write feedback export",
            context={"path": str(target), "error": str(exc)},
        ) from exc


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


# Convenience aliases for callers that prefer a verb or a shorter name.
ExportFeedbackUseCase = ExportFeedback
export_feedback = ExportFeedback


__all__ = [
    "CSV_COLUMNS",
    "ExportFeedback",
    "ExportFeedbackUseCase",
    "export_feedback",
]
