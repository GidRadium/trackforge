"""TrackForge application bootstrap."""

from __future__ import annotations

from trackforge.bootstrap.container import (
    AppContext,
    CatalogContext,
    build_artifact_store,
    build_cas,
    build_catalog,
    build_catalog_context,
    build_context,
    build_unit_of_work,
)
from trackforge.bootstrap.logging import (
    JsonlFormatter,
    configure_logging,
    register_secret,
)

__all__ = [
    "AppContext",
    "CatalogContext",
    "JsonlFormatter",
    "build_artifact_store",
    "build_cas",
    "build_catalog",
    "build_catalog_context",
    "build_context",
    "build_unit_of_work",
    "configure_logging",
    "register_secret",
]
