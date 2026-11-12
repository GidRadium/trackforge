"""Generic yt-dlp loader for single media items and collections.

The module keeps :mod:`yt_dlp` imports lazy so importing the acquisition
package stays offline.  ``get_info`` only extracts metadata
(``download=False``); downloading belongs to L2.  Collections are resolved in
two stages: a bounded flat listing first, then a full resolve of the selected
item by its ``page_url``.
"""

# pyright: standard

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ..auth import ensure_auth_ready
from ..config import SourceProfile
from ..download import (
    YtDlpProgressHook,
    build_postprocessors,
    collect_download_outputs,
    ensure_ffmpeg,
    ensure_ffprobe,
    requires_ffmpeg,
)
from ..errors import (
    AcquireError,
    AuthRequired,
    CancelledError,
    DownloadFailed,
    PostprocessError,
    PreviewOnly,
    Unavailable,
)
from ..models import (
    CollisionPolicy,
    DownloadedFile,
    DownloadFailure,
    DownloadRequest,
    DownloadResult,
    SourceInfo,
    SourceKind,
    SourceRef,
    Thumbnail,
)
from ..planner import DownloadPlan, ItemPlan, YtDlpPlanner
from ..progress import ProgressEvent, ProgressStage
from ..staging import StagingArea
from .base import LoaderCapabilities, LoaderContext, MediaLoader
from .ytdlp_errors import (
    map_ytdlp_error,
)
from .ytdlp_mapper import (
    DEFAULT_FLAT_LIMIT,
    MAX_FLAT_LIMIT,
    YtDlpMapper,
)
from .ytdlp_options import (
    build_ydl_options,
    create_youtube_dl,
    deno_js_runtimes,
)

_AUTH_AVAILABILITY = frozenset(
    {"private", "premium_only", "subscriber_only", "needs_auth", "members_only"}
)

# yt-dlp extractors (not only Yandex) can report important warnings through
# the logger instead of the info_dict.  They are collected during extraction
# and attached under a private key so site loaders can surface the relevant
# ones without exposing raw log output to users.
INFO_WARNINGS_KEY = "_tf_warnings"


class _WarningCollector(logging.Handler):
    """Collect formatted warning messages emitted during one extraction."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never break extraction
            return
        if message:
            self.messages.append(message)


def _configured_limit(profile: SourceProfile, key: str, default: int) -> int:
    """Read a positive integer limit override from ``SourceProfile.extra``."""
    value = profile.extra.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def effective_flat_limit(
    limit: int | None,
    profile: SourceProfile,
) -> int:
    """Return a bounded item limit for a flat collection extraction.

    ``None``, zero and negative values mean "use the configured default"
    rather than "fetch everything".  Both the default and the hard maximum can
    be overridden for a source through ``extra.flat_limit`` and
    ``extra.flat_limit_max``.
    """
    default = _configured_limit(profile, "flat_limit", DEFAULT_FLAT_LIMIT)
    maximum = _configured_limit(profile, "flat_limit_max", MAX_FLAT_LIMIT)
    requested = (
        limit
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        else default
    )
    return max(1, min(requested, maximum))


class YtDlpLoader(MediaLoader):
    """Fallback loader for any http(s) URL understood by yt-dlp."""

    name = "yt-dlp"
    config_key = "yt_dlp"
    priority = 1000
    capabilities = LoaderCapabilities(
        search=False,
        playlists=True,
        posts=True,
        live=True,
        subtitles=True,
        cookies=True,
        proxy=True,
    )

    def supports_url(self, url: str) -> bool:
        """Accept any absolute http(s) URL; yt-dlp's GenericIE decides later."""
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def preflight(self, url: str, profile: SourceProfile) -> None:
        """Apply the loader's auth policy before a network request."""
        if self.requires_auth(url):
            ensure_auth_ready(
                profile,
                source=self.name,
                cookies_required=True,
            )

    def _base_options(
        self,
        profile: SourceProfile,
        *,
        logger: logging.Logger | None = None,
        cookiefile: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return options suitable for a captured/mock ``YoutubeDL``."""
        return build_ydl_options(
            profile,
            cookiefile=cookiefile,
            logger=logger,
            env=env,
        )

    @staticmethod
    def _create_youtube_dl(options: Mapping[str, Any]) -> Any:
        return create_youtube_dl(options)

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        """Extract metadata only; never download media files.

        ``flat=True`` asks yt-dlp for a bounded list of collection entries
        without resolving their formats.  ``flat=None`` is auto mode: a
        collection is listed flat, while a single item keeps its formats.
        A full resolve is intended for one selected item's ``page_url``;
        ``noplaylist`` keeps a ``watch?v&list`` URL from expanding into the
        whole playlist.
        """
        self.preflight(ref.url, ctx.profile)
        effective_limit = effective_flat_limit(limit, ctx.profile)
        info = self._extract(
            ref.url,
            ctx=ctx,
            flat=flat,
            limit=effective_limit,
        )
        source = YtDlpMapper(ctx.profile).map(
            info,
            ref,
            flat=flat,
            limit=effective_limit,
        )
        source = self._finalize(source, ctx)
        source = self._validate_collection(source, info)
        warnings = self._info_warnings(info)
        if warnings:
            source = replace(source, warnings=source.warnings + warnings)
        return source

    def _info_warnings(self, info: Mapping[str, Any]) -> tuple[str, ...]:
        """Turn collected yt-dlp warnings into source warnings.

        The generic loader ignores extractor chatter; site-specific loaders
        override this for warnings that carry real user-facing meaning.
        """
        del info
        return ()

    def _validate_collection(
        self,
        source: SourceInfo,
        info: Mapping[str, Any],
    ) -> SourceInfo:
        """Turn an empty/private collection into a typed acquisition error."""
        if source.kind is not SourceKind.COLLECTION or source.items:
            return source
        availability = str(info.get("availability") or "").strip().lower()
        if availability in _AUTH_AVAILABILITY:
            raise AuthRequired(
                f"collection is not accessible ({availability}): {source.url}",
            )
        raise Unavailable(
            f"collection is empty or has no accessible items: {source.url}",
        )

    def _extract(
        self,
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> Mapping[str, Any]:
        """Run ``extract_info(download=False)`` and sanitize the payload."""
        effective_limit = effective_flat_limit(limit, ctx.profile)
        options = self._base_options(ctx.profile, logger=ctx.logger)
        options.update(
            {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "ignoreerrors": False,
                "cachedir": False,
                # ``None`` is auto mode: ask yt-dlp to flatten collection
                # entries, but let the mapper keep a single item fully
                # resolved (the top-level video still carries its formats).
                "extract_flat": False if flat is False else "in_playlist",
                "playlist_items": f"1:{effective_limit}",
                # An explicit full resolve targets one item; never expand its
                # playlist.  Flat/auto must allow the playlist extractor to
                # return its entries instead of only the first one.
                "noplaylist": flat is False,
                "remote_components": [],
            }
        )
        js_runtimes = deno_js_runtimes()
        if js_runtimes:
            options["js_runtimes"] = js_runtimes

        from yt_dlp.utils import DownloadError, ExtractorError

        collector = _WarningCollector()
        ctx.logger.addHandler(collector)
        try:
            ydl = self._create_youtube_dl(options)
            try:
                if hasattr(ydl, "__enter__") and hasattr(ydl, "__exit__"):
                    with ydl as active:
                        info = active.extract_info(url, download=False)
                        sanitize = getattr(active, "sanitize_info", None)
                        if callable(sanitize):
                            info = sanitize(info)
                else:
                    try:
                        info = ydl.extract_info(url, download=False)
                        sanitize = getattr(ydl, "sanitize_info", None)
                        if callable(sanitize):
                            info = sanitize(info)
                    finally:
                        close = getattr(ydl, "close", None)
                        if callable(close):
                            close()
            except (DownloadError, ExtractorError) as exc:
                raise map_ytdlp_error(exc, url=url) from exc
        finally:
            ctx.logger.removeHandler(collector)

        if info is None:
            raise map_ytdlp_error(
                DownloadError("yt-dlp returned no data"),
                url=url,
            )
        if not isinstance(info, Mapping):
            raise map_ytdlp_error(
                DownloadError("yt-dlp returned an unexpected result"),
                url=url,
            )
        if collector.messages:
            info = dict(info)
            info[INFO_WARNINGS_KEY] = tuple(collector.messages)
        return info

    def _finalize(self, source: SourceInfo, ctx: LoaderContext) -> SourceInfo:
        """Apply source-specific thumbnail and recommendation policy."""
        items = []
        for item in source.items:
            candidates = self.thumbnail_candidates(item, ctx=ctx)
            chosen = self.choose_thumbnail(item, ctx=ctx)
            if chosen is not None and not any(
                candidate.url == chosen.url for candidate in candidates
            ):
                # A loader override may choose a candidate outside the generic
                # list (for example Yandex's verified ``/orig`` cover).
                candidates = (chosen, *candidates)
            items.append(
                replace(
                    item,
                    thumbnails=candidates,
                    thumbnail=chosen or item.thumbnail,
                    recommendations=self.recommend(item, ctx=ctx),
                )
            )
        items_tuple = tuple(items)
        thumbnail = next(
            (item.thumbnail for item in items_tuple if item.thumbnail is not None),
            None,
        ) or source.thumbnail
        return replace(source, items=items_tuple, thumbnail=thumbnail)

    # Yandex overrides this to verify that the served file is not a preview.
    requires_ffprobe_for_download = False

    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult:
        """Download every selected variant into this job's staging directory.

        Items are processed sequentially.  A failure in one item is recorded
        in :attr:`DownloadResult.failures` and does not stop the remaining
        items.  Cancellation is different: it aborts the whole job and
        removes the staging directory unless ``keep_temp`` is set.
        """
        plan = YtDlpPlanner(request, job_id=ctx.job_id).plan()
        if not plan.items:
            return DownloadResult(files=())
        ctx.cancel.raise_if_cancelled()
        for item_plan in plan.items:
            self.preflight(
                item_plan.item.page_url or plan.source.url,
                ctx.profile,
            )

        if any(requires_ffmpeg(item_plan, plan.options) for item_plan in plan.items):
            ensure_ffmpeg()
        if self.requires_ffprobe_for_download:
            ensure_ffprobe()

        staging = ctx.staging or StagingArea(plan.options.target_dir)
        job_dir = staging.job_dir(plan.job_id)
        files: list[DownloadedFile] = []
        failures: list[DownloadFailure] = []
        try:
            for index, item_plan in enumerate(plan.items, start=1):
                ctx.cancel.raise_if_cancelled()
                try:
                    downloaded = self._download_item(
                        plan,
                        item_plan,
                        item_index=index,
                        ctx=ctx,
                        job_dir=job_dir,
                    )
                except CancelledError:
                    raise
                except PreviewOnly as exc:
                    failures.append(
                        DownloadFailure(
                            item_id=item_plan.item.id,
                            message=str(exc),
                            retryable=exc.retryable,
                        )
                    )
                except AcquireError as exc:
                    failures.append(
                        DownloadFailure(
                            item_id=item_plan.item.id,
                            message=str(exc),
                            retryable=exc.retryable,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one item must not stop the rest
                    ctx.logger.warning(
                        "download failed for item %s: %s",
                        item_plan.item.id,
                        exc,
                    )
                    failures.append(
                        DownloadFailure(
                            item_id=item_plan.item.id,
                            message=str(exc) or type(exc).__name__,
                        )
                    )
                else:
                    files.append(downloaded)
                    ctx.progress.emit(
                        ProgressEvent(
                            stage=ProgressStage.DONE,
                            job_id=plan.job_id,
                            item_index=index,
                            item_count=len(plan.items),
                            filename=downloaded.path.name,
                        )
                    )
            return DownloadResult(files=tuple(files), failures=tuple(failures))
        except CancelledError:
            if not plan.options.keep_temp:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except BaseException:
            if not plan.options.keep_temp:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def _download_item(
        self,
        plan: DownloadPlan,
        item_plan: ItemPlan,
        *,
        item_index: int,
        ctx: LoaderContext,
        job_dir: Path,
    ) -> DownloadedFile:
        hook = YtDlpProgressHook(
            ctx.progress,
            ctx.cancel,
            plan,
            item_plan,
            item_index,
        )
        options = self._download_options(
            plan,
            item_plan,
            ctx=ctx,
            job_dir=job_dir,
            hook=hook,
        )
        relative = PurePosixPath(item_plan.output_stem)
        (job_dir / relative.parent).mkdir(parents=True, exist_ok=True)
        (job_dir / "tmp").mkdir(parents=True, exist_ok=True)

        url = item_plan.item.page_url or plan.source.url
        info = self._extract_download(url, options, ctx=ctx)
        ctx.cancel.raise_if_cancelled()
        media, sidecars = collect_download_outputs(item_plan, job_dir, info)
        try:
            self._validate_downloaded(item_plan, media, ctx=ctx)
        except PreviewOnly:
            if not plan.options.keep_temp:
                for path in (media, *sidecars):
                    path.unlink(missing_ok=True)
            raise

        warnings: list[str] = []
        if plan.options.write_thumbnail:
            sidecar_thumbnail = self.thumbnail_sidecar(
                item_plan.item,
                ctx=ctx,
            )
            if sidecar_thumbnail is not None:
                thumbnail_path, warning = self._write_thumbnail(
                    sidecar_thumbnail,
                    media,
                    ctx=ctx,
                )
                if thumbnail_path is not None:
                    sidecars = (*sidecars, thumbnail_path)
                if warning is not None:
                    warnings.append(warning)
        return DownloadedFile(
            item_id=item_plan.item.id,
            path=media,
            item=item_plan.item,
            sidecars=sidecars,
            warnings=tuple(warnings),
        )

    def _write_thumbnail(
        self,
        thumbnail: Thumbnail,
        media: Path,
        *,
        ctx: LoaderContext,
    ) -> tuple[Path | None, str | None]:
        asset = self.fetch_thumbnail(thumbnail, ctx=ctx)
        if asset is None:
            return None, "thumbnail is not available"
        extension = (asset.ext or thumbnail.ext or "jpg").strip().lower()
        if extension == "jpeg":
            extension = "jpg"
        target = media.with_suffix(f".{extension}")
        if target == media:
            target = media.parent / f"{media.stem}.thumb.{extension}"
        if target.exists():
            target = media.parent / f"{media.stem}.thumbnail.{extension}"
        target.write_bytes(asset.data)
        return target, None

    def _fetch_thumbnail_bytes(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int,
    ) -> bytes:
        """Fetch an image through yt-dlp's networking (cookies/proxy aware)."""
        from yt_dlp.networking import Request

        options = self._base_options(ctx.profile, logger=ctx.logger)
        options.update(
            {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
            }
        )
        ydl = self._create_youtube_dl(options)
        data: bytes = b""
        try:
            headers = {
                "Accept": (
                    "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.1"
                ),
                "Accept-Encoding": "identity",
            }
            headers.update(
                {
                    str(key): str(value)
                    for key, value in thumbnail.http_headers.items()
                }
            )
            response = ydl.urlopen(Request(thumbnail.url, headers=headers))
            try:
                data = response.read(max_bytes + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        finally:
            close = getattr(ydl, "close", None)
            if callable(close):
                close()
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise DownloadFailed("thumbnail response was empty")
        if len(data) > max_bytes:
            raise DownloadFailed("thumbnail exceeds the size limit")
        return bytes(data)

    def _download_options(
        self,
        plan: DownloadPlan,
        item_plan: ItemPlan,
        *,
        ctx: LoaderContext,
        job_dir: Path,
        hook: YtDlpProgressHook,
    ) -> dict[str, Any]:
        options = dict(self._base_options(ctx.profile, logger=ctx.logger))
        postprocessors = build_postprocessors(item_plan, plan.options)
        options.update(
            {
                "format": item_plan.selector,
                "paths": {
                    "home": str(job_dir),
                    "temp": str(job_dir / "tmp"),
                },
                "outtmpl": {"default": f"{item_plan.output_stem}.%(ext)s"},
                "noplaylist": True,
                "skip_download": False,
                "writethumbnail": (
                    plan.options.write_thumbnail
                    and self.thumbnail_sidecar(item_plan.item, ctx=ctx)
                    is None
                ),
                "writeinfojson": plan.options.write_info_json,
                "writesubtitles": bool(plan.options.subtitles),
                "subtitleslangs": list(plan.options.subtitles),
                "subtitlesformat": "best",
                "overwrites": plan.options.collision is CollisionPolicy.OVERWRITE,
                "continuedl": True,
                "keepvideo": plan.options.keep_temp,
                "postprocessors": postprocessors,
                "progress_hooks": [hook.download],
                "postprocessor_hooks": [hook.postprocessor],
            }
        )
        container = _normalise_container(plan.options.container)
        if container is not None and item_plan.variant.requires_merge:
            # ffmpeg merges into the requested container directly; adding a
            # separate remuxer afterwards would process the file twice.
            options["merge_output_format"] = container
        return options

    def _extract_download(
        self,
        url: str,
        options: Mapping[str, Any],
        *,
        ctx: LoaderContext,
    ) -> Mapping[str, Any]:
        from yt_dlp.utils import (
            DownloadCancelled as YtDlpDownloadCancelled,
        )
        from yt_dlp.utils import DownloadError, ExtractorError, PostProcessingError

        ydl = self._create_youtube_dl(options)
        try:
            try:
                if hasattr(ydl, "__enter__") and hasattr(ydl, "__exit__"):
                    with ydl as active:
                        info = active.extract_info(url, download=True)
                        sanitize = getattr(active, "sanitize_info", None)
                        if callable(sanitize):
                            info = sanitize(info)
                else:
                    try:
                        info = ydl.extract_info(url, download=True)
                        sanitize = getattr(ydl, "sanitize_info", None)
                        if callable(sanitize):
                            info = sanitize(info)
                    finally:
                        close = getattr(ydl, "close", None)
                        if callable(close):
                            close()
            except YtDlpDownloadCancelled as exc:
                raise CancelledError("download cancelled by user") from exc
            except PostProcessingError as exc:
                raise PostprocessError(
                    f"post-processing failed: {exc}",
                    hint="check that ffmpeg is installed and the file is valid",
                ) from exc
            except (DownloadError, ExtractorError) as exc:
                if ctx.cancel.cancelled:
                    raise CancelledError("download cancelled by user") from exc
                if _looks_like_postprocess_error(exc):
                    raise PostprocessError(
                        f"post-processing failed: {exc}",
                        hint="check that ffmpeg is installed and the file is valid",
                    ) from exc
                raise map_ytdlp_error(exc, url=url) from exc
        finally:
            close = getattr(ydl, "close", None)
            if callable(close):
                close()

        if info is None:
            raise DownloadFailed(
                "yt-dlp returned no data while downloading",
                hint="retry the download; refresh the item if it persists",
            )
        if not isinstance(info, Mapping):
            raise DownloadFailed(
                "yt-dlp returned an unexpected download result",
                hint="retry the download; refresh the item if it persists",
            )
        return info

    def _validate_downloaded(
        self,
        item_plan: ItemPlan,
        path: Path,
        *,
        ctx: LoaderContext,
    ) -> None:
        """Source-specific post-download verification hook."""
        del item_plan, path, ctx


def _normalise_container(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower().lstrip(".")
    return text or None


def _looks_like_postprocess_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "postprocessing" in text or "post-processing" in text
