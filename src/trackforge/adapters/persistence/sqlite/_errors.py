"""Translate SQLAlchemy integrity failures into typed catalog errors."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from trackforge.domain.errors import (
    CatalogError,
    DuplicateCacheKey,
    DuplicateContentHash,
    DuplicateLock,
    DuplicateRecord,
    ForeignKeyViolation,
)


def translate_integrity_error(exc: IntegrityError) -> CatalogError:
    message = str(exc.orig)
    lowered = message.lower()
    context = {"database_error": message}

    if "media_objects.content_hash" in lowered:
        return DuplicateContentHash(
            "a media object with this content hash already exists",
            context=context,
        )
    if "artifacts.cache_key" in lowered:
        return DuplicateCacheKey(
            "an artifact with this cache key already exists",
            context=context,
        )
    if "locks.item_id" in lowered and "locks.field" in lowered:
        return DuplicateLock(
            "a lock for this item and field already exists",
            context=context,
        )
    if "foreign key constraint failed" in lowered:
        return ForeignKeyViolation(
            "catalog write references a missing row",
            context=context,
        )
    return DuplicateRecord(
        "catalog write violates a unique constraint",
        context=context,
    )


__all__ = ["translate_integrity_error"]
