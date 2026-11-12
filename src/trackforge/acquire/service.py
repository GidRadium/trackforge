"""High-level acquisition service.

The service owns loader selection and context creation.  ``inspect`` is the
public L1 entry point: it resolves a URL, returns a normalized
:class:`~trackforge.acquire.models.SourceInfo` and never downloads media or
asks the user a question.  JSON rendering lives in
:mod:`trackforge.acquire.serialization`.
"""

# pyright: standard

from __future__ import annotations

import logging
import uuid

from .config import AppConfig
from .loaders.base import LoaderContext
from .models import (
    DownloadRequest,
    DownloadResult,
    SourceInfo,
    SourceRef,
    Thumbnail,
    ThumbnailAsset,
)
from .progress import CancellationToken, NullProgressSink, ProgressSink
from .registry import LoaderRegistry, ResolvedLoader, default_registry
from .staging import StagingArea
from .thumbnail_cache import ThumbnailCache

_LOGGER = logging.getLogger("trackforge.acquire")


class AcquisitionService:
    """Resolve a URL through the registry and inspect its metadata."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        registry: LoaderRegistry | None = None,
        http: object | None = None,
        logger: logging.Logger | None = None,
        thumbnail_cache: ThumbnailCache | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.registry = registry or default_registry(self.config)
        self.http = http
        self.logger = logger or _LOGGER
        self.thumbnail_cache = (
            thumbnail_cache
            if thumbnail_cache is not None
            else ThumbnailCache()
        )

    def inspect(
        self,
        url: str,
        *,
        flat: bool = False,
        limit: int | None = None,
        source: str | None = None,
    ) -> SourceInfo:
        """Return normalized source metadata without downloading media.

        The registry chooses the loader and its merged profile; the caller
        never has to know which source a URL belongs to.  Errors are already
        typed :class:`~trackforge.acquire.errors.AcquireError` instances and
        propagate unchanged.  ``limit`` is only meaningful for collections.
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")
        resolved = self.registry.resolve(url, source=source)
        ctx = self.context_for(resolved)
        ref = SourceRef(source=resolved.loader.name, url=url)
        return resolved.loader.get_info(ref, ctx=ctx, flat=flat, limit=limit)

    def inspect_json(
        self,
        url: str,
        *,
        flat: bool = False,
        limit: int | None = None,
        source: str | None = None,
        indent: int | None = 2,
    ) -> str:
        """Inspect and return the frozen schema-version-2 JSON text."""
        from .serialization import source_info_to_json

        return source_info_to_json(
            self.inspect(url, flat=flat, limit=limit, source=source),
            indent=indent,
        )

    def fetch_thumbnail(
        self,
        thumbnail: Thumbnail,
        *,
        url: str | None = None,
        source: str | None = None,
        max_bytes: int | None = None,
    ) -> ThumbnailAsset | None:
        """Return cached thumbnail bytes, fetching them when necessary.

        After ``inspect`` the chosen thumbnail is normally already cached, so
        bots/GUIs can call this without a network request.  ``url``/``source``
        are only needed when the cache is cold.
        """
        cached = self.thumbnail_cache.get(thumbnail.url)
        if cached is not None:
            return cached
        if url is None:
            return None
        resolved = self.registry.resolve(url, source=source)
        ctx = self.context_for(resolved)
        asset = resolved.loader.fetch_thumbnail(
            thumbnail,
            ctx=ctx,
            max_bytes=max_bytes,
        )
        if asset is not None:
            self.thumbnail_cache.put(thumbnail.url, asset)
        return asset

    def context_for(
        self,
        resolved: ResolvedLoader,
        *,
        job_id: str | None = None,
        progress: ProgressSink | None = None,
        cancel: CancellationToken | None = None,
        staging: StagingArea | None = None,
    ) -> LoaderContext:
        """Build a context for an already resolved loader."""
        return LoaderContext(
            job_id=job_id or uuid.uuid4().hex,
            profile=resolved.profile,
            progress=progress or NullProgressSink(),
            cancel=cancel or CancellationToken(),
            staging=staging,
            http=self.http,
            logger=self.logger,
            thumbnail_cache=self.thumbnail_cache,
        )

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        cancel: CancellationToken | None = None,
        staging: StagingArea | None = None,
        job_id: str | None = None,
    ) -> DownloadResult:
        """Download the selected variants into staging.

        The request already contains a frozen ``SourceInfo`` and explicit
        selections, so no user interaction happens here.  ``target_dir`` is
        used as the staging root unless a :class:`StagingArea` is injected;
        L2 never commits files to their final ingest path.
        """
        resolved = self.registry.resolve(
            request.source_info.url,
            source=request.source_info.source,
        )
        ctx = self.context_for(
            resolved,
            job_id=job_id,
            progress=progress,
            cancel=cancel,
            staging=staging or StagingArea(request.options.target_dir),
        )
        return resolved.loader.download(request, ctx=ctx)


__all__ = ["AcquisitionService"]
