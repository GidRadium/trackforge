"""LLM provider adapters."""

from __future__ import annotations

from trackforge.adapters.analysis.llm.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)

__all__ = ["OpenAICompatibleConfig", "OpenAICompatibleProvider"]
