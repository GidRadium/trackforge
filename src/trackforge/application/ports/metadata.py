"""Ports and value objects for external metadata providers.

The concrete value objects live in :mod:`trackforge.application.dto.providers`
so tests, adapters, and the providers themselves can import one stable module.
This module re-exports the port for callers that look for it under ``ports``.
"""

from __future__ import annotations

from trackforge.application.dto.providers import (
    MetadataProvider,
    MetadataQuery,
    ProviderObservation,
    ttl_expiry,
)

__all__ = [
    "MetadataProvider",
    "MetadataQuery",
    "ProviderObservation",
    "ttl_expiry",
]
