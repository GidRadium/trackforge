"""Process a collection sequentially, persisting per-item resume state.

One item is acquired/observed/resolved/rendered/published at a time.  A
failure marks only that item failed; the queue continues unless ``fail_fast``
is set.  Processed items are never downloaded or rendered again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

from trackforge.application.dto.collections import (
    CollectionItemContext,
    CollectionPlanView,
    CollectionRunItem,
    CollectionRunResult,
    CollectionRunStatus,
)
from trackforge.application.dto.progress import (
    STAGE_INDEX,
    CollectionProgressStatus,
    HandlerReport,
    ItemProgress,
    ProgressContext,
    ProgressStatus,
)
from trackforge.application.dto.results import RunOptions, RunResult
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.services.collection_cover import (
    CollectionCoverService,
    cover_context_claim,
)
from trackforge.application.services.collection_queue import CollectionQueue
from trackforge.application.services.progress import ProgressBoard, ProgressTracker
from trackforge.application.use_cases.process_item import ProcessItem, _candidate_label
from trackforge.domain.errors import DomainValidationError, RecordNotFound, TrackForgeError
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.collection import (
    Collection,
    CollectionItem,
    CollectionItemStatus,
)
from trackforge.domain.model.ids import CollectionId, ItemId


class ProcessCollection:
    """Sequential collection runner with pause/cancel and resume support."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        process_item: ProcessItem,
        *,
        collection_cover: CollectionCoverService | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        progress: ProgressBoard | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._process_item = process_item
        self._collection_cover = collection_cover
        self._sleep = sleep or asyncio.sleep
        self._queue: CollectionQueue | None = None
        self._progress = progress if progress is not None else ProgressBoard()

    def pause(self) -> None:
        if self._queue is not None:
            self._queue.pause()

    def resume_queue(self) -> None:
        if self._queue is not None:
            self._queue.resume()

    def cancel(self) -> None:
        if self._queue is not None:
            self._queue.cancel()

    async def process_plan(
        self,
        plan: CollectionPlanView,
        options: RunOptions | None = None,
        *,
        resume: bool = False,
        fail_fast: bool = False,
        min_sleep: float = 0.0,
        skip_positions: Sequence[int] = (),
    ) -> CollectionRunResult:
        """Persist/reconcile the plan and process its selected items."""

        opts = options or RunOptions()
        collection = await self._persist_plan(plan, resume=resume)
        if skip_positions:
            collection = await self._mark_skipped(collection, skip_positions)
        selected_positions = {item.position for item in plan.selected_items}
        selected = [item for item in collection.items if item.position in selected_positions]
        result = await self._run_selected(
            collection,
            selected,
            opts,
            fail_fast=fail_fast,
            min_sleep=min_sleep,
        )
        skipped_items = tuple(
            CollectionRunItem(
                position=item.position,
                status=CollectionItemStatus.SKIPPED,
                item_id=item.item_id,
                error=item.error,
            )
            for item in collection.items
            if item.position in set(skip_positions) and item.status is CollectionItemStatus.SKIPPED
        )
        items = tuple(sorted((*result.items, *skipped_items), key=lambda value: value.position))
        return replace(
            result,
            items=items,
            skipped=result.skipped + len(skipped_items),
            warnings=tuple(dict.fromkeys((*plan.warnings, *result.warnings))),
        )

    async def resume(
        self,
        collection_id: CollectionId,
        options: RunOptions | None = None,
        *,
        fail_fast: bool = False,
        min_sleep: float = 0.0,
    ) -> CollectionRunResult:
        """Continue a persisted collection from planned/acquired items."""

        opts = options or RunOptions()
        async with self._uow_factory() as uow:
            collection = await uow.collections.get(collection_id)
        if collection is None:
            raise RecordNotFound(
                "collection not found",
                user_action="Run `trackforge collection list` to find the collection id.",
                context={"collection_id": collection_id},
            )
        selected = [
            item
            for item in collection.items
            if item.status
            in {
                CollectionItemStatus.PLANNED,
                CollectionItemStatus.ACQUIRED,
                CollectionItemStatus.FAILED,
            }
        ]
        return await self._run_selected(
            collection,
            selected,
            opts,
            fail_fast=fail_fast,
            min_sleep=min_sleep,
            retry_failed=True,
        )

    async def _persist_plan(
        self,
        plan: CollectionPlanView,
        *,
        resume: bool,
    ) -> Collection:
        async with self._uow_factory() as uow:
            existing = await uow.collections.find_by_source(plan.collection.source_ref)
            if resume and existing is None:
                raise RecordNotFound(
                    "no collection to resume for this source",
                    user_action="Run add without --resume first.",
                    context={"source_type": type(plan.collection.source_ref).__name__},
                )
            if existing is None:
                await uow.collections.save(plan.collection)
                saved = await uow.collections.get(plan.collection.id)
            else:
                merged = replace(
                    plan.collection,
                    id=existing.id,
                    created_at=existing.created_at,
                    tag_basis=(existing.tag_basis if resume else plan.collection.tag_basis),
                )
                await uow.collections.save(merged)
                saved = await uow.collections.get(existing.id)
        if saved is None:  # pragma: no cover - the save just committed
            raise RecordNotFound("collection was not persisted")
        return saved

    async def _mark_skipped(
        self,
        collection: Collection,
        positions: Sequence[int],
    ) -> Collection:
        wanted = {int(position) for position in positions}
        for item in collection.items:
            if item.position not in wanted:
                continue
            if item.status in {
                CollectionItemStatus.PROCESSED,
                CollectionItemStatus.ACQUIRED,
                CollectionItemStatus.SKIPPED,
            }:
                continue
            async with self._uow_factory() as uow:
                await uow.collections.update_item(
                    collection.id,
                    item.position,
                    status=CollectionItemStatus.SKIPPED,
                    item_id=item.item_id,
                    error=None,
                )
        async with self._uow_factory() as uow:
            updated = await uow.collections.get(collection.id)
        return updated if updated is not None else collection

    async def _run_selected(
        self,
        collection: Collection,
        selected: Sequence[CollectionItem],
        options: RunOptions,
        *,
        fail_fast: bool,
        min_sleep: float,
        retry_failed: bool = False,
    ) -> CollectionRunResult:
        results: list[CollectionRunItem] = []
        pending: list[CollectionItem] = []
        warnings = list(collection.warnings)
        errors: list[str] = []
        attempted_successes = 0
        attempted_failures = 0
        skipped = 0
        handler_reports: list[HandlerReport] = []

        ordered = sorted(selected, key=lambda value: value.position)
        total = len(ordered)
        contexts: dict[int, ProgressContext] = {}
        queued: list[ItemProgress] = []
        for index, collection_item in enumerate(ordered, start=1):
            context = ProgressContext(
                scope_id=str(collection.id),
                collection_id=str(collection.id),
                item_id=(
                    str(collection_item.item_id) if collection_item.item_id is not None else None
                ),
                position=index,
                total=max(1, total),
                label=_candidate_label(collection_item.candidate, collection_item.position),
                stage="plan",
            )
            contexts[collection_item.position] = context
            queued.append(context.queued())
        self._progress.start_collection(str(collection.id), queued)

        collection_claims = collection.context_claims
        if self._collection_cover is not None and selected:
            cover_hash = await self._collection_cover.resolve(collection)
            warnings.extend(self._collection_cover.warnings)
            if cover_hash is not None:
                collection_claims = (*collection_claims, cover_context_claim(cover_hash))

        force_reprocess = (
            bool(
                set(options.force_steps) & {"observe", "interpret", "resolve", "render", "publish"}
            )
            or options.refresh
        )
        for item in ordered:
            context = contexts[item.position]
            if item.status is CollectionItemStatus.PROCESSED and not force_reprocess:
                skipped += 1
                output_path = await self._published_path(item.item_id)
                context = replace(
                    context,
                    label=await self._final_label(item.item_id, context.label),
                )
                self._progress.update(
                    _board_item(
                        context,
                        status=ProgressStatus.DONE,
                        output_path=output_path,
                        warnings=("already_processed",),
                    )
                )
                results.append(
                    CollectionRunItem(
                        position=item.position,
                        status=CollectionItemStatus.PROCESSED,
                        item_id=item.item_id,
                        output_path=output_path,
                        warnings=("already_processed",),
                        cached=True,
                    )
                )
                continue
            if item.status is CollectionItemStatus.SKIPPED:
                skipped += 1
                self._progress.update(
                    _board_item(
                        context,
                        status=ProgressStatus.SKIPPED,
                        message=item.error,
                    )
                )
                results.append(
                    CollectionRunItem(
                        position=item.position,
                        status=CollectionItemStatus.SKIPPED,
                        item_id=item.item_id,
                        error=item.error,
                    )
                )
                continue
            if item.status is CollectionItemStatus.FAILED:
                if retry_failed:
                    if item.item_id is not None and await self._artifact_ready(item.item_id):
                        pending.append(
                            replace(item, status=CollectionItemStatus.ACQUIRED, error=None)
                        )
                    else:
                        await self._reset_for_retry(collection.id, item)
                        pending.append(
                            replace(
                                item,
                                status=CollectionItemStatus.PLANNED,
                                item_id=None,
                                error=None,
                            )
                        )
                    continue
                attempted_failures += 1
                self._progress.update(
                    _board_item(
                        context,
                        status=ProgressStatus.FAILED,
                        message=item.error or "previous attempt failed",
                    )
                )
                results.append(
                    CollectionRunItem(
                        position=item.position,
                        status=CollectionItemStatus.FAILED,
                        item_id=item.item_id,
                        error=item.error or "previous attempt failed",
                    )
                )
                if item.error:
                    errors.append(f"position {item.position}: {item.error}")
                if fail_fast:
                    break
                continue
            if item.status is CollectionItemStatus.ACQUIRED:
                if await self._artifact_ready(item.item_id):
                    pending.append(item)
                else:
                    await self._reset_for_retry(collection.id, item)
                    pending.append(
                        replace(
                            item,
                            status=CollectionItemStatus.PLANNED,
                            item_id=None,
                            error=None,
                        )
                    )
                continue
            pending.append(item)

        queue = CollectionQueue(pending, min_sleep=min_sleep, sleep=self._sleep)
        self._queue = queue
        try:
            async for item in queue:
                item_context = CollectionItemContext.from_collection(collection, item)
                if collection_claims is not collection.context_claims:
                    item_context = replace(
                        item_context,
                        context_claims=collection_claims,
                    )
                progress_context = contexts[item.position]
                acquired_id: ItemId | None = item.item_id
                tracker = ProgressTracker(self._progress, progress_context)
                if acquired_id is not None:
                    tracker.bind(item_id=acquired_id)
                tracker.stage("plan", message="collection item")

                async def on_acquired(
                    new_item_id: ItemId,
                    *,
                    position: int = item.position,
                    tracker: ProgressTracker = tracker,
                ) -> None:
                    nonlocal acquired_id
                    acquired_id = new_item_id
                    tracker.bind(item_id=new_item_id)
                    async with self._uow_factory() as uow:
                        await uow.collections.update_item(
                            collection.id,
                            position,
                            status=CollectionItemStatus.ACQUIRED,
                            item_id=new_item_id,
                            error=None,
                        )

                try:
                    run_result: RunResult
                    final_item_id: ItemId | None
                    if (
                        item.status
                        in {CollectionItemStatus.ACQUIRED, CollectionItemStatus.PROCESSED}
                        and item.item_id is not None
                    ):
                        run_result = await self._process_item.run_item(
                            item.item_id,
                            options,
                            collection=item_context,
                            tracker=tracker,
                        )
                        final_item_id = item.item_id
                    else:
                        candidate_result = await self._process_item.run_candidate(
                            item.candidate,
                            options,
                            collection=item_context,
                            on_acquired=on_acquired,
                            tracker=tracker,
                        )
                        if not isinstance(candidate_result, RunResult):
                            raise DomainValidationError(
                                "collection item returned a plan instead of a run result",
                                context={"position": item.position},
                            )
                        run_result = candidate_result
                        final_item_id = acquired_id
                        if final_item_id is None and run_result.items:
                            final_item_id = run_result.items[0].item_id
                    if final_item_id is None:
                        raise DomainValidationError(
                            "collection item produced no item id",
                            context={"position": item.position},
                        )
                    async with self._uow_factory() as uow:
                        await uow.collections.update_item(
                            collection.id,
                            item.position,
                            status=CollectionItemStatus.PROCESSED,
                            item_id=final_item_id,
                            error=None,
                        )
                except Exception as exc:
                    message = exc.message if isinstance(exc, TrackForgeError) else str(exc)
                    message = message or type(exc).__name__
                    tracker.failed(message)
                    self._append_board_reports(
                        handler_reports,
                        str(collection.id),
                        progress_context.position,
                    )
                    attempted_failures += 1
                    errors.append(f"position {item.position}: {message}")
                    async with self._uow_factory() as uow:
                        await uow.collections.update_item(
                            collection.id,
                            item.position,
                            status=CollectionItemStatus.FAILED,
                            item_id=acquired_id,
                            error=message,
                        )
                    results.append(
                        CollectionRunItem(
                            position=item.position,
                            status=CollectionItemStatus.FAILED,
                            item_id=acquired_id,
                            error=message,
                        )
                    )
                    if fail_fast:
                        break
                    continue

                attempted_successes += 1
                warnings.extend(run_result.warnings)
                handler_reports.extend(run_result.handler_reports)
                output = run_result.output_paths[0] if run_result.output_paths else None
                tracker.done(output)
                results.append(
                    CollectionRunItem(
                        position=item.position,
                        status=CollectionItemStatus.PROCESSED,
                        item_id=final_item_id,
                        output_path=output,
                        warnings=tuple(run_result.warnings),
                        cached=run_result.cache_hit,
                    )
                )
        except asyncio.CancelledError:
            finished_positions = {result.position for result in results}
            for pending_item in pending:
                if pending_item.position not in finished_positions:
                    self._progress.update(
                        _board_item(
                            contexts[pending_item.position],
                            status=ProgressStatus.SKIPPED,
                            message="cancelled",
                        )
                    )
            self._progress.mark_collection(
                str(collection.id),
                CollectionProgressStatus.CANCELLED,
            )
            raise
        finally:
            self._queue = None

        if queue.cancelled:
            finished_positions = {result.position for result in results}
            for item in pending:
                if item.position not in finished_positions:
                    self._progress.update(
                        _board_item(
                            contexts[item.position],
                            status=ProgressStatus.SKIPPED,
                            message="cancelled",
                        )
                    )
            self._progress.mark_collection(
                str(collection.id),
                CollectionProgressStatus.CANCELLED,
            )

        processed_items = tuple(sorted(results, key=lambda result: result.position))
        output_paths = tuple(
            dict.fromkeys(
                result.output_path for result in processed_items if result.output_path is not None
            )
        )
        remaining = await self._remaining_count(collection.id)
        status = _collection_status(
            cancelled=queue.cancelled,
            failures=attempted_failures,
            successes=attempted_successes,
        )
        return CollectionRunResult(
            collection_id=collection.id,
            status=status,
            items=processed_items,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(dict.fromkeys(errors)),
            output_paths=output_paths,
            processed=attempted_successes,
            failed=attempted_failures,
            skipped=skipped,
            remaining=remaining,
            handler_reports=tuple(handler_reports),
        )

    def _append_board_reports(
        self,
        reports: list[HandlerReport],
        collection_id: str,
        position: int,
    ) -> None:
        seen = {(report.position, report.capability_id, report.stage) for report in reports}
        for report in self._progress.reports(collection_id):
            key = (report.position, report.capability_id, report.stage)
            if report.position == position and key not in seen:
                reports.append(report)
                seen.add(key)

    async def _final_label(self, item_id: ItemId | None, fallback: str) -> str:
        if item_id is None:
            return fallback
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None or item.decision_record_id is None:
                return fallback
            record = await uow.decisions.get_record(item.decision_record_id)
        if record is None:
            return fallback
        for decision in record.decisions:
            if decision.subject is Subject.ITEM and decision.field == "title":
                value = decision.value
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return fallback

    async def _artifact_ready(self, item_id: ItemId | None) -> bool:
        if item_id is None:
            return False
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None or item.media_id is None:
                return False
            media = await uow.media.get(item.media_id)
            return media is not None and Path(media.cas_path).is_file()

    async def _reset_for_retry(self, collection_id: CollectionId, item: CollectionItem) -> None:
        async with self._uow_factory() as uow:
            await uow.collections.update_item(
                collection_id,
                item.position,
                status=CollectionItemStatus.PLANNED,
                item_id=None,
                error=None,
            )

    async def _published_path(self, item_id: ItemId | None) -> str | None:
        if item_id is None:
            return None
        async with self._uow_factory() as uow:
            entries = await uow.library.list_entries(item_id=item_id)
        return entries[-1].path if entries else None

    async def _remaining_count(self, collection_id: CollectionId) -> int:
        async with self._uow_factory() as uow:
            collection = await uow.collections.get(collection_id)
        if collection is None:
            return 0
        return sum(
            1
            for item in collection.items
            if item.status in {CollectionItemStatus.PLANNED, CollectionItemStatus.ACQUIRED}
        )


def _board_item(
    context: ProgressContext,
    *,
    status: ProgressStatus,
    output_path: str | None = None,
    message: str | None = None,
    warnings: Sequence[str] = (),
) -> ItemProgress:
    return ItemProgress(
        scope_id=context.scope_id,
        collection_id=context.collection_id,
        item_id=context.item_id,
        position=context.position,
        total=context.total,
        label=context.label,
        stage=context.stage,
        stage_index=STAGE_INDEX[context.stage],
        status=status,
        output_path=output_path,
        message=message,
        warnings=tuple(warnings),
    )


def _collection_status(
    *,
    cancelled: bool,
    failures: int,
    successes: int,
) -> CollectionRunStatus:
    if cancelled:
        return CollectionRunStatus.CANCELLED
    if failures and successes:
        return CollectionRunStatus.PARTIAL
    if failures:
        return CollectionRunStatus.FAILED
    return CollectionRunStatus.COMPLETED


__all__ = ["ProcessCollection"]
