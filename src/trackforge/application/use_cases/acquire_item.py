"""Select an acquirer and persist one candidate as an acquired item."""

from __future__ import annotations

from collections.abc import Sequence

from trackforge.application.dto.plan import AcquireOptions, ItemView
from trackforge.application.ports.acquisition import Acquirer, IntakeResult
from trackforge.domain.errors import DomainValidationError, UnsupportedSource
from trackforge.domain.model.ids import ItemId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import Candidate


class AcquireItem:
    """Turn a chosen candidate into CAS bytes plus catalog provenance."""

    async def acquire(
        self,
        candidate: Candidate,
        options: AcquireOptions | None = None,
        acquirers: Sequence[Acquirer] | None = None,
    ) -> ItemView:
        """Acquire ``candidate`` with the first supporting acquirer."""

        if not isinstance(candidate, Candidate):
            raise DomainValidationError("candidate must be a Candidate")
        selected = next(
            (acquirer for acquirer in tuple(acquirers or ()) if acquirer.supports(candidate)),
            None,
        )
        if selected is None:
            raise UnsupportedSource(
                "no acquirer supports the selected candidate",
                user_action="Choose a candidate from a supported source.",
                context={"source_type": type(candidate.source_ref).__name__},
            )
        result = await selected.acquire(candidate, options)
        return item_view(result)


def item_view(result: IntakeResult) -> ItemView:
    """Map an acquisition result to the stable application view."""

    return ItemView(
        item_id=ItemId(result.item_id),
        status=ItemStatus.ACQUIRED,
        media_id=result.media.id,
        intake_id=result.intake.id,
        file_instance_id=result.file_instance.id,
        content_hash=result.media.content_hash,
        size_bytes=result.media.size_bytes,
        cas_path=result.media.cas_path,
        source_path=result.file_instance.path,
        reused_media=result.reused_media,
    )


__all__ = ["AcquireItem", "item_view"]
