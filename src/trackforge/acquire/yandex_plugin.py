"""Detection helpers for the external ``yt-dlp-yandex-music`` plugin.

The built-in yt-dlp Yandex Music extractor is considered broken: it still
talks to the retired ``handlers/*.jsx`` endpoints.  Yandex support therefore
requires the third-party plugin in the *same* environment as yt-dlp.

``yt-dlp --list-extractors`` does not list plugins, and
``yt_dlp.extractor.gen_extractor_classes()`` may only show built-in
extractor names.  The only reliable checks are the installed distribution
and an actual import of the plugin module.
"""

# pyright: standard

from __future__ import annotations

import importlib
import importlib.metadata
from types import ModuleType

DISTRIBUTION = "yt-dlp-yandex-music"
MIN_VERSION = "2026.9.24"

# The v2 module is the current implementation.  The unsuffixed module is kept
# as a fallback for older plugin releases.
_PLUGIN_MODULES = (
    "yt_dlp_plugins.extractor.yandex_music_v2",
    "yt_dlp_plugins.extractor.yandex_music",
)


def distribution_version() -> str | None:
    """Return the installed plugin version, or ``None`` when absent."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - metadata problems mean "not usable"
        return None


def import_yandex_plugin() -> ModuleType:
    """Import and return the plugin module.

    Raises :class:`ModuleNotFoundError` when neither known module can be
    imported.  Importing is the strongest offline signal that the plugin is
    actually usable: a stale distribution can be present while its module
    fails to load against the installed yt-dlp.
    """
    last_error: ModuleNotFoundError | None = None
    for name in _PLUGIN_MODULES:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            last_error = exc
    raise ModuleNotFoundError(
        f"no importable Yandex Music plugin module ({', '.join(_PLUGIN_MODULES)})",
        name=_PLUGIN_MODULES[0],
    ) from last_error


def plugin_available() -> bool:
    """Whether the distribution is installed and its module imports."""
    if distribution_version() is None:
        return False
    try:
        import_yandex_plugin()
    except Exception:  # noqa: BLE001 - any import failure means "not ready"
        return False
    return True


def plugin_install_hint() -> str:
    """Short, copy-pasteable instruction shown in user-facing errors."""
    return (
        f"install {DISTRIBUTION}>={MIN_VERSION} into the same environment as "
        f"yt-dlp (for example: uv add '{DISTRIBUTION}>={MIN_VERSION}' or "
        f"python -m pip install -U '{DISTRIBUTION}>={MIN_VERSION}')"
    )


__all__ = [
    "DISTRIBUTION",
    "MIN_VERSION",
    "distribution_version",
    "import_yandex_plugin",
    "plugin_available",
    "plugin_install_hint",
]
