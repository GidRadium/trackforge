"""``trackforge serve`` — run the optional local HTTP API.

The command binds loopback only in v1.  Remote access, user accounts, and the
GUI are non-goals; the bearer token value is never printed or logged.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from trackforge.config import ConfigError
from trackforge.interfaces.cli.common import build_config, execute
from trackforge.interfaces.http.auth import is_local_host

_LOCAL_HOSTS = "127.0.0.1, localhost, or ::1"


def serve_command(
    host: Annotated[
        str,
        typer.Option("--host", help="Loopback bind address (remote access is not supported)."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", min=0, max=65535, help="TCP port; 0 selects a free port."),
    ] = 8765,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one startup JSON object (no token value)."),
    ] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Serve the local FastAPI API with bearer-token authentication."""

    def body() -> None:
        if not is_local_host(host):
            raise ConfigError(
                f"refusing to bind {host!r}; use {_LOCAL_HOSTS}",
                key="http.host",
            )
        try:
            import uvicorn  # noqa: PLC0415 - optional [http] extra

            from trackforge.interfaces.http.app import create_app  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised without the extra
            raise ConfigError(
                "the HTTP API requires the [http] extra",
                key="http",
            ) from exc

        config = build_config(data_dir=data_dir, library_dir=library_dir)
        app = create_app(config)
        token_path = Path(app.state.http_token_path)
        _serve(
            app,
            host=host,
            port=port,
            log_level=config.log_level,
            as_json=as_json,
            token_path=token_path,
            uvicorn=uvicorn,
        )

    execute(as_json, body)


def _serve(
    app: Any,
    *,
    host: str,
    port: int,
    log_level: str,
    as_json: bool,
    token_path: Path,
    uvicorn: Any,
) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level.casefold(),
            access_log=not as_json,
            lifespan="on",
            timeout_keep_alive=5,
        )
    )

    async def runner() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            await task
            return
        actual_port = _bound_port(server, port)
        _emit_startup(
            host=host,
            port=actual_port,
            token_path=token_path,
            as_json=as_json,
        )
        await task

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        return


def _bound_port(server: Any, fallback: int) -> int:
    for bound in getattr(server, "servers", ()) or ():
        sockets = getattr(bound, "sockets", None) or ()
        for socket in sockets:
            try:
                return int(socket.getsockname()[1])
            except (OSError, IndexError, TypeError):
                continue
    return fallback


def _emit_startup(*, host: str, port: int, token_path: Path, as_json: bool) -> None:
    payload = {
        "event": "http.startup",
        "host": host,
        "port": port,
        "token_path": str(token_path),
        "auth": "bearer",
    }
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    typer.echo(
        f"TrackForge HTTP API on http://{display_host}:{port} "
        f"(token file: {token_path}; use Authorization: Bearer <token>)"
    )


__all__ = ["serve_command"]
