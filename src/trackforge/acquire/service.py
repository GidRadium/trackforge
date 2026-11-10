"""High-level acquisition service.

The service owns loader selection and context creation; it deliberately does
not ask questions or download anything.  L1.5 extends it with the public
``inspect`` CLI/JSON surface, while the method itself already works for the
loaders implemented in L1.1.
"""

# pyright: standard

from __future__ import annotations

import logging
import uuid
from typing import Any

from .config import AppConfig
from .loaders.base import LoaderContext
from .models import SourceInfo, SourceRef
from .progress import NullProgressSink
from .registry import LoaderRegistry, default_registry

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
        """Return normalized source metadata without downloading media."""
        resolved = self.registry.resolve(url, source=source)
        ctx = LoaderContext(
            job_id=uuid.uuid4().hex,
            profile=resolved.profile,
            progress=NullProgressSink(),
            http=self.http,
            logger=self.logger,
        )
        ref = SourceRef(source=resolved.loader.name, url=url)
        return resolved.loader.get_info(ref, ctx=ctx, flat=flat, limit=limit)

    def context_for(self, resolved: Any) -> LoaderContext:
        """Build a context for an already resolved loader (L1.5 helper)."""
        return LoaderContext(
            job_id=uuid.uuid4().hex,
            profile=resolved.profile,
            progress=NullProgressSink(),
            http=self.http,
            logger=self.logger,
        )


__all__ = ["AcquisitionService"]
