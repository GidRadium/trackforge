"""Composition root for TrackForge."""

from __future__ import annotations

from dataclasses import dataclass

from trackforge.config import TrackForgeConfig


@dataclass(frozen=True, slots=True)
class AppContext:
    """Explicit application dependencies assembled at the process boundary."""

    config: TrackForgeConfig


def build_context(config: TrackForgeConfig) -> AppContext:
    """Build the application context from validated configuration."""

    return AppContext(config=config)


__all__ = ["AppContext", "build_context"]
