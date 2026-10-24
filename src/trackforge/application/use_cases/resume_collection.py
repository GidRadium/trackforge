"""Resume a persisted collection without re-acquiring processed items."""

from __future__ import annotations

from trackforge.application.dto.collections import CollectionRunResult
from trackforge.application.dto.results import RunOptions
from trackforge.application.use_cases.process_collection import ProcessCollection
from trackforge.domain.model.ids import CollectionId


class ResumeCollection:
    """Stable resume-facing use case over :class:`ProcessCollection`."""

    def __init__(self, process_collection: ProcessCollection) -> None:
        self._process_collection = process_collection

    async def resume(
        self,
        collection_id: CollectionId | str,
        options: RunOptions | None = None,
        *,
        fail_fast: bool = False,
        min_sleep: float = 0.0,
    ) -> CollectionRunResult:
        return await self._process_collection.resume(
            CollectionId(str(collection_id)),
            options,
            fail_fast=fail_fast,
            min_sleep=min_sleep,
        )


__all__ = ["ResumeCollection"]
