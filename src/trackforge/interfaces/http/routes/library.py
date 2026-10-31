"""Managed-library listing for local clients.

Library scanning and GC stay in the CLI (Plan 20) and are intentionally not
exposed over HTTP in v1.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from trackforge.interfaces.http.deps import Facade, get_facade, validate_id
from trackforge.interfaces.http.errors import ApiError
from trackforge.interfaces.http.serialization import library_payload

_LIBRARY_STATUSES = frozenset({"published", "missing", "modified", "duplicate"})

router = APIRouter(tags=["library"])


@router.get("/api/library")
async def list_library(
    facade: Annotated[Facade, Depends(get_facade)],
    item_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    status: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
) -> JSONResponse:
    """Return published library entries, optionally filtered."""

    entries = await facade.library()
    if item_id is not None:
        target_item = validate_id(item_id, kind="item")
        entries = tuple(entry for entry in entries if str(entry.item_id) == target_item)
    if status is not None:
        normalized = status.strip().casefold()
        if normalized not in _LIBRARY_STATUSES:
            raise ApiError(
                400,
                "invalid_library_status",
                "unknown library status",
                user_action="Use published, missing, modified, or duplicate.",
                context={"status": status},
            )
        entries = tuple(entry for entry in entries if entry.status.value == normalized)
    return JSONResponse(content=library_payload(entries))


__all__ = ["router"]
