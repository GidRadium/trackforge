"""TrackForge package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = ["TrackForgeConfig", "__version__"]

if TYPE_CHECKING:
    from trackforge.config import TrackForgeConfig as TrackForgeConfig


def __getattr__(name: str) -> Any:
    """Keep configuration imports lazy so domain code never pulls in pydantic."""

    if name == "TrackForgeConfig":
        from trackforge.config import TrackForgeConfig

        globals()[name] = TrackForgeConfig
        return TrackForgeConfig
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
