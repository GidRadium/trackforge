"""Serialized access to one SQLAlchemy session across worker threads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

T = TypeVar("T")


class SessionRunner:
    """Run synchronous session work in a thread, one operation at a time.

    SQLAlchemy sessions are not thread-safe.  ``asyncio.to_thread`` may use a
    different worker for each call, so an ``asyncio.Lock`` serializes access
    within a unit of work.  Cross-process writes are serialized by SQLite WAL
    and ``busy_timeout``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._lock = asyncio.Lock()

    @property
    def session(self) -> Session:
        return self._session

    async def run(self, operation: Callable[[Session], T]) -> T:
        async with self._lock:
            try:
                return await asyncio.to_thread(operation, self._session)
            except SQLAlchemyError:
                # A failed flush leaves the session in a deactive transaction;
                # roll it back before the translated error reaches the caller.
                await asyncio.to_thread(_rollback_if_needed, self._session)
                raise


def _rollback_if_needed(session: Session) -> None:
    if session.in_transaction():
        session.rollback()


__all__ = ["SessionRunner"]
