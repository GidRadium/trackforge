"""Automatic loader selection and profile binding."""

# pyright: standard

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .config import AppConfig, SourceProfile
from .errors import ConfigError, UnsupportedUrl
from .loaders.base import MediaLoader


@dataclass(frozen=True, slots=True)
class ResolvedLoader:
    """A loader together with the merged profile it should run with."""

    loader: MediaLoader
    profile: SourceProfile


class LoaderRegistry:
    """Pick the first ready loader that supports a URL.

    Loaders are sorted by :attr:`MediaLoader.priority` (smaller first).  The
    generic ``yt-dlp`` loader uses a high priority so it never shadows a
    site-specific adapter.
    """

    def __init__(
        self,
        loaders: Iterable[MediaLoader],
        config: AppConfig | None = None,
    ) -> None:
        self._loaders = sorted(loaders, key=lambda loader: loader.priority)
        self._config = config or AppConfig()

    @property
    def loaders(self) -> tuple[MediaLoader, ...]:
        return tuple(self._loaders)

    @property
    def config(self) -> AppConfig:
        return self._config

    def resolve(self, url: str, *, source: str | None = None) -> ResolvedLoader:
        """Resolve ``url`` to a loader and its merged source profile.

        With ``source``, that loader is used as an explicit override; its
        support and readiness are still checked.  Without it, loaders are
        tried in priority order and disabled/unready ones are skipped.
        """
        if source is not None:
            loader = self._lookup(source)
            profile = self._config.profile_for(loader.profile_name())
            if not profile.enabled:
                raise ConfigError(f"loader {loader.name!r} is disabled")
            if not loader.is_ready(profile):
                error = loader.unready_error(profile)
                if error is not None:
                    raise error
                raise ConfigError(f"loader {loader.name!r} is not configured")
            if not loader.supports_url(url):
                raise UnsupportedUrl(url, source=source)
            return ResolvedLoader(loader=loader, profile=profile)

        for loader in self._loaders:
            profile = self._config.profile_for(loader.profile_name())
            if not profile.enabled:
                continue
            if not loader.is_ready(profile):
                # A site-specific loader may forbid falling through to the
                # generic yt-dlp extractor (for example a missing Yandex
                # plugin must not silently use the broken built-in one).
                if loader.supports_url(url):
                    error = loader.unready_error(profile)
                    if error is not None:
                        raise error
                continue
            if loader.supports_url(url):
                return ResolvedLoader(loader=loader, profile=profile)
        raise UnsupportedUrl(url)

    def by_name(self, name: str) -> MediaLoader:
        """Return a loader by its canonical ``name``."""
        for loader in self._loaders:
            if loader.name == name:
                return loader
        raise ConfigError(f"unknown loader: {name!r}")

    def by_profile_name(self, name: str) -> MediaLoader:
        """Return a loader by its config section name (``profile_name``)."""
        for loader in self._loaders:
            if loader.profile_name() == name:
                return loader
        raise ConfigError(f"unknown loader profile: {name!r}")

    def _lookup(self, source: str) -> MediaLoader:
        try:
            return self.by_name(source)
        except ConfigError:
            return self.by_profile_name(source)


def default_registry(config: AppConfig | None = None) -> LoaderRegistry:
    """Build the built-in registry.

    Site-specific loaders run first; the generic yt-dlp adapter remains the
    final fallback (and the only one that accepts arbitrary http(s) URLs).
    """
    from .loaders.soundcloud import SoundCloudLoader
    from .loaders.yandex_music import YandexMusicLoader
    from .loaders.youtube import YouTubeLoader
    from .loaders.ytdlp import YtDlpLoader

    return LoaderRegistry(
        [
            YouTubeLoader(),
            YandexMusicLoader(),
            SoundCloudLoader(),
            YtDlpLoader(),
        ],
        config,
    )
