"""Interactive and non-interactive item/variant selection for the CLI.

The functions here are deliberately UI-light: they render a numbered list and
read one line at a time.  Nothing touches the network and no selection is ever
made implicitly unless the caller explicitly asked for it with ``--yes``.
"""

# pyright: standard

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from trackforge.acquire.errors import CancelledError
from trackforge.acquire.models import (
    DownloadVariant,
    MediaItem,
    ResolutionState,
    SourceInfo,
    SourceKind,
    StreamRole,
)
from trackforge.acquire.redaction import redact_text

Ask = Callable[[str], str]
Echo = Callable[[str], None]

_HEIGHT_TOKEN = re.compile(
    r"^(?P<height>\d{2,4})p(?:[-_ ](?P<container>[a-z0-9]+))?$",
    re.IGNORECASE,
)
_RECOMMENDED_TOKENS = {
    "recommended",
    "recommended:audio",
    "recommended:video",
    "rec",
    "best",
}


class SelectionError(Exception):
    """The user picked an item or variant that does not exist."""


class SelectionRequired(Exception):
    """A choice is required, but stdin is not interactive."""

    def __init__(
        self,
        message: str,
        *,
        source: SourceInfo,
        items: Sequence[MediaItem] = (),
    ) -> None:
        super().__init__(message)
        self.source = source
        self.items = tuple(items)


@dataclass(frozen=True, slots=True)
class SelectionSpec:
    """Raw CLI selection options, before they are resolved against an item."""

    variant: str | None = None
    audio: str | None = None
    video: str | None = None
    yes: bool = False
    pick: int | None = None

    @property
    def explicit(self) -> bool:
        return any(
            value is not None
            for value in (self.variant, self.audio, self.video)
        )

    @property
    def requested_use(self) -> str | None:
        if self.audio is not None:
            return "audio"
        if self.video is not None:
            return "video"
        return None


def variant_height(variant: DownloadVariant) -> int | None:
    heights = [
        stream.height
        for stream in variant.streams
        if stream.role is StreamRole.VIDEO and stream.height is not None
    ]
    return max(heights) if heights else None


def variant_is_downloadable(variant: DownloadVariant) -> bool:
    if not variant.downloadable or variant.unavailable_reason:
        return False
    return not any(stream.drm for stream in variant.streams)


def downloadable_variants(item: MediaItem) -> tuple[DownloadVariant, ...]:
    return tuple(
        variant for variant in item.variants if variant_is_downloadable(variant)
    )


def _by_id(item: MediaItem, variant_id: str) -> DownloadVariant | None:
    exact = next(
        (variant for variant in item.variants if variant.id == variant_id),
        None,
    )
    if exact is not None:
        return exact
    folded = variant_id.casefold()
    return next(
        (variant for variant in item.variants if variant.id.casefold() == folded),
        None,
    )


def _recommended_id(item: MediaItem, use: str | None) -> str | None:
    recommendations = item.recommendations
    if use == "audio":
        return recommendations.audio
    if use == "video":
        return recommendations.video
    return recommendations.video or recommendations.audio


def _recommended_variant(item: MediaItem, use: str | None) -> DownloadVariant | None:
    if use is None:
        return _recommended_variant(item, "video") or _recommended_variant(
            item,
            "audio",
        )
    candidate_id = _recommended_id(item, use)
    if candidate_id is None:
        return None
    candidate = _by_id(item, candidate_id)
    if candidate is None or not variant_is_downloadable(candidate):
        return None
    return candidate


def default_variant(item: MediaItem) -> DownloadVariant:
    """Return the loader recommendation, falling back to the first usable one."""
    recommended = _recommended_variant(item, None)
    if recommended is not None:
        return recommended
    usable = downloadable_variants(item)
    if not usable:
        raise SelectionError(
            f"item {item.id!r} has no downloadable variants"
        )
    return usable[0]


def _match_height(
    item: MediaItem,
    height: int,
    container: str | None,
) -> DownloadVariant | None:
    candidates = [
        variant
        for variant in downloadable_variants(item)
        if variant_height(variant) == height
        and (
            container is None
            or (variant.container or "").casefold() == container.casefold()
        )
    ]
    if not candidates:
        return None
    recommended = _recommended_variant(item, "video")
    if recommended is not None:
        for candidate in candidates:
            if candidate.id == recommended.id:
                return candidate
    return max(
        candidates,
        key=lambda variant: (
            variant.filesize or variant.filesize_approx or 0,
            variant.label,
        ),
    )


def _resolve_token(
    item: MediaItem,
    token: str,
    *,
    use: str | None,
) -> DownloadVariant:
    text = token.strip()
    if not text:
        raise SelectionError("variant must not be empty")
    folded = text.casefold()

    if folded in _RECOMMENDED_TOKENS:
        resolved_use = use
        if folded == "recommended:audio":
            resolved_use = "audio"
        elif folded == "recommended:video":
            resolved_use = "video"
        variant = _recommended_variant(item, resolved_use)
        if variant is None:
            label = resolved_use or "video/audio"
            raise SelectionError(
                f"item {item.id!r} has no usable recommended {label} variant"
            )
        return variant

    candidate = _by_id(item, text)
    if candidate is None:
        candidate = next(
            (
                variant
                for variant in item.variants
                if variant.label.casefold() == folded
            ),
            None,
        )
    if candidate is None:
        height_match = _HEIGHT_TOKEN.match(text)
        if height_match is not None:
            candidate = _match_height(
                item,
                int(height_match.group("height")),
                height_match.group("container"),
            )
    if candidate is None:
        available = ", ".join(variant.id for variant in item.variants) or "none"
        raise SelectionError(
            f"unknown variant {text!r} for item {item.id!r}; "
            f"available: {available}"
        )
    if not variant_is_downloadable(candidate):
        reason = candidate.unavailable_reason or "not downloadable"
        raise SelectionError(
            f"variant {candidate.id!r} cannot be downloaded: {reason}"
        )
    return candidate


def resolve_explicit_variant(item: MediaItem, spec: SelectionSpec) -> DownloadVariant:
    """Resolve ``--variant``/``--audio``/``--video`` against one item."""
    if spec.variant is not None:
        return _resolve_token(item, spec.variant, use=None)
    if spec.audio is not None:
        return _resolve_token(item, spec.audio, use="audio")
    if spec.video is not None:
        return _resolve_token(item, spec.video, use="video")
    raise ValueError("no explicit variant option was provided")


def _format_duration(value: float | None) -> str:
    if value is None:
        return "—"
    total = max(0, int(value))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _thumbnail_label(item: MediaItem) -> str:
    thumbnail = item.thumbnail
    if thumbnail is None or not thumbnail.url:
        return "—"
    size = (
        f"{thumbnail.width}x{thumbnail.height}"
        if thumbnail.width and thumbnail.height
        else "?x?"
    )
    return f"{redact_text(thumbnail.url)} ({size})"


def render_item_table(source: SourceInfo, echo: Echo) -> None:
    """Render collection items as a numbered list."""
    echo("Items:")
    echo(" #  Title                                     Duration  Thumbnail")
    for index, item in enumerate(source.items, start=1):
        title = item.title or item.id
        if len(title) > 40:
            title = title[:39] + "…"
        echo(
            f"{index:>3}  {title:<40}  {_format_duration(item.duration):>8}  "
            f"{_thumbnail_label(item)}"
        )


def render_variant_table(
    item: MediaItem,
    echo: Echo,
    *,
    heading: str | None = None,
) -> None:
    """Render one item and its variants, marking loader recommendations."""
    if heading:
        echo(heading)
    creator = item.creator or "—"
    echo(f"Title:     {item.title or item.id}")
    echo(f"Creator:   {creator}")
    echo(f"Duration:  {_format_duration(item.duration)}")
    echo(f"Thumbnail: {_thumbnail_label(item)}")
    if not item.variants:
        echo("Variants:  none")
        return

    recommended = {
        item.recommendations.video,
        item.recommendations.audio,
        item.recommendations.image,
        item.recommendations.document,
    }
    echo("Variants:")
    for index, variant in enumerate(item.variants, start=1):
        star = "★ " if variant.id in recommended else "  "
        height = variant_height(variant)
        height_text = f"{height}p" if height is not None else "—"
        merge = " merge" if variant.requires_merge else ""
        line = (
            f"{index:>3}. {star}{variant.label} "
            f"[{variant.kind.value}, {variant.container or '—'}, "
            f"{height_text}, {variant.size_label}{merge}] ({variant.id})"
        )
        if not variant_is_downloadable(variant):
            line += f" — unavailable: {variant.unavailable_reason or 'no'}"
        echo(line)


def _parse_item_indices(raw: str, count: int) -> list[int] | None:
    text = raw.strip().casefold()
    if text in {"a", "all", "*"}:
        return list(range(1, count + 1))
    if not text:
        return [1]

    selected: list[int] = []
    for part in text.split(","):
        fragment = part.strip()
        if not fragment:
            return None
        if "-" in fragment:
            left, _, right = fragment.partition("-")
            try:
                start = int(left)
                end = int(right)
            except ValueError:
                return None
            if start > end:
                return None
            indices = range(start, end + 1)
        else:
            try:
                indices = (int(fragment),)
            except ValueError:
                return None
        for index in indices:
            if index < 1 or index > count:
                return None
            if index not in selected:
                selected.append(index)
    return selected or None


def select_items(
    source: SourceInfo,
    spec: SelectionSpec,
    *,
    interactive: bool,
    ask: Ask,
    echo: Echo,
) -> tuple[MediaItem, ...]:
    """Choose collection items; single items are always selected."""
    items = tuple(source.items)
    if not items:
        raise SelectionError("source has no items")
    if source.kind is not SourceKind.COLLECTION or len(items) == 1:
        return items
    if not interactive or spec.yes:
        raise SelectionRequired(
            "a playlist/collection needs an explicit item selection",
            source=source,
            items=items,
        )

    render_item_table(source, echo)
    while True:
        raw = ask(
            f"Select items [1-{len(items)}, 1,3-5, a=all, q]: "
        )
        if raw.strip().casefold() in {"q", "quit"}:
            raise CancelledError("selection cancelled")
        indices = _parse_item_indices(raw, len(items))
        if indices is not None:
            return tuple(items[index - 1] for index in indices)
        echo("error: enter item numbers such as 1, 1,3 or 1-2")


def _ask_variant(
    item: MediaItem,
    *,
    ask: Ask,
    echo: Echo,
) -> DownloadVariant:
    usable = downloadable_variants(item)
    if not usable:
        raise SelectionError(f"item {item.id!r} has no downloadable variants")
    default = default_variant(item)
    prompt = (
        f"Select [1-{len(item.variants)}, a=recommended audio, "
        "v=recommended video, q]: "
    )
    while True:
        raw = ask(prompt)
        text = raw.strip()
        folded = text.casefold()
        if not text:
            return default
        if folded in {"q", "quit"}:
            raise CancelledError("selection cancelled")
        if folded in {"a", "audio"}:
            variant = _recommended_variant(item, "audio")
            if variant is None:
                echo("error: this item has no recommended audio variant")
                continue
            return variant
        if folded in {"v", "video"}:
            variant = _recommended_variant(item, "video")
            if variant is None:
                echo("error: this item has no recommended video variant")
                continue
            return variant
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(item.variants):
                candidate = item.variants[index - 1]
                if variant_is_downloadable(candidate):
                    return candidate
                echo(
                    "error: "
                    f"{candidate.id} is unavailable: "
                    f"{candidate.unavailable_reason or 'no'}"
                )
                continue
            echo(f"error: choose a number between 1 and {len(item.variants)}")
            continue
        try:
            return _resolve_token(item, text, use=None)
        except SelectionError as exc:
            echo(f"error: {exc}")


def select_variant(
    item: MediaItem,
    spec: SelectionSpec,
    *,
    interactive: bool,
    ask: Ask,
    echo: Echo,
) -> DownloadVariant:
    """Resolve one variant using explicit options, ``--yes`` or a prompt."""
    if spec.explicit:
        return resolve_explicit_variant(item, spec)
    if spec.yes:
        return default_variant(item)
    if not interactive:
        raise SelectionRequired(
            "a variant selection is required; pass --variant, "
            "--audio/--video, or --yes",
            source=SourceInfo(
                source="",
                url=item.page_url or "",
                kind=SourceKind.SINGLE,
                items=(item,),
            ),
            items=(item,),
        )
    render_variant_table(item, echo)
    return _ask_variant(item, ask=ask, echo=echo)


def confirm_playlist(count: int, *, ask: Ask, echo: Echo) -> None:
    """Ask for confirmation before downloading a multi-item playlist."""
    del echo
    raw = ask(f"Download {count} item(s)? [Y/n]: ").strip().casefold()
    if raw not in {"", "y", "yes"}:
        raise CancelledError("download cancelled")


def selection_needs_resolution(item: MediaItem) -> bool:
    """Whether a flat entry must be resolved through its page URL first."""
    return (
        item.resolution in {ResolutionState.FLAT, ResolutionState.UNRESOLVED}
        or not item.variants
    )


__all__ = [
    "SelectionError",
    "SelectionRequired",
    "SelectionSpec",
    "confirm_playlist",
    "default_variant",
    "downloadable_variants",
    "render_item_table",
    "render_variant_table",
    "resolve_explicit_variant",
    "select_items",
    "select_variant",
    "selection_needs_resolution",
    "variant_height",
    "variant_is_downloadable",
]
