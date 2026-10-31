"""HTTP route modules grouped for the application factory."""

from __future__ import annotations

from fastapi import APIRouter

from trackforge.interfaces.http.routes import doctor, items, library, meta, plan, runs

routers: tuple[APIRouter, ...] = (
    meta.router,
    plan.router,
    runs.router,
    items.router,
    library.router,
    doctor.router,
)

__all__ = ["routers"]
