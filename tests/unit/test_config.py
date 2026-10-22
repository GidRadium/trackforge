"""Unit tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.bootstrap import build_context
from trackforge.config import ConfigError, TrackForgeConfig

pytestmark = pytest.mark.unit


def test_defaults_use_platformdirs_and_music_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    home = tmp_path / "home"
    xdg_config = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    xdg_cache = tmp_path / "xdg-cache"
    xdg_state = tmp_path / "xdg-state"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))

    config = TrackForgeConfig()

    assert config.config_dir == xdg_config / "trackforge"
    assert config.data_dir == xdg_data / "trackforge"
    assert config.cache_dir == xdg_cache / "trackforge"
    assert config.state_dir == xdg_state / "trackforge"
    assert config.library_dir == home / "Music" / "TrackForge"
    assert config.profile == "balanced"
    assert config.mode == "policy"
    assert config.log_level == "INFO"
    assert config.offline is False


def test_env_overrides_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRACKFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRACKFORGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TRACKFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRACKFORGE_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "full")
    monkeypatch.setenv("TRACKFORGE_CORE_MODE", "manual")
    monkeypatch.setenv("TRACKFORGE_CORE_LOG_LEVEL", "debug")
    monkeypatch.setenv("TRACKFORGE_CORE_OFFLINE", "true")

    config = TrackForgeConfig()

    assert config.config_dir == tmp_path / "config"
    assert config.data_dir == tmp_path / "data"
    assert config.cache_dir == tmp_path / "cache"
    assert config.state_dir == tmp_path / "state"
    assert config.library_dir == tmp_path / "library"
    assert config.profile == "full"
    assert config.mode == "manual"
    assert config.log_level == "DEBUG"
    assert config.offline is True


def test_direct_env_name_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_PROFILE", "fast")

    assert TrackForgeConfig().profile == "fast"


def test_toml_overrides_defaults(
    toml_config_path: Path,
    tmp_path: Path,
    clean_env: None,
) -> None:
    library_dir = tmp_path / "music"
    with toml_config_path.open("a", encoding="utf-8") as file:
        file.write(f'library_dir = "{library_dir}"\n')

    config = TrackForgeConfig.from_file(toml_config_path)

    assert config.profile == "fast"
    assert config.library_dir == library_dir


def test_env_overrides_toml(
    toml_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "full")

    config = TrackForgeConfig.from_file(toml_config_path)

    assert config.profile == "full"


def test_explicit_argument_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "fast")

    config = TrackForgeConfig(profile="custom")

    assert config.profile == "custom"


def test_from_file_uses_default_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[core]\nprofile = "fast"\n', encoding="utf-8")
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(config_dir))

    assert TrackForgeConfig.from_file().profile == "fast"


def test_missing_default_config_file_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "absent"))

    assert TrackForgeConfig.from_file().profile == "balanced"


def test_missing_explicit_config_file_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "config"
    assert "missing.toml" in str(exc_info.value)


@pytest.mark.parametrize(
    ("env_name", "value", "key"),
    [
        ("TRACKFORGE_CORE_PROFILE", "turbo", "profile"),
        ("TRACKFORGE_CORE_MODE", "autopilot", "mode"),
        ("TRACKFORGE_CORE_LOG_LEVEL", "verbose", "log_level"),
    ],
)
def test_invalid_enum_values_raise_config_error(
    env_name: str,
    value: str,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv(env_name, value)

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig()

    assert exc_info.value.key == key
    assert key in str(exc_info.value)


def test_invalid_constructor_value_raises_config_error(clean_env: None) -> None:
    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig(profile="turbo")  # type: ignore[arg-type]

    assert exc_info.value.key == "profile"


def test_unknown_toml_key_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[core]\nmystery = "value"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert "core.mystery" in str(exc_info.value)
    assert exc_info.value.key == "core.mystery"


def test_unknown_toml_section_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[library]\nlayout = "flat"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert "library" in str(exc_info.value)


def test_invalid_toml_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[core\nprofile = 'fast'\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "config"


def test_empty_env_path_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_DATA_DIR", "")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig()

    assert exc_info.value.key == "data_dir"
    assert "empty" in str(exc_info.value)


def test_path_values_expand_home(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("TRACKFORGE_LIBRARY_DIR", "~/Music/TrackForge")

    assert TrackForgeConfig().library_dir == Path("/home/tester/Music/TrackForge")


def test_config_load_does_not_create_directories_or_read_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    (tmp_path / ".env").write_text(
        "TRACKFORGE_CORE_PROFILE=fast\nTRACKFORGE_DATA_DIR=/wrong\n",
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file()

    assert config.profile == "balanced"
    assert not config.config_dir.exists()
    assert not config.data_dir.exists()
    assert not config.cache_dir.exists()
    assert not config.state_dir.exists()
    assert not config.library_dir.exists()


def test_fixture_config_is_wrapped_by_container(config: TrackForgeConfig) -> None:
    context = build_context(config)

    assert context.config is config
