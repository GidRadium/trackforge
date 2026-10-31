"""Item read models, decisions, review, and explain endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from trackforge.interfaces.http.deps import Facade, get_facade, validate_id
from trackforge.interfaces.http.schemas import ReviewRequestPayload
from trackforge.interfaces.http.serialization import (
    decision_payload,
    explanation_payload,
    item_payload,
    review_payload,
)

router = APIRouter(tags=["items"])


@router.get("/api/items/{item_id}")
async def get_item(
    item_id: str,
    facade: Annotated[Facade, Depends(get_facade)],
) -> JSONResponse:
    """Return the same portable ``ItemView`` exposed by the Python API."""

    view = await facade.item(validate_id(item_id, kind="item"))
    return JSONResponse(content=item_payload(view))


@router.get("/api/items/{item_id}/decisions")
async def get_decisions(
    item_id: str,
    facade: Annotated[Facade, Depends(get_facade)],
) -> JSONResponse:
    """Return persisted ``DecisionView`` values without re-resolving them."""

    decisions = await facade.decisions(validate_id(item_id, kind="item"))
    return JSONResponse(content=[decision_payload(view) for view in decisions])


@router.post("/api/items/{item_id}/review")
async def review_item(
    item_id: str,
    payload: ReviewRequestPayload,
    facade: Annotated[Facade, Depends(get_facade)],
) -> JSONResponse:
    """Apply item-scoped corrections or return the current review table."""

    corrections = payload.correction_values()
    view = await facade.review(
        validate_id(item_id, kind="item"),
        corrections,
        action=payload.action,
        field=payload.field,
        value=payload.value,
        lock=payload.lock,
        reason=payload.reason,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
        scope=payload.scope,
        mode=payload.mode,
        auto=payload.auto,
    )
    return JSONResponse(content=review_payload(view))


@router.get("/api/items/{item_id}/explain")
async def explain_item(
    item_id: str,
    field: Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")],
    facade: Annotated[Facade, Depends(get_facade)],
) -> JSONResponse:
    """Explain one field decision with its chosen claim and alternatives."""

    view = await facade.explain(validate_id(item_id, kind="item"), field)
    return JSONResponse(content=explanation_payload(view))


__all__ = ["router"]
