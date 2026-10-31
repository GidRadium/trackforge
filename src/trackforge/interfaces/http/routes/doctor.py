"""Read-only environment health endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from trackforge.interfaces.http.deps import Facade, get_facade
from trackforge.interfaces.http.serialization import doctor_payload

router = APIRouter(tags=["doctor"])


@router.get("/api/doctor")
async def doctor(facade: Annotated[Facade, Depends(get_facade)]) -> JSONResponse:
    """Return the same redacted ``DoctorReport`` as the Python API."""

    report = await facade.doctor()
    return JSONResponse(content=doctor_payload(report))


__all__ = ["router"]
