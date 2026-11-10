"""Build yt-dlp options from a :class:`SourceProfile`.

This module is the L1.0 boundary between configuration and a real
``YoutubeDL`` instance.  It resolves the proxy, passes the configured cookie
file to yt-dlp directly (yt-dlp may refresh it in place), and never writes
cookies or proxy credentials into logs.
"""

# pyright: standard

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..auth import ensure_auth_ready, resolve_proxy, validate_cookie_file
from ..config import SourceProfile
from ..redaction import RedactingLoggerAdapter
from ..staging import StagingArea

_LOGGER = logging.getLogger("trackforge.acquire.yt_dlp")


def deno_js_runtimes() -> dict[str, dict[str, str]]:
    """Return yt-dlp's ``js_runtimes`` mapping for the bundled Deno.

    A missing Deno package is not fatal for every extractor, so the option is
    simply omitted; ``doctor`` reports it as a failure.
    """
    try:
        import deno
    except ImportError:
        return {}
    try:
        binary = deno.find_deno_bin()
    except Exception:  # noqa: BLE001 - optional runtime must not break options
        return {}
    if not binary:
        return {}
    return {"deno": {"path": str(binary)}}


def _is_sensitive_header(name: str) -> bool:
    lowered = name.lower()
    return lowered in {"authorization", "cookie", "proxy-authorization"} or "token" in lowered


def _logger_for(
    profile: SourceProfile,
    logger: logging.Logger | None,
    resolved_proxy: str | None,
) -> RedactingLoggerAdapter:
    secrets = [profile.proxy or "", resolved_proxy or "", profile.token or ""]
    for name, value in profile.headers.items():
        if _is_sensitive_header(name):
            secrets.append(str(value))
    return RedactingLoggerAdapter(
        logger or _LOGGER,
        [secret for secret in secrets if secret],
    )


def _impersonate_target(value: str) -> object:
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return ImpersonateTarget.from_str(value)
    except (ImportError, AttributeError, ValueError):
        return value


def build_ydl_options(
    profile: SourceProfile,
    *,
    cookiefile: str | Path | None = None,
    logger: logging.Logger | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the shared ``YoutubeDL`` options for one source profile.

    The configured cookie file is passed to yt-dlp directly so yt-dlp can
    refresh it in place.  An explicit ``cookiefile`` still wins for callers
    that manage their own path.
    """
    resolved_proxy = resolve_proxy(profile.proxy, env=env, label=f"sources.{profile.name}.proxy")
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
        "socket_timeout": profile.socket_timeout,
        "retries": profile.retries,
        "fragment_retries": profile.retries,
        "extractor_retries": profile.retries,
        "cachedir": False,
        "remote_components": [],
        "logger": _logger_for(profile, logger, resolved_proxy),
    }

    js_runtimes = deno_js_runtimes()
    if js_runtimes:
        options["js_runtimes"] = js_runtimes

    selected_cookie_file: Path | None = None
    if cookiefile is not None:
        selected_cookie_file = Path(cookiefile).expanduser()
    elif profile.cookies_file is not None:
        selected_cookie_file = Path(profile.cookies_file).expanduser()

    if selected_cookie_file is not None:
        validate_cookie_file(selected_cookie_file, require_private=True)
        options["cookiefile"] = str(selected_cookie_file)
    elif profile.cookies_from_browser:
        options["cookiesfrombrowser"] = tuple(profile.cookies_from_browser)

    if resolved_proxy is not None:
        options["proxy"] = resolved_proxy

    if profile.impersonate:
        options["impersonate"] = _impersonate_target(profile.impersonate)
    if profile.extractor_args:
        options["extractor_args"] = {
            extractor: {key: list(values) for key, values in values_by_key.items()}
            for extractor, values_by_key in profile.extractor_args.items()
        }
    if profile.headers or profile.user_agent:
        headers = dict(profile.headers)
        if profile.user_agent and not any(
            key.lower() == "user-agent" for key in headers
        ):
            headers["User-Agent"] = profile.user_agent
        options["http_headers"] = headers
    if profile.rate_limit:
        options["ratelimit"] = profile.rate_limit
    if profile.geo_bypass:
        options["geo_bypass"] = True
    return options


def prepare_ydl_options(
    profile: SourceProfile,
    *,
    staging: StagingArea | str | Path | None = None,
    job_id: str | None = None,
    logger: logging.Logger | None = None,
    env: Mapping[str, str] | None = None,
    cookies_required: bool = False,
) -> dict[str, Any]:
    """Validate auth and build yt-dlp options using the configured cookies.

    ``staging``/``job_id`` are accepted for callers that also prepare a job
    directory, but the cookie file is intentionally not copied.  yt-dlp owns
    the configured file and may refresh it in place.
    """
    del staging, job_id
    ensure_auth_ready(
        profile,
        source=profile.name,
        cookies_required=cookies_required,
    )
    return build_ydl_options(
        profile,
        logger=logger,
        env=env,
    )


def create_youtube_dl(
    options: Mapping[str, Any],
) -> Any:
    """Create a ``YoutubeDL`` instance (kept lazy so imports stay offline)."""
    from yt_dlp import YoutubeDL

    # yt-dlp's stub exposes a private TypedDict; the values are runtime-checked
    # by yt-dlp itself and are assembled by build_ydl_options above.
    params: Any = dict(options)
    return YoutubeDL(params)
