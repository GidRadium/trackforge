"""Configured cookie files plus domain-based fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.adapters.sources.ytdlp.cookies import CookieResolver
from trackforge.domain.errors import CookiesPermissionsError

pytestmark = pytest.mark.unit


def _cookie(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_configured_route_can_cover_multiple_domains_with_one_file(
    tmp_path: Path,
) -> None:
    shared = _cookie(tmp_path / "account-cookies.txt")
    resolver = CookieResolver.from_mapping(
        tmp_path / "cookies",
        {
            "youtube.com": str(shared),
            "youtu.be": str(shared),
        },
    )

    youtube = resolver.select("https://www.youtube.com/watch?v=1")
    short = resolver.select("https://youtu.be/abc")

    assert youtube is not None and youtube.path == shared.resolve()
    assert short is not None and short.path == shared.resolve()


def test_no_configured_route_falls_back_to_domain_file(tmp_path: Path) -> None:
    cookie = _cookie(tmp_path / "cookies" / "soundcloud.com.txt")
    resolver = CookieResolver.from_mapping(tmp_path / "cookies", {})

    selected = resolver.select("https://soundcloud.com/artist/track")

    assert selected is not None
    assert selected.path == cookie.resolve()


def test_empty_configured_route_disables_fallback(tmp_path: Path) -> None:
    _cookie(tmp_path / "cookies" / "youtube.com.txt")
    resolver = CookieResolver.from_mapping(
        tmp_path / "cookies",
        {"youtube.com": ""},
    )

    assert resolver.select("https://www.youtube.com/watch?v=1") is None


def test_most_specific_configured_route_wins(tmp_path: Path) -> None:
    generic = _cookie(tmp_path / "generic.txt")
    music = _cookie(tmp_path / "music.txt")
    resolver = CookieResolver.from_mapping(
        tmp_path / "cookies",
        {
            "youtube.com": str(generic),
            "music.youtube.com": str(music),
        },
    )

    selected = resolver.select("https://music.youtube.com/watch?v=1")

    assert selected is not None
    assert selected.path == music.resolve()


def test_missing_configured_cookie_file_is_a_visible_error(tmp_path: Path) -> None:
    resolver = CookieResolver.from_mapping(
        tmp_path / "cookies",
        {"youtube.com": str(tmp_path / "absent.txt")},
    )

    with pytest.raises(CookiesPermissionsError, match="cannot be read"):
        resolver.select("https://www.youtube.com/watch?v=1")


def test_relative_configured_path_is_under_config_dir(tmp_path: Path) -> None:
    cookie = _cookie(tmp_path / "custom" / "cookies" / "account.txt")
    resolver = CookieResolver.from_mapping(
        tmp_path / "custom" / "cookies",
        {"youtube.com": "cookies/account.txt"},
        base_dir=tmp_path / "custom",
    )

    selected = resolver.select("https://www.youtube.com/watch?v=1")

    assert selected is not None
    assert selected.path == cookie.resolve()
