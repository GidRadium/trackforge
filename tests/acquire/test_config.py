"""Tests for TOML/env configuration and profile merging."""

# pyright: standard

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from trackforge.acquire.config import (
    AppConfig,
    DownloadConfig,
    GeneralConfig,
    SearchConfig,
    SourceProfile,
    load_config,
    merge_profiles,
    user_config_path,
)
from trackforge.acquire.errors import ConfigError
from trackforge.acquire.models import CollisionPolicy


def write_config(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }


def test_defaults() -> None:
    config = AppConfig()
    assert config.general == GeneralConfig()
    assert config.download == DownloadConfig()
    assert config.search == SearchConfig()
    assert config.sources == {}
    profile = config.profile_for("youtube")
    assert profile.name == "youtube"
    assert profile.enabled is True
    assert profile.retries == 3


def test_app_config_load_and_from_dict(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[download]\nretries = 4\n")
    env = base_env(tmp_path)
    loaded = AppConfig.load(path, env=env, use_user_config=False)
    assert loaded.download.retries == 4

    from_dict = AppConfig.from_dict(
        {
            "general": {"output_dir": "music"},
            "sources": {"youtube": {"proxy": "http://proxy.test"}},
        },
        base_dir=tmp_path,
        env=env,
    )
    assert from_dict.general.output_dir == tmp_path / "music"
    assert from_dict.profile_for("youtube").proxy == "http://proxy.test"


def test_profile_for_merges_default_and_specific(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.default]
retries = 7
socket_timeout = 30
proxy = "http://default.test:8080"
headers = { "X-Base" = "base" }

[sources.youtube]
retries = 2
cookies = "cookies/youtube.txt"
proxy = "${YT_PROXY}"
headers = { "X-Specific" = "specific" }
extractor_args = { youtube = { player_client = ["web", "tv"] } }
""",
    )
    config = load_config(
        path,
        env={**base_env(tmp_path), "YT_PROXY": "http://specific.test:8080"},
        use_user_config=False,
    )

    profile = config.profile_for("youtube")
    assert profile.name == "youtube"
    assert profile.retries == 2
    assert profile.socket_timeout == 30.0
    assert profile.proxy == "http://specific.test:8080"
    assert profile.cookies_file == path.parent / "cookies" / "youtube.txt"
    assert profile.headers == {"X-Base": "base", "X-Specific": "specific"}
    assert profile.extractor_args == {
        "youtube": {"player_client": ("web", "tv")}
    }

    inherited = config.profile_for("soundcloud")
    assert inherited.name == "soundcloud"
    assert inherited.retries == 7
    assert inherited.socket_timeout == 30.0
    assert inherited.headers == {"X-Base": "base"}


def test_merge_profiles_direct_construction() -> None:
    base = SourceProfile(
        name="default",
        retries=5,
        socket_timeout=40.0,
        headers={"X-Base": "1"},
    )
    specific = SourceProfile(
        name="example",
        proxy="http://proxy.test",
        headers={"X-Specific": "2"},
    )
    merged = merge_profiles(base, specific)
    assert merged.proxy == "http://proxy.test"
    assert merged.retries == 5
    assert merged.socket_timeout == 40.0
    assert merged.headers == {"X-Base": "1", "X-Specific": "2"}


def test_specific_cookies_replace_default_browser_cookies() -> None:
    base = SourceProfile(
        name="default",
        cookies_from_browser=("chrome",),
    )
    specific = SourceProfile(
        name="youtube",
        cookies_file=Path("/tmp/youtube.cookies"),
    )
    merged = merge_profiles(base, specific)
    assert merged.cookies_file == Path("/tmp/youtube.cookies")
    assert merged.cookies_from_browser is None


def test_precedence_user_explicit_env_and_cli(tmp_path: Path) -> None:
    user_path = tmp_path / "xdg" / "trackforge" / "config.toml"
    user_path.parent.mkdir(parents=True)
    user_path.write_text("[download]\nretries = 1\n", encoding="utf-8")

    explicit = write_config(tmp_path, "[download]\nretries = 2\n", "explicit.toml")
    env = {**base_env(tmp_path), "TRACKFORGE_DOWNLOAD_RETRIES": "3"}

    assert load_config(explicit, env=env).download.retries == 3
    overridden = load_config(
        explicit,
        env=env,
        overrides={"download": {"retries": 4}},
    )
    assert overridden.download.retries == 4
    assert load_config(explicit, env=base_env(tmp_path)).download.retries == 2
    assert load_config(env=base_env(tmp_path)).download.retries == 1


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(
            tmp_path / "missing.toml",
            env=base_env(tmp_path),
            use_user_config=False,
        )


def test_bad_toml_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[download\nretries = 3")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path, env=base_env(tmp_path), use_user_config=False)


def test_missing_env_variable_is_an_error(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.youtube]
proxy = "${TRACKFORGE_MISSING_TEST_VAR}"
""",
    )
    with pytest.raises(ConfigError, match="TRACKFORGE_MISSING_TEST_VAR"):
        load_config(path, env=base_env(tmp_path), use_user_config=False)


def test_env_substitution_is_not_shell_evaluation(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.example]
proxy = "!echo do-not-run"
token = "${TOKEN_VALUE}"
""",
    )
    config = load_config(
        path,
        env={**base_env(tmp_path), "TOKEN_VALUE": "abc123"},
        use_user_config=False,
    )
    profile = config.profile_for("example")
    assert profile.proxy == "!echo do-not-run"
    assert profile.token == "abc123"


def test_cookies_and_browser_cookies_are_mutually_exclusive(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.youtube]
cookies = "cookies.txt"
cookies_from_browser = ["chrome"]
""",
    )
    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_config(path, env=base_env(tmp_path), use_user_config=False)


def test_cookies_from_browser_is_normalized(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.youtube]
cookies_from_browser = ["chrome", "firefox"]
""",
    )
    config = load_config(path, env=base_env(tmp_path), use_user_config=False)
    assert config.profile_for("youtube").cookies_from_browser == ("chrome", "firefox")


def test_later_config_layer_replaces_cookie_method(tmp_path: Path) -> None:
    user_path = tmp_path / "xdg" / "trackforge" / "config.toml"
    user_path.parent.mkdir(parents=True)
    user_path.write_text(
        '[sources.youtube]\ncookies = "user.cookies"\n',
        encoding="utf-8",
    )
    explicit = write_config(
        tmp_path,
        '[sources.youtube]\ncookies_from_browser = ["chrome"]\n',
        "explicit.toml",
    )

    config = load_config(explicit, env=base_env(tmp_path))
    profile = config.profile_for("youtube")
    assert profile.cookies_file is None
    assert profile.cookies_from_browser == ("chrome",)

    user_path.write_text(
        '[sources.youtube]\ncookies_from_browser = ["chrome"]\n',
        encoding="utf-8",
    )
    explicit.write_text(
        '[sources.youtube]\ncookies = "explicit.cookies"\n',
        encoding="utf-8",
    )
    config = load_config(explicit, env=base_env(tmp_path))
    profile = config.profile_for("youtube")
    assert profile.cookies_file == explicit.parent / "explicit.cookies"
    assert profile.cookies_from_browser is None

    user_path.write_text(
        '[sources.youtube]\ncookies = "user.cookies"\n',
        encoding="utf-8",
    )
    explicit.write_text(
        '[sources.youtube]\ncookies_file = "explicit.cookies"\n',
        encoding="utf-8",
    )
    config = load_config(explicit, env=base_env(tmp_path))
    profile = config.profile_for("youtube")
    assert profile.cookies_file == explicit.parent / "explicit.cookies"


def test_cwd_config_is_not_read_implicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".trackforge.toml").write_text(
        "[download]\nretries = 99\n",
        encoding="utf-8",
    )
    config = load_config(
        env=base_env(tmp_path),
        use_user_config=False,
    )
    assert config.download.retries == 3


def test_env_overrides_general_and_search(tmp_path: Path) -> None:
    env = {
        **base_env(tmp_path),
        "TRACKFORGE_OUTPUT_DIR": str(tmp_path / "music"),
        "TRACKFORGE_LANGUAGE": "ru",
        "TRACKFORGE_SEARCH_LIMIT": "5",
        "TRACKFORGE_SEARCH_LOADERS": "youtube, soundcloud",
        "TRACKFORGE_COLLISION": "error",
        "TRACKFORGE_WRITE_THUMBNAIL": "false",
    }
    config = load_config(env=env, use_user_config=False)
    assert config.general.output_dir == tmp_path / "music"
    assert config.general.language == "ru"
    assert config.search.limit == 5
    assert config.search.loaders == ("youtube", "soundcloud")
    assert config.download.collision is CollisionPolicy.ERROR
    assert config.download.write_thumbnail is False


def test_unknown_keys_warn_and_are_kept_in_extra(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = write_config(
        tmp_path,
        """
[general]
unexpected = 1

[sources.youtube]
api_version = "5.199"
another_unknown = true
""",
    )
    with caplog.at_level(logging.WARNING):
        config = load_config(path, env=base_env(tmp_path), use_user_config=False)

    assert "general.unexpected" in caplog.text
    assert "sources.youtube.api_version" in caplog.text
    profile = config.profile_for("youtube")
    assert profile.extra["api_version"] == "5.199"
    assert profile.extra["another_unknown"] is True


def test_secrets_are_not_in_repr_or_config_repr() -> None:
    profile = SourceProfile(
        name="example",
        token="TOKEN_SECRET",
        proxy="http://user:password@proxy.test",
        cookies_file=Path("/secret/cookies.txt"),
        headers={"Authorization": "Bearer HEADER_SECRET"},
        extra={"api_key": "EXTRA_SECRET"},
    )
    config = AppConfig(sources={"example": profile})
    rendered = repr(profile) + repr(config)
    for secret in (
        "TOKEN_SECRET",
        "password",
        "/secret/cookies.txt",
        "HEADER_SECRET",
        "EXTRA_SECRET",
    ):
        assert secret not in rendered
    assert profile.token == "TOKEN_SECRET"


def test_proxy_none_sentinel_is_preserved(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.youtube]
proxy = "none"
""",
    )
    config = load_config(path, env=base_env(tmp_path), use_user_config=False)
    assert config.profile_for("youtube").proxy == "none"


def test_disabled_source_is_preserved(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[sources.youtube]
enabled = false
""",
    )
    config = load_config(path, env=base_env(tmp_path), use_user_config=False)
    assert config.profile_for("youtube").enabled is False


def test_user_config_path_respects_xdg(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}
    assert user_config_path(env) == tmp_path / "xdg" / "trackforge" / "config.toml"


def test_example_config_loads_without_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    with caplog.at_level(logging.WARNING):
        config = load_config(
            example,
            env={"HOME": str(example.parent)},
            use_user_config=False,
        )

    assert not caplog.records
    assert config.general.language == "ru"
    assert config.download.collision is CollisionPolicy.SUBFOLDER
    assert config.search.loaders == ()
    assert config.profile_for("youtube").extractor_args == {
        "youtube": {"player_client": ("web", "tv")}
    }
    assert config.profile_for("vk").extra["api_version"] == "5.199"
    assert config.profile_for("soundcloud").proxy == "none"
    assert config.profile_for("yt_dlp").name == "yt_dlp"


@pytest.mark.parametrize(
    ("table", "key", "value"),
    [
        ("sources.example", "retries", -1),
        ("sources.example", "socket_timeout", 0),
        ("download", "retries", -1),
        ("download", "concurrent_fragments", 0),
        ("search", "limit", 0),
        ("download", "collision", "unknown"),
    ],
)
def test_invalid_values_raise_config_error(
    tmp_path: Path,
    table: str,
    key: str,
    value: object,
) -> None:
    rendered = f'"{value}"' if isinstance(value, str) else str(value)
    path = write_config(tmp_path, f"[{table}]\n{key} = {rendered}\n")
    with pytest.raises(ConfigError):
        load_config(path, env=base_env(tmp_path), use_user_config=False)
