"""Configuration errors for TrackForge."""

from __future__ import annotations


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or validated.

    ``key`` identifies the offending configuration key when one is available.
    Values deliberately stay out of the exception text so a malformed config
    cannot leak secrets into logs or terminal output.
    """

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key
