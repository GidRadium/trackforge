"""Optional local HTTP API (GUI deferred to v1.1).

Importing this package must stay lightweight: the CLI imports auth helpers for
``trackforge serve`` even when the ``[http]`` extra is not installed.  The
FastAPI application is therefore loaded lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["create_app"]

if TYPE_CHECKING:
    from trackforge.interfaces.http.app import create_app as create_app


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from trackforge.interfaces.http.app import create_app

        globals()[name] = create_app
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
