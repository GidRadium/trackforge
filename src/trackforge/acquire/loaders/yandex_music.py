"""Yandex Music site policy on top of the generic yt-dlp loader.

Yandex Music needs the external ``yt-dlp-yandex-music`` plugin: the built-in
yt-dlp extractor still targets retired ``handlers/*.jsx`` endpoints.  The
plugin also supplies album/playlist/artist/shared/liked collections.

A logged-in session is mandatory for metadata in practice.  With an expired
session the API can silently return 13--30 second ``smart_preview`` streams;
:func:`warn_on_smart_preview` turns that into a visible ``PreviewOnly``
warning instead of a silent success.
"""

# pyright: standard

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

from ...metadata.model import PartialDate
from ..config import SourceProfile
from ..download import looks_like_preview, probe_media_duration
from ..errors import AuthRequired, MissingPlugin, PreviewOnly, UnsupportedUrl
from ..models import (
    MediaItem,
    MediaKind,
    Recommendations,
    ResolutionState,
    SourceInfo,
    SourceKind,
    SourceRef,
    Thumbnail,
    UrlHint,
)
from ..yandex_plugin import plugin_available, plugin_install_hint
from .base import (
    LoaderCapabilities,
    LoaderContext,
    rank_thumbnails,
    select_best_thumbnail,
)
from .ytdlp import INFO_WARNINGS_KEY, YtDlpLoader, effective_flat_limit
from .ytdlp_mapper import best_variant

if TYPE_CHECKING:
    from ..planner import ItemPlan

_URL_HOSTS = frozenset(
    {
        "music.yandex.ru",
        "music.yandex.kz",
        "music.yandex.ua",
        "music.yandex.by",
        "music.yandex.com",
    }
)
_URL_RE = re.compile(
    r"^https?://music\.yandex\.(?:ru|kz|ua|by|com)(?:/|$)",
    re.IGNORECASE,
)
_COLLECTION_MARKERS = ("/album/", "/playlists/", "/artist/")
_COVER_SIZE_RE = re.compile(r"/(\d{2,5})x(\d{2,5})(?:\.[A-Za-z0-9]+)?/?(?:$|[?#])")
_ORIG_COVER_RE = re.compile(r"/\d{2,5}x\d{2,5}(?:\.[A-Za-z0-9]+)?$")
_TRACK_ID_RE = re.compile(r"/track/(\d+)(?:[/?#]|$)")
_YANDEX_API = "https://api.music.yandex.ru"
_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en",
    "Origin": "https://music.yandex.ru",
    "Referer": "https://music.yandex.ru/",
    "X-Yandex-Music-Client": "YandexMusicWebNext/1.0.0",
}
_PREVIEW_QUALITIES = frozenset({"preview", "smart_preview"})
_PREVIEW_MIN_SECONDS = 13.0
_PREVIEW_MAX_SECONDS = 30.0


def _split_names(values: tuple[str, ...]) -> tuple[str, ...]:
    """Split names the plugin joined with ``", "`` into real artists."""
    result: list[str] = []
    for value in values:
        for part in re.split(r"\s*,\s*", value):
            name = part.strip()
            if name and name not in result:
                result.append(name)
    return tuple(result)


def _strip_duplicate_artist(title: str, item: MediaItem) -> str:
    """Remove a leading ``"Artist - "`` that duplicates ``creator``.

    The plugin normally returns the plain track title, but older/alternative
    payloads repeat the artist.  Never strip when doing so would empty the
    title.
    """
    prefixes = {
        prefix
        for prefix in (
            item.creator,
            ", ".join(item.artists),
            *item.artists,
            *(_split_names(item.artists)[:1]),
        )
        if prefix
    }
    for prefix in sorted(prefixes, key=len, reverse=True):
        lead = f"{prefix} - "
        if title.casefold().startswith(lead.casefold()) and len(title) > len(lead):
            return title[len(lead):].strip()
    return title


def normalize_yandex_titles(source: SourceInfo) -> SourceInfo:
    """Normalize Yandex's ``Artist - Track`` convention and artist lists."""
    items: list[MediaItem] = []
    for item in source.items:
        title = _strip_duplicate_artist(item.title, item)
        artists = _split_names(item.artists)
        album_artists = _split_names(item.album_artists)
        # The primary artist is more useful as ``creator`` than the joined
        # list; all names remain available in ``artists``.
        creator = artists[0] if artists else item.creator
        suggested_stem = item.suggested_stem
        if creator and title:
            prefix = f"{creator} - "
            suggested_stem = (
                title
                if title.casefold().startswith(prefix.casefold())
                else f"{creator} - {title}"
            )
        items.append(
            replace(
                item,
                title=title,
                creator=creator,
                artists=artists,
                album_artists=album_artists,
                suggested_stem=suggested_stem,
            )
        )
    normalized_items = tuple(items)
    title = source.title
    creator = source.creator
    if source.kind is SourceKind.SINGLE and normalized_items:
        title = normalized_items[0].title
        creator = normalized_items[0].creator
    return replace(
        source,
        title=title,
        creator=creator,
        items=normalized_items,
    )


def _preview_reason(item: MediaItem) -> str | None:
    """Return why an item looks like a ``smart_preview`` stream, if it does."""
    if item.resolution is ResolutionState.FLAT:
        # Flat items have no stream to judge; API metadata may be short but
        # that says nothing about a served preview.
        return None
    extra_quality = item.extras.get("quality")
    if isinstance(extra_quality, str):
        quality = extra_quality.strip().lower()
        if quality in _PREVIEW_QUALITIES:
            return f"quality={quality!r}"
    if item.extras.get("smart_preview") is True:
        return "payload is marked smart_preview"
    if item.extras.get("preview") is True:
        return "payload is marked as a preview"
    duration = item.duration
    if (
        duration is not None
        and _PREVIEW_MIN_SECONDS <= duration <= _PREVIEW_MAX_SECONDS
    ):
        return (
            f"duration is {duration:.0f}s, inside the "
            f"{_PREVIEW_MIN_SECONDS:.0f}–{_PREVIEW_MAX_SECONDS:.0f}s preview range"
        )
    return None


def warn_on_smart_preview(source: SourceInfo) -> SourceInfo:
    """Add a visible ``PreviewOnly`` warning for suspicious Yandex items."""
    warnings = list(source.warnings)
    for item in source.items:
        reason = _preview_reason(item)
        if reason is None:
            continue
        message = (
            f"PreviewOnly: item {item.id!r} may be a Yandex Music preview "
            f"({reason}); refresh the session cookies or try another account"
        )
        if message not in warnings and not any(
            "PreviewOnly" in warning and item.id in warning for warning in warnings
        ):
            warnings.append(message)
    if len(warnings) == len(source.warnings):
        return source
    return replace(source, warnings=tuple(warnings))


def _has_session(profile: SourceProfile) -> bool:
    return bool(profile.cookies_file or profile.cookies_from_browser)


def _missing_plugin_error() -> MissingPlugin:
    return MissingPlugin(
        "Yandex Music requires the 'yt-dlp-yandex-music' plugin; "
        "the built-in yt-dlp extractor is broken",
        hint=plugin_install_hint(),
    )


def _normalize_cover(thumbnail: Thumbnail) -> Thumbnail:
    """Expand Yandex ``%%`` cover tokens and trust the URL's own size."""
    url = thumbnail.url.strip()
    if "%%" in url:
        url = url.replace("%%", "1000x1000")
    width = thumbnail.width
    height = thumbnail.height
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    match = _COVER_SIZE_RE.search(path)
    if match is not None:
        width = int(match.group(1))
        height = int(match.group(2))
    if url == thumbnail.url and (width, height) == (
        thumbnail.width,
        thumbnail.height,
    ):
        return thumbnail
    return replace(thumbnail, url=url, width=width, height=height)


def _orig_cover_url(url: str) -> str | None:
    """Derive Yandex's ``/orig`` cover URL from a sized cover URL."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    path = parsed.path.rstrip("/")
    if not path or path.endswith("/orig"):
        return None
    if path.endswith("/%%"):
        new_path = f"{path[:-3]}/orig"
    else:
        new_path = _ORIG_COVER_RE.sub("/orig", path, count=1)
        if new_path == path:
            return None
    return urlunsplit(
        (parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment)
    )


def _normalize_source_covers(source: SourceInfo) -> SourceInfo:
    """Normalize collection-level cover URLs and their sizes."""
    thumbnails: list[Thumbnail] = []
    seen: set[str] = set()
    for thumbnail in source.thumbnails:
        normalized = _normalize_cover(thumbnail)
        if normalized.url in seen:
            continue
        seen.add(normalized.url)
        thumbnails.append(normalized)
    normalized_thumbnails = tuple(thumbnails)
    thumbnail = source.thumbnail
    if thumbnail is not None:
        thumbnail = _normalize_cover(thumbnail)
    else:
        thumbnail = select_best_thumbnail(normalized_thumbnails)
    return replace(
        source,
        thumbnails=normalized_thumbnails,
        thumbnail=thumbnail,
    )


def _flat_api_target(url: str) -> tuple[str, tuple[str, ...]] | None:
    """Return the Yandex API target for a flat collection page, if known."""
    try:
        path = urlsplit(url).path.rstrip("/")
    except ValueError:
        return None
    match = re.fullmatch(r"/artist/(\d+)(?:/tracks)?", path)
    if match is not None:
        return "artist", (match.group(1),)
    match = re.fullmatch(r"/album/(\d+)", path)
    if match is not None:
        return "album", (match.group(1),)
    match = re.fullmatch(r"/users/([^/]+)/playlists/([^/]+)", path)
    if match is not None:
        return "playlist", (match.group(1), match.group(2))
    match = re.fullmatch(r"/playlists/([^/]+)", path)
    if match is not None:
        return "shared_playlist", (match.group(1),)
    return None


def _track_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _TRACK_ID_RE.search(url)
    if match is not None:
        return match.group(1)
    try:
        last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    except ValueError:
        return None
    return last if last.isdigit() else None


def _api_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for entry in value:
        if isinstance(entry, Mapping):
            name = entry.get("name")
        else:
            name = entry
        text = str(name).strip() if name is not None else ""
        if text and text not in names:
            names.append(text)
    return tuple(names)


def _api_cover(value: object) -> Thumbnail | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if text.startswith("//"):
        text = f"https:{text}"
    elif not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return _normalize_cover(Thumbnail(url=text, source="yandex:api"))


def _api_owner_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("name", "login"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return None


def _apply_api_track(item: MediaItem, track: Mapping[str, Any]) -> MediaItem:
    """Fill a flat item with metadata from a Yandex API track object."""
    track_id = track.get("id")
    title = str(track.get("title") or item.title).strip() or item.title
    artists = _api_names(track.get("artists"))
    albums = track.get("albums")
    album: Mapping[str, Any] = {}
    if isinstance(albums, list) and albums and isinstance(albums[0], Mapping):
        album = albums[0]
    album_artists = _api_names(album.get("artists"))
    creator = artists[0] if artists else item.creator
    duration = item.duration
    duration_ms = track.get("durationMs")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        duration = float(duration_ms) / 1000
    release = item.release_date
    year = album.get("year")
    if isinstance(year, int) and not isinstance(year, bool):
        release = PartialDate(year)
    position = album.get("trackPosition")
    if not isinstance(position, Mapping):
        position = {}

    extras = dict(item.extras)
    values = {
        "album_id": album.get("id"),
        "track_number": position.get("index"),
        "disc_number": position.get("volume"),
        "track_total": album.get("trackCount"),
        "genre": album.get("genre") or track.get("genre"),
        "year": year,
    }
    for key, value in values.items():
        if value is not None:
            extras[key] = value

    cover = _api_cover(track.get("coverUri") or album.get("coverUri"))
    thumbnails = item.thumbnails
    if cover is not None:
        thumbnails = (
            cover,
            *(thumbnail for thumbnail in item.thumbnails if thumbnail.url != cover.url),
        )
    suggested_stem = (
        f"{creator} - {title}"
        if creator and title and not title.casefold().startswith(f"{creator.casefold()} - ")
        else title
    )
    return replace(
        item,
        id=str(track_id) if track_id is not None else item.id,
        title=title,
        kind=MediaKind.AUDIO,
        creator=creator,
        artists=artists or item.artists,
        album=str(album.get("title")) if album.get("title") else item.album,
        album_artists=album_artists or item.album_artists,
        duration=duration,
        release_date=release,
        thumbnails=thumbnails,
        thumbnail=select_best_thumbnail(thumbnails) or item.thumbnail,
        suggested_stem=suggested_stem,
        group_id=str(album.get("id")) if album.get("id") else item.group_id,
        extras=extras,
    )


class YandexMusicLoader(YtDlpLoader):
    """Yandex Music: plugin-backed tracks, albums, playlists and artists."""

    name = "yandex_music"
    config_key = "yandex_music"
    priority = 20
    required_plugins = ("yt-dlp-yandex-music",)
    requires_ffprobe_for_download = True
    capabilities = LoaderCapabilities(
        search=False,
        playlists=True,
        cookies=True,
        proxy=True,
        auth=True,
    )

    def supports_url(self, url: str) -> bool:
        return bool(_URL_RE.match(url.strip()))

    def classify_url(self, url: str) -> UrlHint:
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return UrlHint.UNKNOWN
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or host not in _URL_HOSTS
        ):
            return UrlHint.UNKNOWN
        path = parsed.path.lower()
        if "/track/" in path:
            return UrlHint.SINGLE
        if any(marker in path for marker in _COLLECTION_MARKERS):
            return UrlHint.COLLECTION
        return UrlHint.UNKNOWN

    def is_ready(self, profile: SourceProfile) -> bool:
        """Yandex readiness is only about the plugin, not about cookies.

        Cookies are checked by :meth:`preflight` so a missing session yields
        a clear ``AuthRequired`` instead of hiding the loader entirely.
        """
        del profile
        return plugin_available()

    def unready_error(self, profile: SourceProfile) -> MissingPlugin | None:
        del profile
        if plugin_available():
            return None
        return _missing_plugin_error()

    def preflight(self, url: str, profile: SourceProfile) -> None:
        del url
        if not plugin_available():
            raise _missing_plugin_error()
        if not _has_session(profile):
            raise AuthRequired(
                "Yandex Music requires a logged-in session cookie",
                hint=(
                    "configure [sources.yandex_music].cookies with a "
                    ".yandex.ru Session_id (or cookies_from_browser)"
                ),
            )

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        try:
            source = super().get_info(ref, ctx=ctx, flat=flat, limit=limit)
        except UnsupportedUrl:
            fallback = self._fallback_playlist_source(
                ref,
                ctx=ctx,
                flat=flat,
                limit=limit,
            )
            if fallback is None:
                raise
            return fallback
        source = normalize_yandex_titles(source)
        if source.kind is SourceKind.COLLECTION and any(
            item.resolution is ResolutionState.FLAT for item in source.items
        ):
            source = self._enrich_flat_collection(
                source,
                ref=ref,
                ctx=ctx,
                limit=limit,
            )
            source = normalize_yandex_titles(source)
        source = _normalize_source_covers(source)
        return warn_on_smart_preview(source)

    def _fetch_api_json(
        self,
        url: str,
        *,
        ctx: LoaderContext,
    ) -> Mapping[str, Any] | None:
        """Fetch one Yandex API JSON document (best effort)."""
        http = ctx.http
        if http is not None:
            for method_name in ("get_json", "get_bytes", "get", "read"):
                method = getattr(http, method_name, None)
                if not callable(method):
                    continue
                try:
                    result = method(url)
                except Exception:  # noqa: BLE001 - enrichment is optional
                    return None
                if isinstance(result, Mapping):
                    return result
                if isinstance(result, (bytes, bytearray)):
                    text = bytes(result).decode("utf-8", errors="replace")
                elif isinstance(result, str):
                    text = result
                else:
                    return None
                try:
                    data = json.loads(text)
                except (TypeError, ValueError):
                    return None
                return data if isinstance(data, Mapping) else None
            return None

        ydl = None
        response = None
        try:
            from yt_dlp.networking import Request as YtDlpRequest

            options = self._base_options(ctx.profile, logger=ctx.logger)
            ydl = self._create_youtube_dl(options)
            response = ydl.urlopen(
                YtDlpRequest(url, headers=dict(_API_HEADERS))
            )
            payload = response.read()
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            data = json.loads(payload)
            return data if isinstance(data, Mapping) else None
        except Exception:  # noqa: BLE001 - fall back to bare flat entries
            return None
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if ydl is not None:
                close = getattr(ydl, "close", None)
                if callable(close):
                    close()

    def _fetch_flat_tracks(
        self,
        target: tuple[str, tuple[str, ...]],
        *,
        ctx: LoaderContext,
        limit: int,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """Return flat tracks plus the collection creator when known."""
        kind, values = target
        if kind == "artist":
            artist_id = values[0]
            tracks: list[Mapping[str, Any]] = []
            seen: set[str] = set()
            page = 0
            while len(tracks) < limit and page < 200:
                data = self._fetch_api_json(
                    f"{_YANDEX_API}/artists/{artist_id}/tracks?page={page}",
                    ctx=ctx,
                )
                if data is None:
                    break
                result = data.get("result")
                if not isinstance(result, Mapping):
                    result = data
                batch = result.get("tracks")
                if not isinstance(batch, list):
                    break
                fresh = 0
                for entry in batch:
                    if not isinstance(entry, Mapping):
                        continue
                    track_id = str(entry.get("id") or "")
                    if not track_id or track_id in seen:
                        continue
                    seen.add(track_id)
                    tracks.append(entry)
                    fresh += 1
                    if len(tracks) >= limit:
                        break
                if fresh == 0:
                    break
                pager = result.get("pager")
                total = pager.get("total") if isinstance(pager, Mapping) else None
                if isinstance(total, int) and len(tracks) >= total:
                    break
                page += 1
            return tracks[:limit], None

        if kind == "album":
            album_id = values[0]
            data = self._fetch_api_json(
                f"{_YANDEX_API}/albums/{album_id}/with-tracks",
                ctx=ctx,
            )
            if data is None:
                return [], None
            result = data.get("result")
            if not isinstance(result, Mapping):
                result = data
            album_tracks: list[Mapping[str, Any]] = []
            volumes = result.get("volumes")
            if isinstance(volumes, list):
                for volume in volumes:
                    if not isinstance(volume, list):
                        continue
                    album_tracks.extend(
                        entry for entry in volume if isinstance(entry, Mapping)
                    )
            if not album_tracks:
                raw_tracks = result.get("tracks")
                if isinstance(raw_tracks, list):
                    album_tracks.extend(
                        entry for entry in raw_tracks if isinstance(entry, Mapping)
                    )
            album_artists = _api_names(result.get("artists"))
            return (
                album_tracks[:limit],
                album_artists[0] if album_artists else None,
            )

        if kind == "playlist":
            login, playlist_kind = values
            url = (
                f"{_YANDEX_API}/users/{quote(login, safe='')}/playlists/"
                f"{quote(playlist_kind, safe='')}"
            )
        else:
            url = f"{_YANDEX_API}/playlist/{quote(values[0], safe='')}"
        data = self._fetch_api_json(url, ctx=ctx)
        if data is None:
            return [], None
        result = data.get("result")
        if not isinstance(result, Mapping):
            result = data
        raw_tracks = result.get("tracks")
        if not isinstance(raw_tracks, list):
            return [], None
        playlist_tracks: list[Mapping[str, Any]] = []
        for entry in raw_tracks:
            if not isinstance(entry, Mapping):
                continue
            wrapped = entry.get("track")
            playlist_tracks.append(
                wrapped if isinstance(wrapped, Mapping) else entry
            )
        return (
            playlist_tracks[:limit],
            _api_owner_name(result.get("owner")),
        )

    def _fallback_playlist_source(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None,
        limit: int | None,
    ) -> SourceInfo | None:
        """Resolve a playlist type the yt-dlp plugin does not recognise.

        Yandex uses UUID prefixes the plugin regex does not know yet (for
        example ``ar.``).  The public playlist API still accepts the full UUID
        as ``/playlist/<uuid>``, so build a flat collection from it.
        """
        del flat
        target = _flat_api_target(ref.url)
        if target is None or target[0] != "shared_playlist":
            return None
        playlist_uuid = target[1][0]
        data = self._fetch_api_json(
            f"{_YANDEX_API}/playlist/{quote(playlist_uuid, safe='')}",
            ctx=ctx,
        )
        if data is None:
            return None
        result = data.get("result")
        if not isinstance(result, Mapping):
            result = data
        raw_tracks = result.get("tracks")
        if not isinstance(raw_tracks, list):
            return None

        effective_limit = effective_flat_limit(limit, ctx.profile)
        items: list[MediaItem] = []
        for position, entry in enumerate(raw_tracks[:effective_limit], start=1):
            if not isinstance(entry, Mapping):
                continue
            wrapped = entry.get("track")
            track = wrapped if isinstance(wrapped, Mapping) else entry
            track_id = track.get("id")
            if track_id is None:
                continue
            album_id = None
            albums = track.get("albums")
            if (
                isinstance(albums, list)
                and albums
                and isinstance(albums[0], Mapping)
            ):
                album_id = albums[0].get("id")
            page_url = (
                f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
                if album_id is not None
                else f"https://music.yandex.ru/track/{track_id}"
            )
            base = MediaItem(
                id=str(track_id),
                title=str(track.get("title") or track_id),
                page_url=page_url,
                position=position,
                resolution=ResolutionState.FLAT,
            )
            items.append(_apply_api_track(base, track))
        if not items:
            return None

        cover = _api_cover(result.get("coverUri") or result.get("ogImage"))
        description = result.get("description")
        source = SourceInfo(
            source="yandex_music",
            url=ref.url,
            kind=SourceKind.COLLECTION,
            id=playlist_uuid.split(".", 1)[-1],
            title=str(result.get("title") or ref.url),
            creator=_api_owner_name(result.get("owner")),
            description=str(description) if description else None,
            thumbnails=(cover,) if cover is not None else (),
            thumbnail=cover,
            items=tuple(items),
        )
        source = normalize_yandex_titles(source)
        source = _normalize_source_covers(source)
        return warn_on_smart_preview(source)

    def _enrich_flat_collection(
        self,
        source: SourceInfo,
        *,
        ref: SourceRef,
        ctx: LoaderContext,
        limit: int | None,
    ) -> SourceInfo:
        """Fill Yandex flat items from the public API, without full resolve."""
        target = _flat_api_target(ref.url)
        if target is None:
            return source
        effective_limit = effective_flat_limit(limit, ctx.profile)
        tracks, api_creator = self._fetch_flat_tracks(
            target,
            ctx=ctx,
            limit=effective_limit,
        )
        if not tracks:
            return source
        by_id = {
            str(track.get("id")): track
            for track in tracks
            if track.get("id") is not None
        }
        items: list[MediaItem] = []
        for item in source.items:
            track_id = _track_id_from_url(item.page_url)
            if track_id is None and item.id.isdigit():
                track_id = item.id
            track = by_id.get(track_id) if track_id is not None else None
            items.append(_apply_api_track(item, track) if track is not None else item)
        kind = target[0]
        if source.creator:
            creator = source.creator
        elif kind == "artist":
            # Every track on an artist page belongs to that artist.
            creator = source.title or next(
                (item.creator for item in items if item.creator),
                None,
            )
        elif kind == "album":
            creator = api_creator or next(
                (
                    item.album_artists[0]
                    for item in items
                    if item.album_artists
                ),
                None,
            )
        else:
            # A playlist owner is not the first track's artist; only trust
            # the API owner field here.
            creator = api_creator
        thumbnail = source.thumbnail or next(
            (item.thumbnail for item in items if item.thumbnail is not None),
            None,
        )
        return replace(
            source,
            creator=creator,
            items=tuple(items),
            thumbnail=thumbnail,
        )

    def _info_warnings(self, info: Mapping[str, Any]) -> tuple[str, ...]:
        """Surface the plugin's own ``smart_preview`` warnings.

        yt-dlp's messages are the only reliable signal when the track's
        metadata duration is the full length but the served stream is a
        short preview.  Everything else is extractor chatter and is ignored.
        """
        raw = info.get(INFO_WARNINGS_KEY)
        if not isinstance(raw, (list, tuple)):
            return ()
        warnings: list[str] = []
        for message in raw:
            text = str(message).strip()
            lowered = text.lower()
            if not text:
                continue
            if "smart_preview" not in lowered and "served stream is ~" not in lowered:
                continue
            warning = f"PreviewOnly: Yandex Music reported a preview stream ({text})"
            if warning not in warnings:
                warnings.append(warning)
        return tuple(warnings)

    def thumbnail_candidates(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> tuple[Thumbnail, ...]:
        """Return normalized covers plus a potential ``/orig`` master.

        No URL is probed here.  A full inspect tries the ordered candidates
        and stops at the first working one.
        """
        del ctx
        candidates: list[Thumbnail] = []
        seen: set[str] = set()
        for thumbnail in (*item.thumbnails, item.thumbnail):
            if thumbnail is None:
                continue
            normalized = _normalize_cover(thumbnail)
            if normalized.url in seen:
                continue
            seen.add(normalized.url)
            candidates.append(normalized)

        ranked = list(rank_thumbnails(candidates))
        if (
            item.position in (None, 1)
            and not any((thumbnail.width or 0) >= 2000 for thumbnail in ranked)
        ):
            for candidate in ranked:
                original_url = _orig_cover_url(candidate.url)
                if original_url is None or original_url in seen:
                    continue
                seen.add(original_url)
                return (
                    Thumbnail(url=original_url, source="yandex:orig"),
                    *ranked,
                )
        return tuple(ranked)

    def recommend(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Recommendations:
        """Recommend the best available audio: FLAC, else the best MP3.

        Yandex serves lossless only when the account/album allows it, so this
        is a preference, not a promise.  ``best_variant`` falls back to the
        highest-bitrate MP3 (320 before 192) when FLAC is absent.
        """
        del ctx
        audio = best_variant(
            item.variants,
            kinds=(MediaKind.AUDIO,),
            prefer_codec="flac",
        )
        return Recommendations(audio=audio.id if audio is not None else None)

    def _validate_downloaded(
        self,
        item_plan: ItemPlan,
        path: Path,
        *,
        ctx: LoaderContext,
    ) -> None:
        """Reject a short ``smart_preview`` even when download "succeeded"."""
        item = item_plan.item
        expected = item.duration
        quality = item.extras.get("quality")
        reason: str | None = None
        if isinstance(quality, str) and quality.strip().lower() in _PREVIEW_QUALITIES:
            reason = f"quality={quality.strip().lower()!r}"
        else:
            actual = probe_media_duration(
                path,
                timeout=ctx.profile.socket_timeout,
            )
            if looks_like_preview(actual, expected):
                if expected is not None:
                    reason = (
                        f"downloaded duration is {actual:.1f}s, "
                        f"expected {expected:.1f}s"
                    )
                else:
                    reason = f"downloaded duration is {actual:.1f}s"
        if reason is None:
            return
        raise PreviewOnly(
            f"PreviewOnly: item {item.id!r} looks like a Yandex preview "
            f"({reason}); refresh the session cookies or try another account",
            hint="refresh the Yandex session cookies and download again",
        )


__all__ = [
    "YandexMusicLoader",
    "normalize_yandex_titles",
    "warn_on_smart_preview",
]
