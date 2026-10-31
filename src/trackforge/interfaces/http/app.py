"""FastAPI application factory for the optional local HTTP API.

The transport layer is deliberately thin: it authenticates local callers,
validates request shapes, and delegates every operation to ``AsyncTrackForge``.
There is no database or adapter access from routes.  The GUI is deferred.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from trackforge import __version__
from trackforge.api import AsyncTrackForge
from trackforge.config import TrackForgeConfig
from trackforge.interfaces.http.auth import (
    AUTHORIZATION_HEADER,
    ensure_token,
    is_local_origin,
    verify_bearer,
)
from trackforge.interfaces.http.deps import Facade
from trackforge.interfaces.http.errors import error_response, install_error_handlers
from trackforge.interfaces.http.routes import routers

MAX_REQUEST_BYTES = 1_048_576
LOCAL_ORIGIN_REGEX = r"^http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$"
_PUBLIC_PATHS = frozenset({"/healthz"})


def _requires_auth(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def create_app(
    config: TrackForgeConfig,
    *,
    facade: Facade | None = None,
    token: str | None = None,
    token_path: Path | None = None,
) -> FastAPI:
    """Build the local HTTP application.

    ``facade`` and ``token`` are injectable for tests and embedding; production
    callers pass only ``config`` and receive a facade opened during lifespan.
    """

    owned_facade = facade is None
    active_facade: Facade = facade if facade is not None else cast(Facade, AsyncTrackForge(config))
    token_value, token_file = ensure_token(config, token=token, path=token_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owned_facade:
            await active_facade.__aenter__()  # type: ignore[attr-defined]
        try:
            yield
        finally:
            tasks: set[asyncio.Task[Any]] = app.state.background_tasks
            for task in tuple(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
            if owned_facade:
                await active_facade.close()

    app = FastAPI(
        title="TrackForge Local HTTP API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.facade = active_facade
    app.state.http_token = token_value
    app.state.http_token_path = token_file
    app.state.background_tasks = set()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=LOCAL_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[AUTHORIZATION_HEADER, "Content-Type", "Prefer"],
        max_age=600,
    )

    @app.middleware("http")
    async def _local_auth_and_limits(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if request.method == "OPTIONS" and is_local_origin(request.headers.get("origin", "")):
            return await call_next(request)
        if _requires_auth(path) and path not in _PUBLIC_PATHS:
            if not verify_bearer(request.headers.get(AUTHORIZATION_HEADER), token_value):
                return error_response(
                    401,
                    "unauthorized",
                    "missing or invalid bearer token",
                    retryable=False,
                    user_action=(
                        "Read the token from state_dir/http-token and send "
                        "'Authorization: Bearer <token>'."
                    ),
                )
        if request.method in {"POST", "PUT", "PATCH"}:
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except ValueError:
                    return error_response(
                        400,
                        "bad_request",
                        "invalid Content-Length header",
                    )
                if length > MAX_REQUEST_BYTES:
                    return error_response(
                        413,
                        "payload_too_large",
                        f"request body exceeds {MAX_REQUEST_BYTES} bytes",
                    )
        response: Response = await call_next(request)
        if _requires_auth(path):
            if "Cache-Control" not in response.headers:
                response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    install_error_handlers(app)
    for router in routers:
        app.include_router(router)
    return app


__all__ = ["MAX_REQUEST_BYTES", "create_app"]
