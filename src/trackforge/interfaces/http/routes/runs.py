"""Run lifecycle routes: create, status, cancel, and SSE events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from trackforge.application.dto.db import RunStatus
from trackforge.application.dto.plan import PlanView
from trackforge.config import TrackForgeConfig
from trackforge.interfaces.http.deps import Facade, get_config, get_facade, validate_id
from trackforge.interfaces.http.errors import ApiError
from trackforge.interfaces.http.schemas import RunRequest, SourcePayload, options_for
from trackforge.interfaces.http.serialization import (
    event_payload,
    plan_from_payload,
    plan_result_to_payload,
    run_result_payload,
    source_from_payload,
    status_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


def _response_for(result: Any) -> JSONResponse:
    if isinstance(result, PlanView):
        return JSONResponse(content=plan_result_to_payload(result))
    return JSONResponse(content=run_result_payload(result))


def _track_task(task: asyncio.Task[Any], tasks: set[asyncio.Task[Any]]) -> None:
    tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("background run failed: %s", type(error).__name__)


@router.post("/api/runs")
async def create_run(
    payload: RunRequest,
    request: Request,
    facade: Annotated[Facade, Depends(get_facade)],
    config: Annotated[TrackForgeConfig, Depends(get_config)],
    wait: bool = Query(default=True, description="Wait for completion before responding."),
) -> JSONResponse:
    """Run a source or a previously returned plan.

    By default the endpoint returns the completed ``RunResult``.  With
    ``?wait=false`` (or ``Prefer: respond-async``) it returns ``202`` and a
    ``RunView`` as soon as the run id exists, so a client can subscribe to SSE
    or cancel it.
    """

    prefer_async = "respond-async" in request.headers.get("prefer", "").casefold()
    if prefer_async:
        wait = False
    options = options_for(payload.options, config)
    target: Any
    if payload.plan is not None:
        target = plan_from_payload(payload.plan)
    else:
        source_value = (
            payload.source.model_dump()
            if isinstance(payload.source, SourcePayload)
            else payload.source
        )
        target = source_from_payload(source_value)

    if wait:
        result = await facade.run(target, options)
        return _response_for(result)

    run_id_holder: dict[str, str] = {}
    started = asyncio.Event()

    def on_start(run_id: str) -> None:
        run_id_holder["run_id"] = run_id
        started.set()

    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    task: asyncio.Task[Any] = asyncio.create_task(facade.run(target, options, on_start=on_start))
    tasks.add(task)
    task.add_done_callback(lambda finished: _track_task(finished, tasks))

    waiter = asyncio.create_task(started.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    run_id = run_id_holder.get("run_id")
    if run_id is None:
        # Dry runs and collection plans may finish before creating a run
        # record; return their normal result instead of an unusable 202.
        result = await task
        return _response_for(result)
    status = await facade.status(run_id)
    return JSONResponse(status_code=202, content=status_payload(status))


@router.get("/api/runs/{run_id}")
async def get_run(
    run_id: str,
    facade: Annotated[Facade, Depends(get_facade)],
) -> JSONResponse:
    """Return persisted run/step status; never a live-only progress value."""

    status = await facade.status(validate_id(run_id, kind="run"))
    return JSONResponse(content=status_payload(status))


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    facade: Annotated[Facade, Depends(get_facade)],
) -> JSONResponse:
    """Request cooperative cancellation and return ``202`` when accepted."""

    run_id = validate_id(run_id, kind="run")
    status = await facade.status(run_id)
    if status.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ApiError(
            409,
            "run_not_running",
            "run is already finished",
            user_action="Refresh the run status.",
            context={"run_id": run_id, "status": status.status.value},
        )
    cancellation_requested = await facade.cancel(run_id)
    if not cancellation_requested:
        raise ApiError(
            409,
            "run_not_cancellable",
            "run is not active in this server process",
            user_action="Wait for the run to finish or restart the operation.",
            context={"run_id": run_id},
        )
    return JSONResponse(
        status_code=202,
        content={"run_id": run_id, "cancelled": True, "status": "cancelling"},
    )


@router.get("/api/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    facade: Annotated[Facade, Depends(get_facade)],
) -> EventSourceResponse:
    """Stream the same in-process ``EventBus`` events used by the Python API."""

    run_id = validate_id(run_id, kind="run")
    await facade.status(run_id)

    async def stream() -> AsyncIterator[dict[str, Any]]:
        async for event in facade.events(run_id):
            if await request.is_disconnected():
                return
            payload = event_payload(event)
            yield {
                "event": str(payload.get("type", "message")),
                "id": str(payload.get("sequence", "")),
                "data": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }

    return EventSourceResponse(
        stream(),
        ping=15,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = ["router"]
