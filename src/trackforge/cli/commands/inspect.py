"""Temporary ``tfg inspect`` command for poking at acquisition metadata.

This is a debug surface for L1.1; the public, frozen ``inspect`` CLI arrives
in L1.5.  It intentionally exposes both the normalized ``SourceInfo`` and the
raw sanitized yt-dlp ``info_dict``.
"""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from textwrap import wrap

import typer

from trackforge.acquire.config import SourceProfile, load_config
from trackforge.acquire.errors import AcquireError
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.models import (
    DownloadVariant,
    LiveStatus,
    MediaItem,
    MediaKind,
    Recommendations,
    SourceInfo,
    SourceRef,
    StreamInfo,
    StreamRole,
    SubtitleTrack,
    Thumbnail,
    UrlHint,
)
from trackforge.acquire.progress import NullProgressSink
from trackforge.acquire.registry import default_registry

_LOGGER = logging.getLogger("trackforge.acquire")


def inspect_url(
    urls: list[str] = typer.Argument(  # noqa: B008
        ...,
        help="One or more http(s) URLs to inspect.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Force a loader by name or profile key (youtube, yt_dlp, yt-dlp).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        help="Explicit config file; user config still has lower precedence.",
    ),
    cookies: Path | None = typer.Option(  # noqa: B008
        None,
        "--cookies",
        help="One-shot cookies file override for the selected/YouTube profile.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="One-shot proxy override (URL, 'env' or 'none').",
    ),
    player_client: str | None = typer.Option(
        None,
        "--player-client",
        help=(
            "Override YouTube player clients for this run "
            "(comma-separated; 'default' lets yt-dlp choose)."
        ),
    ),
    no_extractor_args: bool = typer.Option(
        False,
        "--no-extractor-args",
        help="Ignore configured extractor_args for this run.",
    ),
    flat: bool | None = typer.Option(
        None,
        "--flat/--no-flat",
        help=(
            "Flat collection listing without item formats. "
            "Defaults to flat for playlists/collections."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum items for a flat collection listing (default 50).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print normalized SourceInfo as JSON.",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print the sanitized raw yt-dlp info_dict instead (implies JSON).",
    ),
    show_variants: bool = typer.Option(
        True,
        "--variants/--no-variants",
        help="Show the variant summary in human output.",
    ),
    flat_variants: bool = typer.Option(
        False,
        "--flat-variants",
        help="Show every variant combination instead of grouped choices.",
    ),
    show_subtitles: bool = typer.Option(
        False,
        "--subtitles/--no-subtitles",
        help="Show subtitle language summary (manual and automatic).",
    ),
    all_subtitles: bool = typer.Option(
        False,
        "--all-subtitles",
        help="Show every subtitle language/format track (can be huge).",
    ),
    show_thumbnails: bool = typer.Option(
        False,
        "--thumbnails/--no-thumbnails",
        help="Show every thumbnail in human output.",
    ),
    show_extras: bool = typer.Option(
        False,
        "--extras/--no-extras",
        help="Show per-item extras as JSON in human output.",
    ),
) -> None:
    """Inspect URL metadata without downloading media (TEMPORARY L1.1 command)."""
    overrides = _overrides(cookies=cookies, proxy=proxy, source=source)
    try:
        config = load_config(config_path, overrides=overrides or None)
    except (AcquireError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    registry = default_registry(config)
    results: list[tuple[str, object]] = []
    failures: list[tuple[str, str]] = []
    failed = False

    for index, url in enumerate(urls, start=1):
        try:
            resolved = registry.resolve(url, source=source)
            run_profile = _profile_with_client_override(
                resolved.profile,
                player_client=player_client,
                no_extractor_args=no_extractor_args,
            )
            ctx = LoaderContext(
                job_id=uuid.uuid4().hex,
                profile=run_profile,
                progress=NullProgressSink(),
                logger=_LOGGER,
            )
            ref = SourceRef(resolved.loader.name, url)
            run_flat = _resolve_flat(
                flat,
                resolved.loader.classify_url(url),
            )
            if raw:
                preflight = getattr(resolved.loader, "preflight", None)
                if callable(preflight):
                    preflight(url, run_profile)
                extract = getattr(resolved.loader, "_extract", None)
                if not callable(extract):
                    raise AcquireError(
                        f"--raw is only supported for yt-dlp based loaders; "
                        f"got {resolved.loader.name!r}"
                    )
                extracted = extract(url, ctx=ctx, flat=run_flat, limit=limit)
                results.append((url, extracted))
            else:
                source_info = resolved.loader.get_info(
                    ref,
                    ctx=ctx,
                    flat=run_flat,
                    limit=limit,
                )
                results.append((url, source_info))
                if not json_output:
                    if len(urls) > 1:
                        typer.echo(f"\n=== {index}/{len(urls)}: {url} ===")
                    _render_source(
                        source_info,
                        show_variants=show_variants,
                        flat_variants=flat_variants,
                        show_subtitles=show_subtitles or all_subtitles,
                        all_subtitles=all_subtitles,
                        show_thumbnails=show_thumbnails,
                        show_extras=show_extras,
                    )
        except BrokenPipeError:
            raise typer.Exit(code=0) from None
        except (AcquireError, OSError, ValueError) as exc:
            failed = True
            message = f"{type(exc).__name__}: {exc}"
            failures.append((url, message))
            typer.echo(f"error [{url}]: {message}", err=True)
            hint = getattr(exc, "hint", None)
            if isinstance(hint, str) and hint:
                typer.echo(f"hint: {hint}", err=True)

    if json_output or raw:
        if len(urls) == 1 and results:
            typer.echo(_json_dumps(results[0][1]))
        elif len(urls) > 1:
            json_payload: list[dict[str, object]] = [
                {"url": url, "ok": True, "data": _jsonable(value)}
                for url, value in results
            ]
            json_payload.extend(
                {"url": url, "ok": False, "error": message}
                for url, message in failures
            )
            typer.echo(json.dumps(json_payload, ensure_ascii=False, indent=2))

    if failed:
        raise typer.Exit(code=1)


def _resolve_flat(flat: bool | None, hint: UrlHint) -> bool:
    """Honour an explicit --flat/--no-flat, otherwise auto-flat collections.

    Running a full playlist extraction by accident resolves every entry and
    issues one YouTube request per item.  A collection URL should therefore
    start in the cheap flat mode unless the user explicitly asks otherwise.
    """
    if flat is not None:
        return flat
    return hint is UrlHint.COLLECTION


def _profile_with_client_override(
    profile: SourceProfile,
    *,
    player_client: str | None,
    no_extractor_args: bool,
) -> SourceProfile:
    """Apply a one-shot player-client/extractor-args override."""
    if no_extractor_args:
        return replace(profile, extractor_args={})
    if player_client is None:
        return profile

    values = [
        item
        for item in re.split(r"[,;\s]+", player_client.strip())
        if item
    ]
    if len(values) == 1 and values[0].lower() in {"default", "auto"}:
        values = []

    extractor_args: dict[str, dict[str, tuple[str, ...]]] = {
        extractor: {key: tuple(val) for key, val in values_by_key.items()}
        for extractor, values_by_key in profile.extractor_args.items()
    }
    youtube_args = dict(extractor_args.get("youtube", {}))
    if values:
        youtube_args["player_client"] = tuple(values)
    else:
        youtube_args.pop("player_client", None)
    if youtube_args:
        extractor_args["youtube"] = youtube_args
    else:
        extractor_args.pop("youtube", None)
    return replace(profile, extractor_args=extractor_args)


def _overrides(
    *,
    cookies: Path | None,
    proxy: str | None,
    source: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if cookies is not None:
        payload["cookies"] = str(cookies)
    if proxy is not None:
        payload["proxy"] = proxy
    if not payload:
        return {}
    sources: dict[str, object] = {"default": dict(payload)}
    profile_key = source or "youtube"
    if profile_key in {"yt-dlp", "yt_dlp"}:
        profile_key = "yt_dlp"
    sources[profile_key] = dict(payload)
    return {"sources": sources}


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_dumps(value: object) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, indent=2)


def _render_source(
    source: SourceInfo,
    *,
    show_variants: bool,
    flat_variants: bool,
    show_subtitles: bool,
    all_subtitles: bool,
    show_thumbnails: bool,
    show_extras: bool,
) -> None:
    typer.echo(f"Source:        {source.source}")
    typer.echo(f"Kind:          {source.kind.value}")
    typer.echo(f"ID:            {_dash(source.id)}")
    typer.echo(f"Title:         {_dash(source.title)}")
    typer.echo(f"Creator:       {_dash(source.creator)}")
    typer.echo(f"Publisher:     {_dash(source.publisher)}")
    typer.echo(f"URL:           {source.url}")
    typer.echo(f"Description:   {_short(source.description)}")
    typer.echo(f"Thumbnail:     {_thumbnail(source.thumbnail)}")
    if show_thumbnails:
        for thumbnail in source.thumbnails:
            typer.echo(f"  thumbnail:   {_thumbnail(thumbnail)}")
    else:
        typer.echo(f"Thumbnails:    {len(source.thumbnails)} (--thumbnails)")

    for item in source.items:
        typer.echo("")
        _render_item(
            item,
            show_variants=show_variants,
            flat_variants=flat_variants,
            show_subtitles=show_subtitles,
            all_subtitles=all_subtitles,
            show_extras=show_extras,
        )

    for warning in source.warnings:
        typer.echo(f"Warning:       {warning}")


def _render_item(
    item: MediaItem,
    *,
    show_variants: bool,
    flat_variants: bool,
    show_subtitles: bool,
    all_subtitles: bool,
    show_extras: bool,
) -> None:
    position = item.position if item.position is not None else "?"
    typer.echo(f"Item {position}:       {item.id}")
    typer.echo(f"  kind:          {item.kind.value}")
    typer.echo(f"  resolution:    {item.resolution.value}")
    typer.echo(f"  live_status:   {item.live_status.value}")
    if item.live_status is LiveStatus.LIVE:
        typer.echo("  live:          LIVE STREAM")
    typer.echo(f"  title:         {_dash(item.title)}")
    typer.echo(f"  creator:       {_dash(item.creator)}")
    typer.echo(f"  artists:       {_join(item.artists)}")
    typer.echo(f"  album:         {_dash(item.album)}")
    typer.echo(f"  album artists: {_join(item.album_artists)}")
    typer.echo(f"  duration:      {_duration(item.duration)}")
    typer.echo(f"  date:          {_dash(str(item.release_date) if item.release_date else None)}")
    typer.echo(f"  page_url:      {_dash(item.page_url)}")
    typer.echo(f"  stem:          {_dash(item.suggested_stem)}")
    typer.echo(f"  thumbnail:     {_thumbnail(item.thumbnail)}")
    typer.echo(f"  thumbnails:    {len(item.thumbnails)}")
    typer.echo(f"  recommendations: {_recommendations(item.recommendations)}")
    _render_subtitles(
        item.subtitles,
        show_languages=show_subtitles,
        show_all=all_subtitles,
    )
    if show_extras:
        typer.echo(
            "  extras:        "
            + json.dumps(_jsonable(item.extras), ensure_ascii=False)
        )

    if not show_variants:
        return
    if flat_variants:
        _render_flat_variants(item.variants)
    else:
        _render_variant_groups(item.variants)


def _render_subtitles(
    tracks: Sequence[SubtitleTrack],
    *,
    show_languages: bool,
    show_all: bool,
) -> None:
    manual = [track for track in tracks if not track.is_automatic]
    automatic = [track for track in tracks if track.is_automatic]
    manual_languages = sorted({track.language for track in manual})
    automatic_languages = sorted({track.language for track in automatic})
    typer.echo(
        f"  subtitles:     {len(tracks)} tracks "
        f"(manual {len(manual)} tracks / {len(manual_languages)} languages; "
        f"automatic {len(automatic)} tracks / "
        f"{len(automatic_languages)} languages)"
    )
    if show_all:
        for track in tracks:
            kind = "auto" if track.is_automatic else "manual"
            name = f" — {track.name}" if track.name else ""
            typer.echo(
                f"    {track.language:<10} {_dash(track.ext):<6} {kind:<6}{name}"
            )
        return
    if not show_languages:
        return
    for label, languages in (
        ("manual", manual_languages),
        ("automatic", automatic_languages),
    ):
        if not languages:
            continue
        lines = wrap(", ".join(languages), width=88) or [""]
        typer.echo(
            f"    {label} languages ({len(languages)}): {lines[0]}"
        )
        for line in lines[1:]:
            typer.echo(f"      {line}")


def _render_flat_variants(variants: Sequence[DownloadVariant]) -> None:
    typer.echo(f"  variants:      {len(variants)}")
    for index, variant in enumerate(variants, start=1):
        status = "downloadable" if variant.downloadable else "NOT downloadable"
        typer.echo(
            f"    [{index:>2}] {variant.id:<22} {variant.kind.value:<11} "
            f"{_dash(variant.container):<7} {variant.size_label:>9}  {status}"
        )
        typer.echo(
            f"         label: {variant.label}  merge={variant.requires_merge} "
            f"ffmpeg={variant.requires_ffmpeg}"
        )
        if variant.unavailable_reason:
            typer.echo(f"         reason: {variant.unavailable_reason}")
        typer.echo(f"         selector: {_dash(variant.selector)}")
        for stream in variant.streams:
            typer.echo(f"         stream: {_stream(stream)}")


def _render_variant_groups(variants: Sequence[DownloadVariant]) -> None:
    muxed = [
        variant
        for variant in variants
        if variant.kind is MediaKind.AUDIO_VIDEO
        and not variant.requires_merge
    ]
    merged = [
        variant
        for variant in variants
        if variant.kind is MediaKind.AUDIO_VIDEO
        and variant.requires_merge
    ]
    video_only = [
        variant for variant in variants if variant.kind is MediaKind.VIDEO
    ]
    audio = [
        variant for variant in variants if variant.kind is MediaKind.AUDIO
    ]
    typer.echo(
        f"  variants:      {len(variants)} total — "
        f"muxed {len(muxed)}, video {len(merged) + len(video_only)}, "
        f"audio {len(audio)}"
    )
    if muxed:
        typer.echo("    muxed by source (no merge):")
        for variant in muxed:
            typer.echo(
                f"      {variant.id:<24} {variant.label:<26} "
                f"{variant.size_label:>9}  {_variant_language(variant)}"
            )
    if merged:
        typer.echo("    video choices (video+audio, merge):")
        groups: dict[tuple[int, float, str, str, str], list[DownloadVariant]] = {}
        for variant in merged:
            video = _variant_stream(variant, StreamRole.VIDEO)
            key = (
                int(video.height) if video and video.height else 0,
                float(video.fps) if video and video.fps else 0.0,
                (variant.container or "").lower(),
                _codec_label(video.codec if video else None),
                (video.dynamic_range or "").upper() if video else "",
            )
            groups.setdefault(key, []).append(variant)
        for key, group in sorted(
            groups.items(),
            key=lambda item: (
                -item[0][0],
                -item[0][1],
                item[0][2],
                item[0][3],
                item[0][4],
            ),
        ):
            height, fps, container, codec, dynamic_range = key
            resolution = f"{height}p" if height else "?"
            if fps:
                resolution = f"{resolution}{fps:g}"
            label = f"{resolution} {str(container).upper()} {codec}".strip()
            if dynamic_range and dynamic_range != "SDR":
                label = f"{label} {dynamic_range}"
            typer.echo(f"      {label}")
            for variant in group:
                audio_stream = _variant_stream(variant, StreamRole.AUDIO)
                language = _variant_language(variant)
                audio_bits = []
                if audio_stream is not None:
                    if audio_stream.ext:
                        audio_bits.append(audio_stream.ext.upper())
                    if audio_stream.bitrate_kbps:
                        audio_bits.append(f"{audio_stream.bitrate_kbps:g}k")
                audio_label = " ".join(audio_bits) or "audio"
                typer.echo(
                    f"        {language:<10} {variant.id:<24} "
                    f"{variant.size_label:>9}  {audio_label}"
                )
    if video_only:
        typer.echo("    video-only choices (no audio):")
        for variant in video_only:
            typer.echo(
                f"      {variant.id:<24} {variant.label:<26} "
                f"{variant.size_label:>9}"
            )
    if audio:
        typer.echo("    audio-only choices:")
        for variant in audio:
            audio_stream = _variant_stream(variant, StreamRole.AUDIO)
            codec = _codec_label(audio_stream.codec if audio_stream else None)
            bitrate = (
                f"{audio_stream.bitrate_kbps:g}k"
                if audio_stream is not None and audio_stream.bitrate_kbps
                else "?"
            )
            typer.echo(
                f"      {_variant_language(variant):<10} {variant.id:<24} "
                f"{variant.size_label:>9}  {codec} {bitrate}"
            )
    typer.echo("    (use --flat-variants for selectors and stream details)")


def _variant_stream(
    variant: DownloadVariant,
    role: StreamRole,
) -> StreamInfo | None:
    return next(
        (stream for stream in variant.streams if stream.role is role),
        None,
    )


def _variant_language(variant: DownloadVariant) -> str:
    audio = _variant_stream(variant, StreamRole.AUDIO)
    language = getattr(audio, "language", None)
    return str(language) if language else "—"


def _codec_label(codec: str | None) -> str:
    if not codec:
        return "?"
    stem = codec.split(".", 1)[0].strip().lower()
    return {
        "avc1": "H.264",
        "avc3": "H.264",
        "h264": "H.264",
        "hev1": "H.265",
        "hvc1": "H.265",
        "h265": "H.265",
        "vp9": "VP9",
        "vp09": "VP9",
        "av1": "AV1",
        "av01": "AV1",
        "mp4a": "AAC",
        "aac": "AAC",
        "opus": "Opus",
        "vorbis": "Vorbis",
    }.get(stem, stem.upper())


def _stream(stream: object) -> str:
    role = getattr(stream, "role", None)
    role_name = role.value if isinstance(role, Enum) else str(role)
    parts = [
        role_name,
        _dash(getattr(stream, "format_id", None)),
        _dash(getattr(stream, "codec", None)),
        _dash(getattr(stream, "ext", None)),
    ]
    height = getattr(stream, "height", None)
    if height:
        parts.append(f"{height}p")
    fps = getattr(stream, "fps", None)
    if fps:
        parts.append(f"{fps:g}fps")
    bitrate = getattr(stream, "bitrate_kbps", None)
    if bitrate:
        parts.append(f"{bitrate:g}k")
    sample_rate = getattr(stream, "sample_rate", None)
    if sample_rate:
        parts.append(f"{sample_rate}Hz")
    channels = getattr(stream, "channels", None)
    if channels:
        parts.append(f"{channels}ch")
    language = getattr(stream, "language", None)
    if language:
        parts.append(str(language))
    dynamic_range = getattr(stream, "dynamic_range", None)
    if dynamic_range and str(dynamic_range).upper() != "SDR":
        parts.append(str(dynamic_range))
    if getattr(stream, "drm", False):
        parts.append("DRM")
    size = getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", None)
    if size:
        parts.append(f"{size / 1_000_000:.1f}MB")
    return " ".join(parts)


def _thumbnail(thumbnail: Thumbnail | None) -> str:
    if thumbnail is None:
        return "—"
    dimensions = (
        f"{thumbnail.width}x{thumbnail.height}"
        if thumbnail.width and thumbnail.height
        else "?x?"
    )
    ext = thumbnail.ext or thumbnail.mime or "?"
    return f"{thumbnail.url} ({dimensions}, {ext})"


def _recommendations(recommendations: Recommendations) -> str:
    return (
        f"audio={_dash(recommendations.audio)} "
        f"video={_dash(recommendations.video)} "
        f"image={_dash(recommendations.image)} "
        f"document={_dash(recommendations.document)}"
    )


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d} ({seconds:g}s)"
    return f"{minutes}:{secs:02d} ({seconds:g}s)"


def _short(value: str | None, limit: int = 240) -> str:
    if not value:
        return "—"
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}…"


def _dash(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _join(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "—"


__all__ = ["inspect_url"]
