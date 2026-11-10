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
from .models import SourceInfo, SourceRef
from .progress import NullProgressSink
from .registry import LoaderRegistry, ResolvedLoader, default_registry

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
    ) -> None:
        self.config = config or AppConfig()
        self.registry = registry or default_registry(self.config)
        self.http = http
        self.logger = logger or _LOGGER

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
        """Inspect and return the frozen schema-version-1 JSON text."""
        from .serialization import source_info_to_json

        return source_info_to_json(
            self.inspect(url, flat=flat, limit=limit, source=source),
            indent=indent,
        )

    def context_for(self, resolved: ResolvedLoader) -> LoaderContext:
        """Build a context for an already resolved loader."""
        return LoaderContext(
            job_id=uuid.uuid4().hex,
            profile=resolved.profile,
            progress=NullProgressSink(),
            http=self.http,
            logger=self.logger,
        )


__all__ = ["AcquisitionService"]
