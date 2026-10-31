"""Typed HTTP error mapping and redaction.

The response body is deliberately flat and stable:

``{code, message, retryable, user_action, context?}``

No request headers, bodies, credentials, or token values are echoed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from trackforge.config import ConfigError
from trackforge.domain.errors import (
    AcquisitionFailed,
    ArtifactConflict,
    ArtifactNotFound,
    BudgetExceeded,
    CandidateNotFound,
    CatalogError,
    CatalogLocked,
    CollectionTooLarge,
    DomainValidationError,
    DuplicateRecord,
    InvalidStateTransition,
    MigrationError,
    ModelUnavailable,
    ObservationFailed,
    PluginError,
    ProviderUnavailable,
    PublishFailed,
    RateLimited,
    RecordNotFound,
    RenderFailed,
    ResolutionFailed,
    ReviewRequired,
    StorageError,
    TrackForgeError,
    UnsupportedMode,
    UnsupportedScope,
    UnsupportedSource,
)
from trackforge.interfaces.http.serialization import redact

logger = logging.getLogger(__name__)

_MAX_MESSAGE = 2_000
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|cookie|password|passwd|secret)\b"
    r"\s*[:=]\s*[^\s,;]+"
)


class ApiError(Exception):
    """An expected HTTP error raised by transport code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        user_action: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.user_action = user_action
        self.context = dict(context or {})


def _snake_name(value: str) -> str:
    step_one = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    step_two = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step_one)
    return step_two.lower()


def _scrub(text: str, secret: str | None) -> str:
    value = redact(text, secret=secret)
    if not isinstance(value, str):
        value = str(value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1=***", value)
    if len(value) > _MAX_MESSAGE:
        value = f"{value[:_MAX_MESSAGE]}…"
    return value


def _status_for(exc: BaseException) -> int:
    if isinstance(exc, RecordNotFound | ArtifactNotFound | CandidateNotFound):
        return 404
    if isinstance(
        exc,
        InvalidStateTransition
        | ReviewRequired
        | DuplicateRecord
        | ArtifactConflict
        | CatalogLocked,
    ):
        return 409
    if isinstance(exc, RateLimited):
        return 429
    if isinstance(exc, ProviderUnavailable | AcquisitionFailed):
        return 503
    if isinstance(exc, TrackForgeError) and not isinstance(
        exc,
        DomainValidationError
        | UnsupportedSource
        | UnsupportedMode
        | UnsupportedScope
        | BudgetExceeded
        | ModelUnavailable
        | CollectionTooLarge
        | ResolutionFailed
        | RenderFailed
        | PublishFailed
        | ObservationFailed
        | StorageError
        | CatalogError
        | MigrationError
        | PluginError,
    ):
        return 500
    if isinstance(
        exc,
        DomainValidationError
        | UnsupportedSource
        | UnsupportedMode
        | UnsupportedScope
        | BudgetExceeded
        | ModelUnavailable
        | CollectionTooLarge
        | ResolutionFailed,
    ):
        return 400
    if isinstance(exc, ConfigError):
        return 500
    return 500


def error_body(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    user_action: str | None = None,
    context: Mapping[str, Any] | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "message": _scrub(message, secret),
        "retryable": bool(retryable),
        "user_action": _scrub(user_action, secret) if user_action else None,
    }
    if context:
        body["context"] = redact(dict(context), secret=secret)
    return body


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    user_action: str | None = None,
    context: Mapping[str, Any] | None = None,
    secret: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(
            code,
            message,
            retryable=retryable,
            user_action=user_action,
            context=context,
            secret=secret,
        ),
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            **dict(headers or {}),
        },
    )


def trackforge_error_response(exc: TrackForgeError, *, secret: str | None = None) -> JSONResponse:
    status_code = _status_for(exc)
    return error_response(
        status_code,
        _snake_name(type(exc).__name__),
        str(exc),
        retryable=exc.retryable,
        user_action=exc.user_action,
        context=exc.context,
        secret=secret,
    )


def _request_secret(request: Request) -> str | None:
    token = getattr(request.app.state, "http_token", None)
    return token if isinstance(token, str) else None


def install_error_handlers(app: FastAPI) -> None:
    """Register exception handlers that never leak internal details."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            retryable=exc.retryable,
            user_action=exc.user_action,
            context=exc.context,
            secret=_request_secret(request),
        )

    @app.exception_handler(TrackForgeError)
    async def _trackforge_error(request: Request, exc: TrackForgeError) -> JSONResponse:
        return trackforge_error_response(exc, secret=_request_secret(request))

    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, exc: ConfigError) -> JSONResponse:
        return error_response(
            500,
            "config_error",
            str(exc),
            retryable=False,
            user_action="Check the TrackForge configuration.",
            context={"key": exc.key} if exc.key else None,
            secret=_request_secret(request),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors: list[dict[str, Any]] = []
        for error in exc.errors():
            errors.append(
                {
                    "loc": [str(part) for part in error.get("loc", ())],
                    "msg": str(error.get("msg", "invalid value")),
                    "type": str(error.get("type", "value_error")),
                }
            )
        return error_response(
            422,
            "validation_error",
            "request validation failed",
            retryable=False,
            user_action="Check the request body and query parameters.",
            context={"errors": errors},
            secret=_request_secret(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            429: "rate_limited",
        }
        return error_response(
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(exc.detail) if exc.detail else "request failed",
            user_action=None,
            secret=_request_secret(request),
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Log only the type: messages and tracebacks can carry source URLs,
        # credentials, or user payloads.
        logger.error("unhandled HTTP API error: %s", type(exc).__name__)
        return error_response(
            500,
            "internal_error",
            "internal server error",
            retryable=False,
            user_action="Check the server logs; the request was not completed.",
            secret=_request_secret(request),
        )


__all__ = [
    "ApiError",
    "error_body",
    "error_response",
    "install_error_handlers",
    "trackforge_error_response",
]
