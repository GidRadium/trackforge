"""Alembic migration assets referenced by the SQLite catalog bootstrap.

The canonical ``env.py`` and version scripts live in the repository-level
``migrations/`` directory so they can be inspected and run with the standard
Alembic CLI.  This package marks the adapter-owned migration boundary and is
imported by tooling that wants to locate the catalog adapter.
"""

from __future__ import annotations

__all__: list[str] = []
