"""TrackForge public package API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = [
    "AsyncTrackForge",
    "CollectionPlanOptions",
    "CollectionPlanView",
    "CollectionRunResult",
    "RunOptions",
    "TrackForge",
    "TrackForgeConfig",
    "__version__",
]

if TYPE_CHECKING:
    from trackforge.api import AsyncTrackForge as AsyncTrackForge
    from trackforge.api import TrackForge as TrackForge
    from trackforge.application.dto.collections import (
        CollectionPlanOptions as CollectionPlanOptions,
    )
    from trackforge.application.dto.collections import (
        CollectionPlanView as CollectionPlanView,
    )
    from trackforge.application.dto.collections import (
        CollectionRunResult as CollectionRunResult,
    )
    from trackforge.application.dto.results import RunOptions as RunOptions
    from trackforge.config import TrackForgeConfig as TrackForgeConfig


def __getattr__(name: str) -> Any:
    """Keep imports lazy so the domain package stays framework-free."""

    if name == "TrackForgeConfig":
        from trackforge.config import TrackForgeConfig

        value: Any = TrackForgeConfig
    elif name in {"TrackForge", "AsyncTrackForge"}:
        from trackforge import api

        value = getattr(api, name)
    elif name == "RunOptions":
        from trackforge.application.dto.results import RunOptions

        value = RunOptions
    elif name in {"CollectionPlanOptions", "CollectionPlanView", "CollectionRunResult"}:
        from trackforge.application.dto import collections

        value = getattr(collections, name)
    else:
        message = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(message)
    globals()[name] = value
    return value
