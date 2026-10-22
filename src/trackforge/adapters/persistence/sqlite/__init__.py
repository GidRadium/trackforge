"""SQLite catalog adapter (SQLAlchemy 2 sync + async unit of work)."""

from __future__ import annotations

from trackforge.adapters.persistence.sqlite.engine import (
    BUSY_TIMEOUT_MS,
    CATALOG_FILE_NAME,
    Catalog,
    catalog_path,
    create_catalog_engine,
    initialize_catalog,
)
from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.adapters.persistence.sqlite.unit_of_work import (
    SqliteUnitOfWork,
    UnitOfWork,
)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "CATALOG_FILE_NAME",
    "Catalog",
    "FileLock",
    "SqliteUnitOfWork",
    "UnitOfWork",
    "catalog_path",
    "create_catalog_engine",
    "initialize_catalog",
]
