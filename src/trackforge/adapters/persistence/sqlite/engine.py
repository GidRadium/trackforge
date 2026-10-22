"""SQLite engine construction, pragmas, and catalog initialization.

There is no module-level engine: callers construct a :class:`Catalog` from an
explicit data directory.  Every connection enables foreign keys and a busy
timeout; the database file itself uses WAL journaling.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.domain.errors import CatalogError, MigrationError

CATALOG_FILE_NAME = "catalog.sqlite"
BUSY_TIMEOUT_MS = 5000


def catalog_path(data_dir: Path) -> Path:
    """Return the catalog file path for ``data_dir`` without touching disk."""

    return Path(data_dir).expanduser() / CATALOG_FILE_NAME


def create_catalog_engine(path: Path) -> Engine:
    """Create an engine for one catalog file with the required pragmas."""

    database = Path(path).expanduser()
    engine = create_engine(
        URL.create("sqlite", database=str(database)),
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine


class Catalog:
    """A live catalog engine plus its session factory."""

    def __init__(self, data_dir: Path, path: Path, engine: Engine) -> None:
        self._data_dir = Path(data_dir).expanduser()
        self._path = Path(path)
        self._engine = engine
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine,
            expire_on_commit=False,
        )
        self._closed = False

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def path(self) -> Path:
        return self._path

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Dispose the engine and release pooled connections."""

        if self._closed:
            return
        self._closed = True
        self._engine.dispose()

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Open a short-lived connection for inspection or tests."""

        with self._engine.connect() as connection:
            yield connection

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Catalog(path={str(self._path)!r}, closed={self._closed!r})"


def initialize_catalog(data_dir: Path) -> Catalog:
    """Create ``data_dir`` if needed and migrate the catalog to head.

    The data directory and catalog file are created with private permissions
    (0700/0600).  Repeated calls are idempotent because Alembic only applies
    missing revisions.
    """

    directory = _ensure_data_dir(data_dir)
    database = directory / CATALOG_FILE_NAME
    lock = FileLock(directory)
    with lock:
        engine = create_catalog_engine(database)
        try:
            config = _alembic_config()
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.commit()
        except (SQLAlchemyError, sqlite3.DatabaseError, OSError) as exc:
            engine.dispose()
            raise MigrationError(
                "failed to initialize the TrackForge catalog",
                context={"path": str(database), "error": str(exc)},
            ) from exc
        except Exception:
            engine.dispose()
            raise

    _secure_catalog_files(database)
    return Catalog(directory, database, engine)


def _ensure_data_dir(data_dir: Path) -> Path:
    directory = Path(data_dir).expanduser()
    if directory.exists() and not directory.is_dir():
        raise CatalogError(
            "data directory path is not a directory",
            context={"path": str(directory)},
        )
    created = not directory.exists()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise CatalogError(
            "cannot create the TrackForge data directory",
            context={"path": str(directory), "error": str(exc)},
        ) from exc
    return directory


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[5]
    ini_path = root / "alembic.ini"
    script_path = root / "migrations"
    if not ini_path.is_file() or not script_path.is_dir():
        raise MigrationError(
            "Alembic configuration is missing from the TrackForge installation",
            context={"alembic_ini": str(ini_path), "migrations": str(script_path)},
        )
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(script_path))
    return config


def _secure_catalog_files(database: Path) -> None:
    for candidate in (
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        if not candidate.exists():
            continue
        try:
            os.chmod(candidate, 0o600)
        except OSError as exc:
            raise CatalogError(
                "cannot set private permissions on the catalog file",
                context={"path": str(candidate), "error": str(exc)},
            ) from exc


__all__ = [
    "BUSY_TIMEOUT_MS",
    "CATALOG_FILE_NAME",
    "Catalog",
    "catalog_path",
    "create_catalog_engine",
    "initialize_catalog",
]
