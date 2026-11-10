"""Cookie passthrough tests: yt-dlp owns and may update the configured file."""

# pyright: standard

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from trackforge.acquire.auth import inspect_cookie_file
from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import CookieError
from trackforge.acquire.loaders.ytdlp_options import (
    build_ydl_options,
    prepare_ydl_options,
)
from trackforge.acquire.staging import StagingArea


def write_cookie_file(path: Path, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tCOOKIE-SECRET\n",
        encoding="utf-8",
    )
    os.chmod(path, mode)
    return path


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_configured_cookie_file_is_passed_directly(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "config" / "youtube.txt")
    options = build_ydl_options(
        SourceProfile(name="youtube", cookies_file=source, proxy="none")
    )

    assert options["cookiefile"] == str(source)
    assert mode_of(source) == 0o600
    assert "proxy" not in options


def test_prepare_options_does_not_create_a_job_copy(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "config" / "youtube.txt")
    staging = StagingArea(tmp_path / "staging")

    options = prepare_ydl_options(
        SourceProfile(name="youtube", cookies_file=source, proxy="none"),
        staging=staging,
        job_id="job-1",
        cookies_required=True,
    )

    assert options["cookiefile"] == str(source)
    assert not (staging.root / "job-1").exists()
    assert not (staging.root / "job-1" / "cookies.txt").exists()


def test_yt_dlp_can_rewrite_the_configured_cookie_file(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "youtube.txt")
    options = build_ydl_options(
        SourceProfile(name="youtube", cookies_file=source)
    )

    # Simulate yt-dlp refreshing its cookie jar in place.
    assert options["cookiefile"] == str(source)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tUPDATED\tNEW-VALUE\n"
        )

    assert "UPDATED" in source.read_text(encoding="utf-8")
    assert inspect_cookie_file(source).data_lines == 2


def test_world_readable_cookie_file_is_still_rejected(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "youtube.txt", 0o644)
    with pytest.raises(CookieError, match="world-readable"):
        build_ydl_options(SourceProfile(name="youtube", cookies_file=source))


def test_invalid_cookie_file_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.txt"
    source.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\n",
        encoding="utf-8",
    )
    os.chmod(source, 0o600)
    with pytest.raises(CookieError) as error:
        build_ydl_options(SourceProfile(name="youtube", cookies_file=source))
    assert "COOKIE-SECRET" not in str(error.value)
    assert "7 tab-separated" in str(error.value)


def test_inspection_never_returns_cookie_values(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "youtube.txt")
    result = inspect_cookie_file(source)
    rendered = repr(result)
    for secret in ("COOKIE-SECRET", "SID=COOKIE-SECRET"):
        assert secret not in rendered
    assert "COOKIE-SECRET" not in repr(result.error)
