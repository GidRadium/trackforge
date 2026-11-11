"""Build a deterministic download plan from explicit user selections.

The planner is deliberately offline: it only inspects the ``SourceInfo`` that
was already resolved by ``inspect``.  It never talks to a source and never
writes a file.  The resulting :class:`DownloadPlan` is the single contract the
yt-dlp adapter needs in order to download one or more selected variants into a
job's staging directory.
"""

# pyright: standard

from __future__ import annotations

import re
from typing import Final

from .errors import VariantUnavailable
from .models import (
    DownloadPlan,
    DownloadRequest,
    DownloadVariant,
    ItemPlan,
    ItemSelection,
    MediaItem,
    MediaKind,
    ResolutionState,
    StreamInfo,
    StreamRole,
)

_MAX_STEM_LENGTH: Final = 120
_RESERVED_WINDOWS_NAMES: Final = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_CONTROL_OR_RESERVED = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")


def safe_stem(value: str, *, fallback: str = "media") -> str:
    """Return a single, filesystem-safe stem.

    Path separators are replaced rather than interpreted, so a source title
    (or a ``staging_name`` override) can never escape the job directory.
    Unicode is preserved because the final naming policy belongs to ingest.
    """
    text = _CONTROL_OR_RESERVED.sub("_", str(value))
    text = _WHITESPACE.sub(" ", text).strip().strip(" .")
    if not text or text in {".", ".."}:
        text = fallback
    if text.split(".", 1)[0].strip().casefold() in _RESERVED_WINDOWS_NAMES:
        text = f"_{text}"
    if len(text) > _MAX_STEM_LENGTH:
        text = text[:_MAX_STEM_LENGTH].rstrip(" .")
    return text or fallback


def flat_stem(item: MediaItem) -> str:
    """Return the default flat staging stem for ``item``."""
    if item.suggested_stem:
        return item.suggested_stem
    if item.title:
        return item.title
    if item.position is not None:
        return f"item-{item.position}"
    return f"item-{item.id}"


def _stream(variant: DownloadVariant, role: StreamRole) -> StreamInfo | None:
    return next((stream for stream in variant.streams if stream.role is role), None)


def _split_selector(selector: str) -> tuple[str, str | None]:
    """Split ``exact/fallback`` at the first top-level slash."""
    depth = 0
    for index, char in enumerate(selector):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == "/" and depth == 0:
            exact = selector[:index].strip()
            fallback = selector[index + 1 :].strip()
            return exact, fallback or None
    return selector.strip(), None


def _exact_selector(variant: DownloadVariant) -> str:
    """Build a precise selector from stream ids when the mapper omitted one."""
    video = _stream(variant, StreamRole.VIDEO)
    audio = _stream(variant, StreamRole.AUDIO)
    if video is not None and audio is not None:
        if video.format_id and audio.format_id and video.format_id != audio.format_id:
            return f"{video.format_id}+{audio.format_id}"
        if video.format_id:
            return video.format_id
        if audio.format_id:
            return audio.format_id
    if video is not None and video.format_id:
        return video.format_id
    if audio is not None and audio.format_id:
        return audio.format_id
    return ""


def _fallback_selector(variant: DownloadVariant) -> str:
    video = _stream(variant, StreamRole.VIDEO)
    audio = _stream(variant, StreamRole.AUDIO)
    if video is not None:
        height = video.height
        if height:
            return f"bv*[height={height}]+ba/b[height={height}]"
        return "bv*+ba/b"
    if audio is not None:
        ext = (audio.ext or variant.container or "").strip().lower()
        return f"ba[ext={ext}]/ba/b" if ext else "ba/b"
    if variant.kind is MediaKind.IMAGE:
        return "b/best"
    return "b/best"


def selectors_for_variant(variant: DownloadVariant) -> tuple[str, str | None]:
    """Return ``(selector, fallback)`` for one variant.

    ``selector`` always contains the exact format id(s) first and a broader
    fallback second, so yt-dlp can still recover from a vanished format.
    """
    fallback = _fallback_selector(variant)
    raw = (variant.selector or "").strip()
    exact_from_streams = _exact_selector(variant)
    if not raw:
        exact = exact_from_streams or fallback
    else:
        exact, embedded = _split_selector(raw)
        if embedded is not None:
            # The mapper already produced an ``exact/fallback`` expression.
            return raw, embedded
        if not exact:
            exact = exact_from_streams or fallback
    if exact == fallback:
        return exact, None
    return f"{exact}/{fallback}", fallback


def _allocate_stem(base: str, used: set[str]) -> str:
    candidate = base
    if candidate.casefold() not in used:
        used.add(candidate.casefold())
        return candidate
    number = 1
    while f"{number}/{candidate}".casefold() in used:
        number += 1
    candidate = f"{number}/{base}"
    used.add(candidate.casefold())
    return candidate


class YtDlpPlanner:
    """Resolve ``ItemSelection`` objects into a :class:`DownloadPlan`.

    The planner can be used either as a stateful helper
    (``YtDlpPlanner(request, job_id="...").plan()``) or as a stateless one
    (``YtDlpPlanner().plan(request, job_id="...")``).
    """

    def __init__(
        self,
        request: DownloadRequest | None = None,
        job_id: str | None = None,
    ) -> None:
        self._request = request
        self._job_id = job_id

    def plan(
        self,
        request: DownloadRequest | None = None,
        job_id: str | None = None,
    ) -> DownloadPlan:
        request = request or self._request
        resolved_job = job_id or self._job_id
        if request is None:
            raise ValueError("a DownloadRequest is required")
        if resolved_job is None:
            raise ValueError("a job_id is required")
        return self._build(request, resolved_job)

    build = plan

    def _build(self, request: DownloadRequest, job_id: str) -> DownloadPlan:
        items_by_id = {item.id: item for item in request.source_info.items}
        used: set[str] = set()
        plans: list[ItemPlan] = []
        for selection in request.selections:
            item = self._find_item(items_by_id, selection)
            variant = self._find_variant(item, selection)
            selector, _fallback = selectors_for_variant(variant)
            base = safe_stem(
                request.options.staging_name
                or item.suggested_stem
                or flat_stem(item)
            )
            plans.append(
                ItemPlan(
                    item=item,
                    variant=variant,
                    selector=selector,
                    output_stem=_allocate_stem(base, used),
                )
            )
        return DownloadPlan(
            job_id=job_id,
            source=request.source_info.ref(),
            source_info=request.source_info,
            items=tuple(plans),
            options=request.options,
        )

    @staticmethod
    def _find_item(
        items_by_id: dict[str, MediaItem],
        selection: ItemSelection,
    ) -> MediaItem:
        item = items_by_id.get(selection.item_id)
        if item is None:
            raise VariantUnavailable(
                f"item {selection.item_id!r} is not part of this source; "
                "refresh the item and select again"
            )
        if item.resolution in {ResolutionState.FLAT, ResolutionState.UNRESOLVED}:
            raise VariantUnavailable(
                f"item {item.id!r} is not fully resolved; "
                "inspect its page URL before downloading"
            )
        return item

    @staticmethod
    def _find_variant(
        item: MediaItem,
        selection: ItemSelection,
    ) -> DownloadVariant:
        variant = next(
            (candidate for candidate in item.variants if candidate.id == selection.variant_id),
            None,
        )
        if variant is None:
            raise VariantUnavailable(
                f"variant {selection.variant_id!r} is not available for item "
                f"{item.id!r}; refresh the item and choose again"
            )
        if not variant.downloadable or variant.unavailable_reason:
            reason = variant.unavailable_reason or "variant is marked unavailable"
            raise VariantUnavailable(
                f"variant {variant.id!r} cannot be downloaded: {reason}"
            )
        if any(stream.drm for stream in variant.streams):
            raise VariantUnavailable(
                f"variant {variant.id!r} is DRM-protected and cannot be downloaded"
            )
        return variant


__all__ = [
    "DownloadPlan",
    "ItemPlan",
    "YtDlpPlanner",
    "flat_stem",
    "safe_stem",
    "selectors_for_variant",
]
