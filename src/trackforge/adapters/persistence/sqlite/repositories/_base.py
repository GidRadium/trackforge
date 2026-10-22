"""Shared base class for SQLite repositories."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._session import SessionRunner

T = TypeVar("T")


class SqliteRepository:
    """A repository bound to one unit-of-work session."""

    def __init__(self, runner: SessionRunner) -> None:
        self._runner = runner

    async def _run(self, operation: Callable[[Session], T]) -> T:
        return await self._runner.run(operation)


__all__ = ["SqliteRepository"]
