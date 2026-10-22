"""Alembic environment for the TrackForge catalog.

The application calls :func:`trackforge.adapters.persistence.sqlite.engine.
initialize_catalog`, which passes a live connection through
``config.attributes["connection"]``.  The standalone ``alembic upgrade head``
path creates its own engine from ``alembic.ini``.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from trackforge.adapters.persistence.sqlite.models import Base

config = context.config
target_metadata = Base.metadata


def _data_dir_database() -> Path | None:
    raw = os.environ.get("TRACKFORGE_DATA_DIR") or os.environ.get("TRACKFORGE_CORE_DATA_DIR")
    if not raw or not raw.strip():
        return None
    directory = Path(raw).expanduser()
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created:
        os.chmod(directory, 0o700)
    return directory / "catalog.sqlite"


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Run migrations without a live database connection."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against an injected or configured connection."""

    supplied = config.attributes.get("connection")
    if isinstance(supplied, Connection):
        _run_migrations(supplied)
        return

    database = _data_dir_database()
    if database is not None:
        # Use the application's engine factory so a CLI-created catalog also
        # gets WAL, foreign keys, and the busy timeout.
        from trackforge.adapters.persistence.sqlite.engine import create_catalog_engine
        from trackforge.adapters.persistence.sqlite.file_lock import FileLock

        with FileLock(database.parent):
            engine = create_catalog_engine(database)
            try:
                with engine.connect() as connection:
                    _run_migrations(connection)
                    connection.commit()
            finally:
                engine.dispose()
        return

    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_migrations(connection)
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
