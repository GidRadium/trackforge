"""Typed application configuration.

Configuration precedence is, from highest to lowest:

1. Explicit constructor arguments.
2. ``TRACKFORGE_*`` environment variables.
3. ``config.toml`` (the ``[core]`` table).
4. Built-in defaults.

The loader never creates directories and never reads ``.env`` files.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from platformdirs import (
    user_cache_dir,
    user_config_dir,
    user_data_dir,
    user_state_dir,
)
from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from trackforge.config.errors import ConfigError

Profile = Literal["fast", "balanced", "full", "custom"]
Mode = Literal["policy", "llm_assisted", "llm_autopilot", "manual"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_APP_NAME = "trackforge"
_CONFIG_DIRECTORY_FIELDS = (
    "config_dir",
    "data_dir",
    "cache_dir",
    "state_dir",
    "library_dir",
)


def _default_config_dir() -> Path:
    return Path(user_config_dir(_APP_NAME))


def _default_data_dir() -> Path:
    return Path(user_data_dir(_APP_NAME))


def _default_cache_dir() -> Path:
    return Path(user_cache_dir(_APP_NAME))


def _default_state_dir() -> Path:
    return Path(user_state_dir(_APP_NAME))


def _default_library_dir() -> Path:
    return Path.home() / "Music" / "TrackForge"


class _TomlSettingsSource(PydanticBaseSettingsSource):
    """Settings source for values already read from ``config.toml``."""

    def __init__(self, settings_cls: type[BaseSettings], values: Mapping[str, Any]) -> None:
        super().__init__(settings_cls)
        self._values = dict(values)

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # pydantic-settings keys each source by the field's preferred alias.
        # Normalizing TOML keys the same way prevents a TOML field name and an
        # environment alias from being merged as two separate inputs.
        return {_preferred_alias(field_name): value for field_name, value in self._values.items()}


def _preferred_alias(field_name: str) -> str:
    alias = TrackForgeConfig.model_fields[field_name].validation_alias
    if isinstance(alias, AliasChoices):
        for choice in alias.choices:
            if isinstance(choice, str):
                return choice
    return field_name


class TrackForgeConfig(BaseSettings):
    """Validated TrackForge configuration."""

    model_config = SettingsConfigDict(
        env_prefix="TRACKFORGE_",
        env_prefix_target="all",
        extra="forbid",
        validate_by_name=True,
        loc_by_alias=False,
        validate_default=True,
    )

    config_dir: Path = Field(
        default_factory=_default_config_dir,
        validation_alias=AliasChoices("CONFIG_DIR", "CORE_CONFIG_DIR"),
    )
    data_dir: Path = Field(
        default_factory=_default_data_dir,
        validation_alias=AliasChoices("DATA_DIR", "CORE_DATA_DIR"),
    )
    cache_dir: Path = Field(
        default_factory=_default_cache_dir,
        validation_alias=AliasChoices("CACHE_DIR", "CORE_CACHE_DIR"),
    )
    state_dir: Path = Field(
        default_factory=_default_state_dir,
        validation_alias=AliasChoices("STATE_DIR", "CORE_STATE_DIR"),
    )
    library_dir: Path = Field(
        default_factory=_default_library_dir,
        validation_alias=AliasChoices("LIBRARY_DIR", "CORE_LIBRARY_DIR"),
    )
    profile: Profile = Field(
        default="balanced",
        validation_alias=AliasChoices("CORE_PROFILE", "PROFILE"),
    )
    mode: Mode = Field(
        default="policy",
        validation_alias=AliasChoices("CORE_MODE", "MODE"),
    )
    log_level: LogLevel = Field(
        default="INFO",
        validation_alias=AliasChoices("CORE_LOG_LEVEL", "LOG_LEVEL"),
    )
    offline: bool = Field(
        default=False,
        validation_alias=AliasChoices("CORE_OFFLINE", "OFFLINE"),
    )
    # Downloader settings are internal to the URL adapter.  ``config show``
    # filters them so the portable configuration surface stays stable.
    ytdlp_binary: str = Field(
        default="yt-dlp",
        validation_alias=AliasChoices("YTDLP_BINARY"),
    )
    deno_binary: str = Field(
        default="deno",
        validation_alias=AliasChoices("DENO_BINARY"),
    )
    ytdlp_timeout_metadata: float = Field(
        default=60.0,
        gt=0,
        validation_alias=AliasChoices("YTDLP_TIMEOUT_METADATA"),
    )
    ytdlp_timeout_download: float = Field(
        default=1800.0,
        gt=0,
        validation_alias=AliasChoices("YTDLP_TIMEOUT_DOWNLOAD"),
    )

    @property
    def cookies_dir(self) -> Path:
        """Per-site Netscape cookie files live under the config directory."""

        return Path(self.config_dir) / "cookies"

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except (SettingsError, ValidationError) as exc:
            raise _to_config_error(exc) from None

    @field_validator(*_CONFIG_DIRECTORY_FIELDS, mode="before")
    @classmethod
    def _normalize_path(cls, value: object) -> object:
        if isinstance(value, Path):
            return value.expanduser()
        if isinstance(value, str) or isinstance(value, os.PathLike):
            raw = os.fspath(value)
            if isinstance(raw, bytes):
                raw = raw.decode()
            if not raw.strip():
                raise ValueError("must not be empty")
            return Path(raw).expanduser()
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> TrackForgeConfig:
        """Load configuration from TOML, environment, and defaults.

        When ``path`` is omitted, ``<config_dir>/config.toml`` is used if it
        exists. A missing default file is not an error; an explicitly provided
        missing file is.
        """

        explicit_path = path is not None
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = _default_config_path()

        if not config_path.exists():
            if explicit_path:
                raise ConfigError(
                    f"configuration file not found: {config_path}",
                    key="config",
                )
            return cls()

        values = _load_core_table(config_path)
        if not values:
            return cls()
        return _build_with_toml(values)


def _to_config_error(exc: SettingsError | ValidationError) -> ConfigError:
    if isinstance(exc, SettingsError):
        return ConfigError(str(exc), key="config")

    details: list[str] = []
    keys: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        if location:
            keys.append(location)
            details.append(f"{location}: {error['msg']}")
        else:
            details.append(f"config: {error['msg']}")

    key = keys[0] if len(keys) == 1 else None
    message = "; ".join(details) or "invalid configuration"
    return ConfigError(message, key=key)


def _default_config_path() -> Path:
    configured = os.environ.get("TRACKFORGE_CONFIG_DIR") or os.environ.get(
        "TRACKFORGE_CORE_CONFIG_DIR"
    )
    if configured and configured.strip():
        config_dir = Path(configured).expanduser()
    else:
        config_dir = _default_config_dir()
    return config_dir / "config.toml"


def _load_core_table(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}", key="config") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"invalid TOML in {path}: {exc}",
            key="config",
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"cannot read configuration file {path}: {exc.strerror or exc}",
            key="config",
        ) from None

    unknown_sections = sorted(set(document) - {"core"})
    if unknown_sections:
        section = unknown_sections[0]
        raise ConfigError(
            f"unsupported configuration section {section!r}; expected only [core]",
            key=section,
        )

    core = document.get("core", {})
    if not isinstance(core, dict):
        raise ConfigError("configuration key 'core' must be a table", key="core")

    allowed_keys = set(TrackForgeConfig.model_fields)
    unknown_keys = sorted(set(core) - allowed_keys)
    if unknown_keys:
        unknown = unknown_keys[0]
        raise ConfigError(
            f"unknown configuration key 'core.{unknown}'",
            key=f"core.{unknown}",
        )

    return dict(core)


def _build_with_toml(values: dict[str, Any]) -> TrackForgeConfig:
    class TomlBoundConfig(TrackForgeConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # Explicit values beat environment values, which beat config.toml.
            # Field defaults are appended by pydantic-settings automatically.
            return (
                init_settings,
                env_settings,
                _TomlSettingsSource(settings_cls, values),
            )

    return TomlBoundConfig()


__all__ = [
    "LogLevel",
    "Mode",
    "Profile",
    "TrackForgeConfig",
]
