"""Configuration model and TOML/env loading.

Precedence is ``CLI > env > --config > user config > defaults``.  The file in
the current working directory is never read implicitly.  ``${VAR}`` is a pure
environment substitution; there is no shell evaluation.
"""

# pyright: standard

from __future__ import annotations

import copy
import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .models import CollisionPolicy

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_MISSING = object()


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Configuration for one source/loader.

    Fields not understood by the core are stored in :attr:`extra`; this keeps
    loader-specific keys (``api_version`` and friends) available without
    changing the public model.  Sensitive values are excluded from ``repr``.
    """

    name: str
    enabled: bool = True
    cookies_file: Path | None = field(default=None, repr=False)
    cookies_from_browser: tuple[str, ...] | None = field(default=None, repr=False)
    proxy: str | None = field(default=None, repr=False)
    user_agent: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    impersonate: str | None = None
    extractor_args: Mapping[str, Mapping[str, tuple[str, ...]]] = field(
        default_factory=dict
    )
    retries: int = 3
    socket_timeout: float = 20.0
    rate_limit: str | None = None
    geo_bypass: bool = False
    token: str | None = field(default=None, repr=False)
    extra: Mapping[str, object] = field(default_factory=dict, repr=False)
    # Internal: exact keys present in TOML, used to merge default + specific
    # without letting constructor defaults overwrite a base value.
    _specified: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.cookies_file is not None:
            object.__setattr__(
                self,
                "cookies_file",
                Path(self.cookies_file).expanduser(),
            )
        if isinstance(self.cookies_from_browser, str):
            object.__setattr__(
                self,
                "cookies_from_browser",
                (self.cookies_from_browser,),
            )
        elif isinstance(self.cookies_from_browser, list):
            object.__setattr__(
                self,
                "cookies_from_browser",
                tuple(self.cookies_from_browser),
            )
        if self.cookies_file is not None and self.cookies_from_browser is not None:
            raise ConfigError(
                f"source {self.name!r}: cookies_file and cookies_from_browser "
                "are mutually exclusive"
            )
        if self.retries < 0:
            raise ConfigError(f"source {self.name!r}: retries must be >= 0")
        if self.socket_timeout <= 0:
            raise ConfigError(f"source {self.name!r}: socket_timeout must be > 0")


@dataclass(frozen=True, slots=True)
class GeneralConfig:
    output_dir: Path | None = None
    temp_dir: Path | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        for name in ("output_dir", "temp_dir"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).expanduser())


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    retries: int = 3
    concurrent_fragments: int = 4
    write_thumbnail: bool = True
    write_info_json: bool = False
    audio_format: str = "original"
    subtitle_langs: tuple[str, ...] = ()
    collision: CollisionPolicy = CollisionPolicy.SUBFOLDER

    def __post_init__(self) -> None:
        if not isinstance(self.collision, CollisionPolicy):
            object.__setattr__(
                self,
                "collision",
                CollisionPolicy(self.collision),
            )
        if isinstance(self.subtitle_langs, str):
            object.__setattr__(self, "subtitle_langs", (self.subtitle_langs,))
        elif not isinstance(self.subtitle_langs, tuple):
            object.__setattr__(
                self,
                "subtitle_langs",
                tuple(self.subtitle_langs),
            )
        if self.retries < 0:
            raise ConfigError("download.retries must be >= 0")
        if self.concurrent_fragments < 1:
            raise ConfigError("download.concurrent_fragments must be >= 1")


@dataclass(frozen=True, slots=True)
class SearchConfig:
    loaders: tuple[str, ...] = ()
    limit: int = 10

    def __post_init__(self) -> None:
        if isinstance(self.loaders, str):
            object.__setattr__(self, "loaders", (self.loaders,))
        elif not isinstance(self.loaders, tuple):
            object.__setattr__(self, "loaders", tuple(self.loaders))
        if self.limit < 1:
            raise ConfigError("search.limit must be >= 1")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Top-level application configuration."""

    general: GeneralConfig = field(default_factory=GeneralConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    sources: Mapping[str, SourceProfile] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        overrides: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        use_user_config: bool = True,
    ) -> AppConfig:
        """Load config using the same precedence as :func:`load_config`."""
        return load_config(
            path,
            overrides=overrides,
            env=env,
            use_user_config=use_user_config,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        base_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AppConfig:
        """Build a config from an already-parsed TOML-shaped mapping."""
        environment = dict(os.environ if env is None else env)
        prepared = _prepare_layer(
            data,
            Path(base_dir) if base_dir is not None else Path.cwd(),
            environment,
        )
        return _build_app_config(prepared)

    def profile_for(self, name: str) -> SourceProfile:
        """Return ``[sources.default]`` merged with ``[sources.<name>]``."""
        base = self.sources.get("default") or SourceProfile(name="default")
        specific = self.sources.get(name)
        if specific is None:
            return replace(base, name=name)
        merged = merge_profiles(base, specific)
        return replace(merged, name=name)


def merge_profiles(base: SourceProfile, specific: SourceProfile) -> SourceProfile:
    """Merge a specific source profile on top of the default profile.

    Only fields explicitly configured in ``specific`` override ``base``.
    Mappings (headers, extractor args, extra) are merged key by key.
    """
    explicit = specific._specified or _infer_specified(specific)
    updates: dict[str, object] = {}
    for profile_field in fields(SourceProfile):
        name = profile_field.name
        if name in {"name", "_specified"}:
            continue
        if name in explicit:
            updates[name] = getattr(specific, name)

    for mapping_name in ("headers", "extractor_args", "extra"):
        if mapping_name in explicit:
            base_mapping = dict(getattr(base, mapping_name))
            specific_mapping = dict(getattr(specific, mapping_name))
            updates[mapping_name] = _merge_mappings(base_mapping, specific_mapping)

    # Authentication is switched as a unit: a specific cookies source must
    # not be combined with the default profile's browser cookies.
    if "cookies_file" in explicit and "cookies_from_browser" not in explicit:
        updates["cookies_from_browser"] = None
    elif "cookies_from_browser" in explicit and "cookies_file" not in explicit:
        updates["cookies_file"] = None

    return replace(
        base,
        **updates,
        _specified=frozenset(explicit) | base._specified,
    )


def _merge_mappings(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    result: dict[str, object] = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_mappings(current, value)
        else:
            result[key] = value
    return result


def _infer_specified(profile: SourceProfile) -> frozenset[str]:
    """Best effort for profiles constructed directly instead of from TOML."""
    default = SourceProfile(name=profile.name)
    explicit: set[str] = set()
    for profile_field in fields(SourceProfile):
        name = profile_field.name
        if name in {"name", "_specified"}:
            continue
        value = getattr(profile, name)
        default_value = getattr(default, name)
        if name in {"headers", "extractor_args", "extra"}:
            if value:
                explicit.add(name)
        elif value != default_value:
            explicit.add(name)
    if profile.cookies_file is not None:
        explicit.add("cookies_file")
    if profile.cookies_from_browser is not None:
        explicit.add("cookies_from_browser")
    return frozenset(explicit)


def load_config(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    use_user_config: bool = True,
) -> AppConfig:
    """Load configuration from defaults, user config, ``path``, env, overrides.

    ``overrides`` uses the same nested shape as TOML and models CLI options.
    Relative paths in a config file are resolved against that file's parent,
    not the current working directory.
    """
    environment = dict(os.environ if env is None else env)
    merged: dict[str, Any] = {}

    config_files: list[Path] = []
    if use_user_config:
        user_path = user_config_path(environment)
        if user_path.is_file():
            config_files.append(user_path)
        elif user_path.exists():
            raise ConfigError(f"user config is not a file: {user_path}")

    if path is not None:
        explicit_path = Path(path).expanduser()
        if not explicit_path.is_file():
            raise ConfigError(f"config file not found: {explicit_path}")
        if not any(
            explicit_path.resolve() == candidate.resolve()
            for candidate in config_files
        ):
            config_files.append(explicit_path)

    for config_file in config_files:
        raw = _load_toml(config_file)
        prepared = _prepare_layer(raw, config_file.parent, environment)
        merged = _deep_merge(merged, prepared)

    merged = _apply_env_overrides(merged, environment)

    if overrides is not None:
        prepared = _prepare_layer(overrides, Path.cwd(), environment)
        merged = _deep_merge(merged, prepared)

    return _build_app_config(merged)


def load_app_config(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    use_user_config: bool = True,
) -> AppConfig:
    """Alias for :func:`load_config` with an explicit name."""
    return load_config(
        path,
        overrides=overrides,
        env=env,
        use_user_config=use_user_config,
    )


def user_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the platform user config path (whether or not it exists)."""
    environment = os.environ if env is None else env
    if os.name == "nt":
        base = environment.get("APPDATA")
        root = Path(base) if base else Path(environment.get("USERPROFILE", "~")).expanduser()
        return root / "trackforge" / "config.toml"

    xdg = environment.get("XDG_CONFIG_HOME")
    if xdg:
        root = Path(xdg).expanduser()
    else:
        home = environment.get("HOME")
        root = (Path(home) if home else Path.home()).expanduser() / ".config"
    return root / "trackforge" / "config.toml"


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Alias for :func:`user_config_path`."""
    return user_config_path(env)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a table: {path}")
    return data


def _prepare_layer(
    raw: Mapping[str, Any],
    base_dir: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError("config layer must be a table")
    prepared = _substitute_env(raw, env)
    result: dict[str, Any] = dict(prepared)
    _resolve_layer_paths(result, base_dir)
    return result


def _resolve_layer_paths(data: dict[str, Any], base_dir: Path) -> None:
    general = data.get("general")
    if isinstance(general, Mapping):
        general_data = dict(general)
        for key in ("output_dir", "temp_dir"):
            if general_data.get(key) is not None:
                general_data[key] = _resolve_path(general_data[key], base_dir, f"general.{key}")
        data["general"] = general_data

    sources = data.get("sources")
    if isinstance(sources, Mapping):
        prepared_sources: dict[str, Any] = {}
        for name, profile in sources.items():
            if not isinstance(profile, Mapping):
                prepared_sources[name] = profile
                continue
            profile_data = dict(profile)
            for key in ("cookies", "cookies_file"):
                if profile_data.get(key) is not None:
                    profile_data[key] = _resolve_path(
                        profile_data[key], base_dir, f"sources.{name}.{key}"
                    )
            prepared_sources[name] = profile_data
        data["sources"] = prepared_sources


def _resolve_path(value: object, base_dir: Path, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError(f"{label} must be a path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _substitute_env(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def replace_match(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise ConfigError(
                    f"environment variable {name!r} referenced by config is not set"
                )
            return env[name]

        return _ENV_PATTERN.sub(replace_match, value)
    if isinstance(value, Mapping):
        return {key: _substitute_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_env(item, env) for item in value]
    if isinstance(value, tuple):
        return tuple(_substitute_env(item, env) for item in value)
    return value


def _deep_merge(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
    *,
    _path: tuple[str, ...] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(
                current,
                value,
                _path=(*_path, key),
            )
        else:
            result[key] = value

    # A later layer that switches cookie authentication replaces the earlier
    # layer's method instead of leaving both keys in the merged table.
    if len(_path) == 2 and _path[0] == "sources":
        has_cookies = "cookies" in override
        has_cookies_file = "cookies_file" in override
        has_file = has_cookies or has_cookies_file
        has_browser = "cookies_from_browser" in override
        if has_cookies and not has_cookies_file:
            result.pop("cookies_file", None)
        elif has_cookies_file and not has_cookies:
            result.pop("cookies", None)
        if has_file and not has_browser:
            result.pop("cookies_from_browser", None)
        elif has_browser and not has_file:
            result.pop("cookies", None)
            result.pop("cookies_file", None)
    return result


def _apply_env_overrides(
    data: Mapping[str, Any],
    env: Mapping[str, str],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(data))
    settings: tuple[tuple[str, tuple[str, ...], Any], ...] = (
        ("TRACKFORGE_OUTPUT_DIR", ("general", "output_dir"), _env_path),
        ("TRACKFORGE_TEMP_DIR", ("general", "temp_dir"), _env_path),
        ("TRACKFORGE_LANGUAGE", ("general", "language"), str),
        ("TRACKFORGE_DOWNLOAD_RETRIES", ("download", "retries"), int),
        (
            "TRACKFORGE_DOWNLOAD_CONCURRENT_FRAGMENTS",
            ("download", "concurrent_fragments"),
            int,
        ),
        (
            "TRACKFORGE_WRITE_THUMBNAIL",
            ("download", "write_thumbnail"),
            _env_bool,
        ),
        (
            "TRACKFORGE_WRITE_INFO_JSON",
            ("download", "write_info_json"),
            _env_bool,
        ),
        ("TRACKFORGE_AUDIO_FORMAT", ("download", "audio_format"), str),
        (
            "TRACKFORGE_SUBTITLE_LANGS",
            ("download", "subtitle_langs"),
            _env_csv,
        ),
        ("TRACKFORGE_COLLISION", ("download", "collision"), str),
        ("TRACKFORGE_SEARCH_LIMIT", ("search", "limit"), int),
        ("TRACKFORGE_SEARCH_LOADERS", ("search", "loaders"), _env_csv),
    )
    for variable, path, converter in settings:
        if variable not in env:
            continue
        raw_value = env[variable]
        if raw_value == "":
            continue
        value = converter(raw_value)
        _set_nested(result, path, value)
    return result


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    current = data
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _env_path(value: str) -> Path:
    return Path(value).expanduser()


def _env_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigError(f"expected a boolean value, got {value!r}")


def _env_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _build_app_config(raw: Mapping[str, Any]) -> AppConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a table")

    known = {"general", "download", "search", "sources"}
    for key in raw:
        if key not in known:
            logger.warning("unknown top-level config key %r (ignored)", key)

    general = _parse_general(_table(raw.get("general"), "general"))
    download = _parse_download(_table(raw.get("download"), "download"))
    search = _parse_search(_table(raw.get("search"), "search"))
    sources = _parse_sources(_table(raw.get("sources"), "sources"))
    return AppConfig(general=general, download=download, search=search, sources=sources)


def _table(value: object, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a table")
    return value


def _parse_general(raw: Mapping[str, Any]) -> GeneralConfig:
    known = {"output_dir", "temp_dir", "language"}
    for key in raw:
        if key not in known:
            logger.warning("unknown config key general.%s (ignored)", key)
    return GeneralConfig(
        output_dir=_path_or_none(raw.get("output_dir"), "general.output_dir"),
        temp_dir=_path_or_none(raw.get("temp_dir"), "general.temp_dir"),
        language=_str_or_none(raw.get("language"), "general.language"),
    )


def _parse_download(raw: Mapping[str, Any]) -> DownloadConfig:
    known = {
        "retries",
        "concurrent_fragments",
        "write_thumbnail",
        "write_info_json",
        "audio_format",
        "subtitle_langs",
        "collision",
    }
    for key in raw:
        if key not in known:
            logger.warning("unknown config key download.%s (ignored)", key)

    collision_value = raw.get("collision", CollisionPolicy.SUBFOLDER)
    try:
        collision = CollisionPolicy(collision_value)
    except ValueError as exc:
        raise ConfigError(
            f"download.collision must be one of "
            f"{', '.join(policy.value for policy in CollisionPolicy)}"
        ) from exc

    return DownloadConfig(
        retries=_int(raw.get("retries", 3), "download.retries"),
        concurrent_fragments=_int(
            raw.get("concurrent_fragments", 4),
            "download.concurrent_fragments",
        ),
        write_thumbnail=_bool(raw.get("write_thumbnail", True), "download.write_thumbnail"),
        write_info_json=_bool(
            raw.get("write_info_json", False),
            "download.write_info_json",
        ),
        audio_format=_str(raw.get("audio_format", "original"), "download.audio_format"),
        subtitle_langs=_str_tuple(raw.get("subtitle_langs", ()), "download.subtitle_langs"),
        collision=collision,
    )


def _parse_search(raw: Mapping[str, Any]) -> SearchConfig:
    known = {"loaders", "limit"}
    for key in raw:
        if key not in known:
            logger.warning("unknown config key search.%s (ignored)", key)
    return SearchConfig(
        loaders=_str_tuple(raw.get("loaders", ()), "search.loaders"),
        limit=_int(raw.get("limit", 10), "search.limit"),
    )


def _parse_sources(raw: Mapping[str, Any]) -> dict[str, SourceProfile]:
    sources: dict[str, SourceProfile] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            raise ConfigError("source names must be strings")
        sources[name] = _parse_source(name, _table(value, f"sources.{name}"))
    return sources


_SOURCE_KEYS = frozenset(
    {
        "name",
        "enabled",
        "cookies",
        "cookies_file",
        "cookies_from_browser",
        "proxy",
        "user_agent",
        "headers",
        "impersonate",
        "extractor_args",
        "retries",
        "socket_timeout",
        "rate_limit",
        "geo_bypass",
        "token",
        "extra",
    }
)


def _parse_source(name: str, raw: Mapping[str, Any]) -> SourceProfile:
    specified: set[str] = set()

    if "cookies" in raw and "cookies_file" in raw:
        raise ConfigError(
            f"sources.{name}: use either 'cookies' or 'cookies_file', not both"
        )
    cookies_value = raw.get("cookies", raw.get("cookies_file", _MISSING))
    cookies_file = None
    if cookies_value is not _MISSING:
        cookies_file = _path_or_none(cookies_value, f"sources.{name}.cookies")
        specified.add("cookies_file")

    cookies_from_browser: tuple[str, ...] | None = None
    if "cookies_from_browser" in raw:
        cookies_from_browser = _browser_cookies(
            raw["cookies_from_browser"],
            f"sources.{name}.cookies_from_browser",
        )
        specified.add("cookies_from_browser")

    if "enabled" in raw:
        specified.add("enabled")
    if "proxy" in raw:
        specified.add("proxy")
    if "user_agent" in raw:
        specified.add("user_agent")
    if "impersonate" in raw:
        specified.add("impersonate")
    if "retries" in raw:
        specified.add("retries")
    if "socket_timeout" in raw:
        specified.add("socket_timeout")
    if "rate_limit" in raw:
        specified.add("rate_limit")
    if "geo_bypass" in raw:
        specified.add("geo_bypass")
    if "token" in raw:
        specified.add("token")

    headers: dict[str, str] = {}
    if "headers" in raw:
        headers = _str_mapping(raw["headers"], f"sources.{name}.headers")
        specified.add("headers")

    extractor_args: dict[str, dict[str, tuple[str, ...]]] = {}
    if "extractor_args" in raw:
        extractor_args = _extractor_args(
            raw["extractor_args"],
            f"sources.{name}.extractor_args",
        )
        specified.add("extractor_args")

    extra: dict[str, object] = {}
    if "extra" in raw:
        extra = dict(_table(raw["extra"], f"sources.{name}.extra"))
        specified.add("extra")

    for key, value in raw.items():
        if key in _SOURCE_KEYS:
            continue
        logger.warning(
            "unknown config key sources.%s.%s (stored in extra)",
            name,
            key,
        )
        extra[key] = value
        specified.add("extra")

    profile = SourceProfile(
        name=name,
        enabled=_bool(raw.get("enabled", True), f"sources.{name}.enabled"),
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=_str_or_none(raw.get("proxy"), f"sources.{name}.proxy"),
        user_agent=_str_or_none(raw.get("user_agent"), f"sources.{name}.user_agent"),
        headers=headers,
        impersonate=_str_or_none(raw.get("impersonate"), f"sources.{name}.impersonate"),
        extractor_args=extractor_args,
        retries=_int(raw.get("retries", 3), f"sources.{name}.retries"),
        socket_timeout=_float(
            raw.get("socket_timeout", 20.0),
            f"sources.{name}.socket_timeout",
        ),
        rate_limit=_str_or_none(raw.get("rate_limit"), f"sources.{name}.rate_limit"),
        geo_bypass=_bool(raw.get("geo_bypass", False), f"sources.{name}.geo_bypass"),
        token=_str_or_none(raw.get("token"), f"sources.{name}.token"),
        extra=extra,
        _specified=frozenset(specified),
    )
    return profile


def _browser_cookies(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{label} must be a string or a list of strings")
        return tuple(value)
    raise ConfigError(f"{label} must be a string or a list of strings")


def _extractor_args(value: object, label: str) -> dict[str, dict[str, tuple[str, ...]]]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a table")
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for extractor, options in value.items():
        if not isinstance(extractor, str) or not isinstance(options, Mapping):
            raise ConfigError(f"{label}.{extractor} must be a table")
        normalized: dict[str, tuple[str, ...]] = {}
        for key, option in options.items():
            if not isinstance(key, str):
                raise ConfigError(f"{label}.{extractor} keys must be strings")
            if isinstance(option, str):
                normalized[key] = (option,)
            elif isinstance(option, (list, tuple)):
                if not all(isinstance(item, str) for item in option):
                    raise ConfigError(
                        f"{label}.{extractor}.{key} must contain strings"
                    )
                normalized[key] = tuple(option)
            elif isinstance(option, (int, float)):
                normalized[key] = (str(option),)
            else:
                raise ConfigError(
                    f"{label}.{extractor}.{key} must be a string or list of strings"
                )
        result[extractor] = normalized
    return result


def _str_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a table")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ConfigError(f"{label} must map strings to strings")
        result[key] = item
    return result


def _path_or_none(value: object, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError(f"{label} must be a path string")
    return Path(value).expanduser()


def _str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    return value


def _str_or_none(value: object, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return _str(value, label)


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer")
    return value


def _float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number")
    return float(value)


def _bool(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _env_bool(value)
    raise ConfigError(f"{label} must be a boolean")


def _str_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{label} must contain strings")
        return tuple(value)
    raise ConfigError(f"{label} must be a string or a list of strings")
