"""Unauthenticated health probe and model availability listing."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from trackforge import __version__
from trackforge.interfaces.http.deps import Facade, get_facade
from trackforge.interfaces.http.serialization import models_payload

router = APIRouter(tags=["meta"])


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness probe; deliberately requires no token."""

    return JSONResponse(content={"status": "ok", "version": __version__})


@router.get("/api/models")
async def models(facade: Annotated[Facade, Depends(get_facade)]) -> JSONResponse:
    """List configured/registered models without downloading weights."""

    available = await facade.models()
    return JSONResponse(content=models_payload(available))


__all__ = ["router"]
