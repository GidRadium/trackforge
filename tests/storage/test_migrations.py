"""Integration tests for catalog migration, pragmas, and permissions."""

from __future__ import annotations

import stat
import warnings
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, SAWarning

from trackforge.adapters.persistence.sqlite.engine import (
    CATALOG_FILE_NAME,
    catalog_path,
    initialize_catalog,
)
from trackforge.adapters.persistence.sqlite.models import Base
from trackforge.config import TrackForgeConfig
from trackforge.domain.errors import MigrationError

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]

APP_TABLES = {
    "artifacts",
    "claim_events",
    "claims",
    "decision_records",
    "decisions",
    "feedback_events",
    "file_instances",
    "intakes",
    "items",
    "library_entries",
    "locks",
    "media_objects",
    "model_registry",
    "output_bundles",
    "renders",
    "review_session_items",
    "review_sessions",
    "run_items",
    "runs",
    "steps",
}
FORBIDDEN_TABLES = {
    "collections",
    "collection_items",
    "schema_migrations",
    "recordings",
    "releases",
    "artists",
}


def test_empty_data_dir_upgrades_to_all_mvp_tables(
    config: TrackForgeConfig,
) -> None:
    catalog = initialize_catalog(config.data_dir)
    try:
        with catalog.connect() as connection:
            tables = set(inspect(connection).get_table_names())
        assert tables == APP_TABLES | {"alembic_version"}
        assert tables.isdisjoint(FORBIDDEN_TABLES)
        with catalog.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0002"
    finally:
        catalog.close()


def test_repeated_upgrade_is_idempotent(config: TrackForgeConfig) -> None:
    first = initialize_catalog(config.data_dir)
    first.close()

    second = initialize_catalog(config.data_dir)
    try:
        with second.connect() as connection:
            assert inspect(connection).get_table_names().count("items") == 1
            versions = connection.execute(text("SELECT version_num FROM alembic_version")).all()
        assert versions == [("0002",)]
    finally:
        second.close()


def test_required_pragmas_are_active(config: TrackForgeConfig) -> None:
    catalog = initialize_catalog(config.data_dir)
    try:
        with catalog.connect() as connection:
            journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar()
            foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar()
            busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar()
        assert str(journal_mode).lower() == "wal"
        assert foreign_keys == 1
        assert busy_timeout == 5000
    finally:
        catalog.close()


def test_migration_matches_orm_metadata(config: TrackForgeConfig) -> None:
    catalog = initialize_catalog(config.data_dir)
    try:
        with catalog.connect() as connection:
            migration_context = MigrationContext.configure(
                connection,
                opts={"compare_type": True},
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SAWarning)
                differences = compare_metadata(migration_context, Base.metadata)
        assert differences == []
    finally:
        catalog.close()


def test_data_dir_and_catalog_file_are_private(config: TrackForgeConfig) -> None:
    catalog = initialize_catalog(config.data_dir)
    try:
        assert stat.S_IMODE(config.data_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(catalog.path.stat().st_mode) == 0o600
        assert catalog.path == config.data_dir / CATALOG_FILE_NAME
        assert catalog.path == catalog_path(config.data_dir)
    finally:
        catalog.close()


def test_corrupt_database_raises_migration_error(config: TrackForgeConfig) -> None:
    config.data_dir.mkdir(parents=True)
    (config.data_dir / CATALOG_FILE_NAME).write_bytes(b"definitely not sqlite")

    with pytest.raises(MigrationError) as caught:
        initialize_catalog(config.data_dir)

    assert "catalog" in caught.value.message.lower()
    assert caught.value.user_action


def test_downgrade_to_base_and_back(config: TrackForgeConfig) -> None:
    catalog = initialize_catalog(config.data_dir)
    try:
        alembic_config = _alembic_config()
        with catalog.connect() as connection:
            alembic_config.attributes["connection"] = connection
            command.downgrade(alembic_config, "base")
            connection.commit()
        with catalog.connect() as connection:
            assert set(inspect(connection).get_table_names()) <= {"alembic_version"}

        with catalog.connect() as connection:
            alembic_config.attributes["connection"] = connection
            command.upgrade(alembic_config, "head")
            connection.commit()
        with catalog.connect() as connection:
            assert set(inspect(connection).get_table_names()) == APP_TABLES | {"alembic_version"}
    finally:
        catalog.close()


def test_foreign_keys_are_enforced_at_the_database_boundary(
    config: TrackForgeConfig,
) -> None:
    catalog = initialize_catalog(config.data_dir)
    try:
        with pytest.raises(IntegrityError):
            with catalog.connect() as connection:
                connection.execute(
                    text(
                        "INSERT INTO items (id, status, created_at, updated_at) "
                        "VALUES ('item-1', 'discovered', "
                        "'2026-10-22T13:00:00.000000Z', '2026-10-22T13:00:00.000000Z')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO intakes (id, item_id, source_ref_json, candidate_json, "
                        "downloader_id, downloader_version, status, created_at) "
                        "VALUES ('intake-1', 'missing-item', '{}', '{}', 'test', '1', "
                        "'succeeded', '2026-10-22T13:00:00.000000Z')"
                    )
                )
                connection.commit()
    finally:
        catalog.close()


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config
