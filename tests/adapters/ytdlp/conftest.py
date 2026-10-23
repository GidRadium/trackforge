"""Fixtures for the yt-dlp adapter tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from trackforge.adapters.sources.ytdlp.client import YtDlpClient

FAKE_DIR = Path(__file__).resolve().parents[2] / "fakes"
FAKE_YTDLP = FAKE_DIR / "yt-dlp"
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "ytdlp"
YOUTUBE_URL = "https://www.youtube.com/watch?v=demo"
SOUNDCLOUD_URL = "https://soundcloud.com/fixture/fixture-track"


@pytest.fixture
def fake_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PATH", f"{FAKE_DIR}{os.pathsep}{os.environ.get('PATH', '')}")
    assert FAKE_YTDLP.is_file()
    return FAKE_YTDLP


@pytest.fixture
def fake_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "yt-dlp-argv.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(path))
    return path


@pytest.fixture
def fake_client(fake_path: Path, tmp_path: Path) -> YtDlpClient:
    return YtDlpClient(
        str(fake_path),
        timeout_metadata=5,
        timeout_download=10,
        cache_dir=tmp_path / "ytdlp-cache",
    )


def read_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_fixture(name: str) -> dict[str, Any]:
    document = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


__all__ = [
    "FAKE_DIR",
    "FAKE_YTDLP",
    "FIXTURE_DIR",
    "SOUNDCLOUD_URL",
    "YOUTUBE_URL",
    "load_fixture",
    "read_log",
]
