"""yt-dlp playlist enumeration.

The source uses ``--flat-playlist --dump-single-json`` and never requests
media bytes.  Per-item candidates intentionally have no format list: download
selection happens one item at a time through the normal acquirer, which also
captures the final per-item snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from trackforge.adapters.http.url_safety import UnsafeUrl, sanitize_url
from trackforge.adapters.sources.ytdlp.client import DumpOptions
from trackforge.adapters.sources.ytdlp.source import (
    YtDlpSource,
    _looks_like_playlist_url,
    fallback_external_id,
)
from trackforge.domain.errors import (
    ExtractorBroken,
    OfflineModeError,
    Unavailable,
    UnsupportedSource,
    UnsupportedUrl,
)
from trackforge.domain.model.collection import (
    Collection,
    CollectionContextClaim,
    CollectionItem,
    CollectionKind,
)
from trackforge.domain.model.ids import CandidateId, CollectionId, new_uuid7
from trackforge.domain.model.source import Candidate, SourceRef, Thumbnail, Url


class YtDlpPlaylistSource(YtDlpSource):
    """Enumerate a playlist/album URL as an ordered collection."""

    id = "ytdlp"
    version = "1"

    def supports(self, ref: SourceRef) -> bool:
        """Alias used by generic resolver call sites."""

        return self.supports_collection(ref)

    def supports_collection(self, ref: SourceRef) -> bool:
        if not isinstance(ref, Url):
            return False
        if not self._guard.supports(ref.url):
            return False
        try:
            self._guard.validate(ref.url)
        except UnsafeUrl:
            return False
        return _looks_like_playlist_url(ref.url)

    async def resolve_collection(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> Collection:
        if not isinstance(ref, Url):
            raise UnsupportedSource(
                "ytdlp playlist source only supports URLs",
                user_action="Pass a playlist or album URL.",
                context={"source_type": type(ref).__name__},
            )
        options = dict(opts or {})
        if bool(options.get("offline")):
            raise OfflineModeError(
                "offline mode does not allow playlist resolution",
                context={"url": sanitize_url(ref.url)},
            )
        self._guard.validate(ref.url)
        if not _looks_like_playlist_url(ref.url):
            raise UnsupportedUrl(
                "the URL does not look like a playlist or album",
                user_action="Use a playlist URL, or add the single-track URL directly.",
                context={"url": sanitize_url(ref.url)},
            )
        cookie = self._cookies.select(ref.url, site_hint=ref.site_hint)
        document = self._client.dump_json(
            ref.url,
            DumpOptions(
                cookies_path=cookie.path if cookie is not None else None,
                cookie_validated=cookie is not None,
                site_hint=ref.site_hint,
                playlist=True,
                flat_playlist=True,
                playlist_start=_optional_positive_int(options.get("playlist_start")),
                playlist_end=_optional_positive_int(options.get("playlist_end")),
                playlist_items=_playlist_items(options.get("playlist_items")),
            ),
        )
        if not _is_playlist_document(document):
            raise UnsupportedUrl(
                "yt-dlp did not return a playlist document",
                user_action="Use a playlist URL, or add the single-track URL directly.",
                context={"url": sanitize_url(ref.url)},
            )

        title = _text(document.get("title")) or "Playlist"
        owner = _text(document.get("uploader")) or _text(document.get("channel"))
        description = _text(document.get("description"))
        claims = _playlist_context_claims(document, title=title, owner=owner)
        thumbnails = _thumbnails(document.get("thumbnails"))
        if thumbnails and not any(claim.field == "cover" for claim in claims):
            claims = (
                *claims,
                CollectionContextClaim(
                    field="cover",
                    value=max(
                        thumbnails,
                        key=lambda item: (item.width or 0) * (item.height or 0),
                    ).url,
                    source="ytdlp_playlist",
                    confidence=0.6,
                ),
            )

        warnings: list[str] = []
        items: list[CollectionItem] = []
        raw_entries = document.get("entries")
        if not isinstance(raw_entries, (list, tuple)):
            raw_entries = []
        start_fallback = _optional_positive_int(options.get("playlist_start")) or 1
        position_hints = [
            int(item) for item in _playlist_items(options.get("playlist_items")) if item.isdigit()
        ]
        seen_positions: set[int] = set()
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, Mapping):
                warnings.append(f"playlist_entry_skipped:not_an_object:{index + 1}")
                continue
            url = _entry_url(raw_entry)
            if url is None:
                warnings.append(f"playlist_entry_skipped:missing_url:{index + 1}")
                continue
            external_id = (
                _text(raw_entry.get("id"))
                or _text(raw_entry.get("display_id"))
                or fallback_external_id(url)
            )
            entry_title = (
                _text(raw_entry.get("title"))
                or _text(raw_entry.get("fulltitle"))
                or _text(raw_entry.get("track"))
                or external_id
            )
            fallback_position = (
                position_hints[index] if index < len(position_hints) else start_fallback + index
            )
            position = _entry_position(
                raw_entry,
                fallback=fallback_position,
            )
            if position in seen_positions:
                raise ExtractorBroken(
                    "playlist metadata contains a duplicate item position",
                    context={"position": position, "external_id": external_id},
                )
            seen_positions.add(position)
            items.append(
                CollectionItem(
                    position=position,
                    candidate=Candidate(
                        id=CandidateId(new_uuid7()),
                        source_ref=Url(url, site_hint=ref.site_hint),
                        external_id=external_id,
                        title=entry_title,
                        uploader=_text(raw_entry.get("uploader"))
                        or _text(raw_entry.get("channel"))
                        or owner,
                        duration_seconds=_number(raw_entry.get("duration")),
                        thumbnails=_thumbnails(raw_entry.get("thumbnails")),
                        extra={
                            "extractor": _text(raw_entry.get("extractor"))
                            or _text(document.get("extractor"))
                            or "",
                            "webpage_url": sanitize_url(url) or "",
                            "playlist_id": _text(document.get("id")) or "",
                            "playlist_title": title,
                            "playlist_owner": owner or "",
                            "playlist_position": position,
                            "selected_format_id": "",
                        },
                    ),
                )
            )

        if not items:
            raise Unavailable(
                "the playlist contains no downloadable entries",
                user_action="Check that the playlist is public and not empty.",
                context={"url": sanitize_url(ref.url)},
            )
        return Collection(
            id=CollectionId(new_uuid7()),
            kind=_collection_kind(ref.url, document),
            source_ref=ref,
            title=title,
            owner=owner,
            description=description,
            context_claims=claims,
            items=tuple(items),
            warnings=tuple(warnings),
        )

    async def resolve(  # type: ignore[override]
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> Collection:
        """Collection-oriented alias used by generic resolver callers."""

        return await self.resolve_collection(ref, opts)


def _collection_kind(url: str, document: Mapping[str, Any]) -> CollectionKind:
    try:
        path = urlsplit(url).path.casefold()
    except ValueError:
        path = ""
    documented = (_text(document.get("_type")) or "").casefold()
    if documented == "album" or "/album/" in path:
        return CollectionKind.ALBUM
    if "/channel/" in path or "/user/" in path:
        return CollectionKind.CHANNEL
    return CollectionKind.PLAYLIST


def _is_playlist_document(document: Mapping[str, Any]) -> bool:
    kind = (_text(document.get("_type")) or "").casefold()
    if kind in {"playlist", "multi_video"}:
        return True
    entries = document.get("entries")
    return isinstance(entries, (list, tuple)) and bool(entries)


def _entry_url(entry: Mapping[str, Any]) -> str | None:
    for key in ("webpage_url", "original_url", "url"):
        value = _text(entry.get(key))
        if value and value.startswith(("http://", "https://")):
            return value
    external_id = _text(entry.get("id"))
    extractor = (_text(entry.get("extractor")) or "").casefold()
    ie_key = (_text(entry.get("ie_key")) or "").casefold()
    if external_id and ("youtube" in extractor or "youtube" in ie_key):
        return f"https://www.youtube.com/watch?v={external_id}"
    return None


def _entry_position(entry: Mapping[str, Any], *, fallback: int) -> int:
    value = entry.get("playlist_index")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return max(1, fallback)


def _playlist_context_claims(
    document: Mapping[str, Any],
    *,
    title: str,
    owner: str | None,
) -> tuple[CollectionContextClaim, ...]:
    claims = [
        CollectionContextClaim(
            field="title",
            value=title,
            source="ytdlp_playlist",
            confidence=0.9,
            evidence=(f"playlist:{title}",),
        )
    ]
    if owner:
        claims.append(
            CollectionContextClaim(
                field="owner",
                value=owner,
                source="ytdlp_playlist",
                confidence=0.7,
            )
        )
    description = _text(document.get("description"))
    if description:
        claims.append(
            CollectionContextClaim(
                field="description",
                value=description,
                source="ytdlp_playlist",
                confidence=0.6,
            )
        )
    date = _normalize_date(
        _text(document.get("release_date"))
        or _text(document.get("upload_date"))
        or _text(document.get("release_year"))
    )
    if date:
        claims.append(
            CollectionContextClaim(
                field="date",
                value=date,
                source="ytdlp_playlist",
                confidence=0.5,
            )
        )
    return tuple(claims)


def _normalize_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _thumbnails(value: object) -> tuple[Thumbnail, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[Thumbnail] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        url = _text(raw.get("url"))
        if not url:
            continue
        width = raw.get("width")
        height = raw.get("height")
        result.append(
            Thumbnail(
                url=url,
                width=width if isinstance(width, int) and not isinstance(width, bool) else None,
                height=height if isinstance(height, int) and not isinstance(height, bool) else None,
            )
        )
    return tuple(result)


def _playlist_items(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw: tuple[object, ...] = tuple(part for part in value.split(",") if part.strip())
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = tuple(value)
    else:
        return (str(value),)
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _optional_positive_int(value: object) -> int | None:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


__all__ = ["YtDlpPlaylistSource"]
