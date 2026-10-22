"""Integration tests for the data-directory file lock."""

from __future__ import annotations

import os
import select
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.config import TrackForgeConfig
from trackforge.domain.errors import CatalogLocked

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOLDER_SCRIPT = """
import sys
import time
from pathlib import Path

from trackforge.adapters.persistence.sqlite.file_lock import FileLock

lock = FileLock(Path(sys.argv[1]))
lock.acquire()
print("READY", flush=True)
time.sleep(60)
"""


def test_second_lock_in_same_process_fails_clearly(config: TrackForgeConfig) -> None:
    first = FileLock(config.data_dir)
    second = FileLock(config.data_dir)
    first.acquire()
    try:
        assert first.locked
        assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
        with pytest.raises(CatalogLocked) as caught:
            second.acquire()
        assert caught.value.user_action
        assert str(config.data_dir) in caught.value.context["data_dir"]
    finally:
        first.release()
    assert not first.locked

    second.acquire()
    second.release()


def test_lock_can_be_reacquired_after_release(config: TrackForgeConfig) -> None:
    lock = FileLock(config.data_dir)
    with lock:
        assert lock.locked
    assert not lock.locked
    with lock:
        assert lock.locked


@pytest.mark.asyncio
async def test_async_context_manager_releases_lock(config: TrackForgeConfig) -> None:
    lock = FileLock(config.data_dir)
    async with lock:
        assert lock.locked
    assert not lock.locked


def test_second_process_gets_catalog_locked(config: TrackForgeConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT / 'src'}{os.pathsep}{existing}" if existing else str(PROJECT_ROOT / "src")
    )
    process = subprocess.Popen(  # noqa: S603 - test-controlled interpreter and arguments
        [sys.executable, "-c", HOLDER_SCRIPT, str(config.data_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "lock-holder subprocess did not become ready"
        assert process.stdout.readline().strip() == "READY"

        contender = FileLock(config.data_dir)
        with pytest.raises(CatalogLocked) as caught:
            contender.acquire()
        assert caught.value.context["owner_pid"] == process.pid
        assert "another TrackForge process" in caught.value.message
    finally:
        process.terminate()
        try:
            _stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=10)
        assert process.returncode is not None

    assert "Traceback" not in (stderr or "")

    recovered = FileLock(config.data_dir)
    recovered.acquire()
    recovered.release()


def test_lock_file_lives_inside_data_dir(config: TrackForgeConfig) -> None:
    lock = FileLock(config.data_dir)
    assert lock.path == config.data_dir / ".lock"
    lock.acquire()
    try:
        assert lock.path.is_file()
    finally:
        lock.release()


def test_initialize_catalog_respects_existing_lock(config: TrackForgeConfig) -> None:
    from trackforge.adapters.persistence.sqlite.engine import initialize_catalog

    with FileLock(config.data_dir):
        with pytest.raises(CatalogLocked):
            initialize_catalog(config.data_dir)
