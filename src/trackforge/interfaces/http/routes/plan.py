"""``POST /api/plan`` — side-effect-free planning."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from trackforge.config import TrackForgeConfig
from trackforge.interfaces.http.deps import Facade, get_config, get_facade
from trackforge.interfaces.http.schemas import PlanRequest, SourcePayload, options_for
from trackforge.interfaces.http.serialization import plan_result_to_payload, source_from_payload

router = APIRouter(tags=["plan"])


@router.post("/api/plan")
async def plan(
    payload: PlanRequest,
    facade: Annotated[Facade, Depends(get_facade)],
    config: Annotated[TrackForgeConfig, Depends(get_config)],
) -> JSONResponse:
    """Resolve a source and return a read-only ``PlanView``."""

    source_value = (
        payload.source.model_dump() if isinstance(payload.source, SourcePayload) else payload.source
    )
    options = options_for(payload.options, config)
    result = await facade.plan(source_from_payload(source_value), options)
    return JSONResponse(content=plan_result_to_payload(result))
