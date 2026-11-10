"""Offline-first diagnostics for the acquisition layer.

``run_doctor`` performs only local checks by default.  Network access is
opt-in via ``check_network=True`` and is isolated in :meth:`Doctor._probe_network`
so it can be replaced in tests.
"""

# pyright: standard

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shutil
import ssl
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .auth import inspect_cookie_file, resolve_proxy, validate_cookie_file
from .config import AppConfig, SourceProfile
from .errors import ConfigError
from .redaction import redact_proxy, redact_text

_YTDLP_DIST = "yt-dlp"
_EJS_DIST = "yt-dlp-ejs"
_YANDEX_DIST = "yt-dlp-yandex-music"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    source: str
    checks: tuple[DoctorCheck, ...]

    @property
    def failed(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.FAIL)

    @property
    def warnings(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.WARN)

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status.value,
                    "message": check.message,
                }
                for check in self.checks
            ],
        }


@dataclass(frozen=True, slots=True)
class NetworkProbeResult:
    """Safe summary of one opt-in network probe."""

    proxy_ok: bool
    proxy_message: str
    cookies_status: CheckStatus = CheckStatus.SKIP
    cookies_message: str = "cookies were not checked"


NetworkProbe = Callable[[SourceProfile, str | None, float], NetworkProbeResult]


@dataclass(slots=True)
class Doctor:
    config: AppConfig
    source: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    check_network: bool = False
    timeout: float = 10.0
    probe: NetworkProbe | None = None

    def run(self) -> DoctorReport:
        profile = self.config.profile_for(self.source)
        checks = [
            self._check_config(),
            self._check_cookies(profile),
            self._check_ytdlp(),
            self._check_ejs(),
            self._check_deno(),
            self._check_ffmpeg(),
            self._check_yandex_plugin(),
        ]
        proxy_check, proxy = self._check_proxy(profile)
        checks.append(proxy_check)
        checks.extend(self._check_network(profile, proxy))
        return DoctorReport(source=self.source, checks=tuple(checks))

    # -- offline checks -------------------------------------------------

    def _check_config(self) -> DoctorCheck:
        if self.source in self.config.sources:
            return DoctorCheck(
                "config",
                CheckStatus.OK,
                f"[sources.{self.source}] loaded",
            )
        if "default" in self.config.sources:
            return DoctorCheck(
                "config",
                CheckStatus.OK,
                f"no [sources.{self.source}]; [sources.default] inherited",
            )
        return DoctorCheck(
            "config",
            CheckStatus.WARN,
            f"no [sources.{self.source}]; built-in defaults are used",
        )

    def _check_cookies(self, profile: SourceProfile) -> DoctorCheck:
        if profile.cookies_file is not None:
            result = inspect_cookie_file(profile.cookies_file)
            if not result.exists:
                return DoctorCheck(
                    "cookies",
                    CheckStatus.FAIL,
                    f"file not found: {result.path}",
                )
            if not result.is_file:
                return DoctorCheck(
                    "cookies",
                    CheckStatus.FAIL,
                    f"not a regular file: {result.path}",
                )
            if not result.readable:
                return DoctorCheck(
                    "cookies",
                    CheckStatus.FAIL,
                    f"file is not readable: {result.path}",
                )
            mode = f"{result.mode:03o}" if result.mode is not None else "unknown"
            if result.world_readable:
                return DoctorCheck(
                    "cookies",
                    CheckStatus.FAIL,
                    f"world-readable mode {mode}: {result.path}; expected 600",
                )
            if not result.format_ok:
                return DoctorCheck(
                    "cookies",
                    CheckStatus.FAIL,
                    f"invalid Netscape format: {result.error or 'unknown error'} "
                    f"({result.path})",
                )
            header = "with header" if result.has_header else "no header"
            return DoctorCheck(
                "cookies",
                CheckStatus.OK,
                f"{result.data_lines} records, mode {mode}, {header}",
            )

        if profile.cookies_from_browser:
            browsers = ", ".join(profile.cookies_from_browser)
            return DoctorCheck(
                "cookies",
                CheckStatus.OK,
                f"browser cookies configured: {browsers} (verified on --check-network)",
            )

        if self.source == "youtube":
            return DoctorCheck(
                "cookies",
                CheckStatus.FAIL,
                "no cookies configured for YouTube; add "
                "[sources.youtube].cookies or cookies_from_browser",
            )
        return DoctorCheck(
            "cookies",
            CheckStatus.WARN,
            "no cookies configured for this source",
        )

    def _check_ytdlp(self) -> DoctorCheck:
        try:
            module = importlib.import_module("yt_dlp")
            version = getattr(getattr(module, "version", None), "__version__", None)
            version = version or _distribution_version(_YTDLP_DIST)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            return DoctorCheck(
                "yt-dlp",
                CheckStatus.FAIL,
                f"import failed: {redact_text(str(exc))}",
            )
        return DoctorCheck("yt-dlp", CheckStatus.OK, f"version {version}")

    def _check_ejs(self) -> DoctorCheck:
        try:
            importlib.import_module("yt_dlp_ejs")
            version = _distribution_version(_EJS_DIST)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            return DoctorCheck(
                "yt-dlp-ejs",
                CheckStatus.FAIL,
                f"import failed: {redact_text(str(exc))}",
            )
        return DoctorCheck("yt-dlp-ejs", CheckStatus.OK, f"version {version}")

    def _check_deno(self) -> DoctorCheck:
        try:
            import deno
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            return DoctorCheck(
                "deno",
                CheckStatus.FAIL,
                f"import failed: {redact_text(str(exc))}",
            )
        finder = getattr(deno, "find_deno_bin", None)
        if not callable(finder):
            return DoctorCheck(
                "deno",
                CheckStatus.FAIL,
                "deno.find_deno_bin() is missing",
            )
        try:
            binary = finder()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            return DoctorCheck(
                "deno",
                CheckStatus.FAIL,
                f"find_deno_bin() failed: {redact_text(str(exc))}",
            )
        if not binary:
            return DoctorCheck("deno", CheckStatus.FAIL, "find_deno_bin() returned no path")
        binary_path = Path(str(binary))
        if not binary_path.exists():
            return DoctorCheck(
                "deno",
                CheckStatus.FAIL,
                f"binary not found: {binary_path}",
            )
        try:
            completed = subprocess.run(
                [str(binary_path), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return DoctorCheck(
                "deno",
                CheckStatus.FAIL,
                f"cannot run {binary_path}: {redact_text(str(exc))}",
            )
        if completed.returncode != 0:
            return DoctorCheck(
                "deno",
                CheckStatus.FAIL,
                f"{binary_path} exited with {completed.returncode}",
            )
        output = (completed.stdout or completed.stderr).strip().splitlines()
        version = output[0] if output else "version unknown"
        return DoctorCheck("deno", CheckStatus.OK, f"{version} ({binary_path})")

    def _check_ffmpeg(self) -> DoctorCheck:
        missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
        if missing:
            return DoctorCheck(
                "ffmpeg",
                CheckStatus.WARN,
                f"not found: {', '.join(missing)} (needed for L2, not for inspect)",
            )
        return DoctorCheck("ffmpeg", CheckStatus.OK, "ffmpeg and ffprobe found")

    def _check_yandex_plugin(self) -> DoctorCheck:
        try:
            version = _distribution_version(_YANDEX_DIST)
        except importlib.metadata.PackageNotFoundError:
            return DoctorCheck(
                "yt-dlp-yandex-music",
                CheckStatus.WARN,
                "plugin is not installed (Yandex Music will be unavailable)",
            )
        try:
            _import_yandex_plugin()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            return DoctorCheck(
                "yt-dlp-yandex-music",
                CheckStatus.FAIL,
                f"installed {version} but import failed: {redact_text(str(exc))}",
            )
        return DoctorCheck(
            "yt-dlp-yandex-music",
            CheckStatus.OK,
            f"version {version}",
        )

    def _check_proxy(
        self,
        profile: SourceProfile,
    ) -> tuple[DoctorCheck, str | None]:
        try:
            resolved = resolve_proxy(
                profile.proxy,
                env=self.env,
                label=f"sources.{profile.name}.proxy",
            )
        except ConfigError as exc:
            return (
                DoctorCheck("proxy", CheckStatus.FAIL, redact_text(str(exc))),
                None,
            )

        configured = (profile.proxy or "").strip().lower()
        if configured == "none":
            return (
                DoctorCheck("proxy", CheckStatus.OK, "explicitly disabled (none)"),
                None,
            )
        if configured == "env" and resolved is None:
            return (
                DoctorCheck(
                    "proxy",
                    CheckStatus.WARN,
                    "proxy = \"env\" but HTTPS_PROXY/HTTP_PROXY/ALL_PROXY is not set; "
                    "using a direct connection",
                ),
                None,
            )
        if resolved is None:
            return (
                DoctorCheck("proxy", CheckStatus.OK, "direct connection (no proxy)"),
                None,
            )
        return (
            DoctorCheck(
                "proxy",
                CheckStatus.OK,
                f"configured: {redact_proxy(resolved)}",
            ),
            resolved,
        )

    # -- opt-in network check -------------------------------------------

    def _check_network(
        self,
        profile: SourceProfile,
        proxy: str | None,
    ) -> list[DoctorCheck]:
        if not self.check_network:
            return [
                DoctorCheck(
                    "network",
                    CheckStatus.SKIP,
                    "not requested (use --check-network)",
                )
            ]
        configured = (profile.proxy or "").strip().lower()
        if configured not in {"", "none", "env"} and proxy is None:
            return [
                DoctorCheck(
                    "network",
                    CheckStatus.FAIL,
                    "not attempted: the configured proxy is invalid",
                )
            ]
        probe = self.probe or self._probe_network
        try:
            result = probe(profile, proxy, self.timeout)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            return [
                DoctorCheck(
                    "network",
                    CheckStatus.FAIL,
                    f"network check failed: {redact_text(str(exc))}",
                )
            ]
        proxy_status = CheckStatus.OK if result.proxy_ok else CheckStatus.FAIL
        return [
            DoctorCheck("network.proxy", proxy_status, redact_text(result.proxy_message)),
            DoctorCheck(
                "network.cookies",
                result.cookies_status,
                redact_text(result.cookies_message),
            ),
        ]

    def _probe_network(
        self,
        profile: SourceProfile,
        proxy: str | None,
        timeout: float,
    ) -> NetworkProbeResult:
        cookie_header = ""
        cookie_file = profile.cookies_file
        if cookie_file is not None:
            try:
                validate_cookie_file(cookie_file, require_private=True)
            except Exception as exc:  # noqa: BLE001 - probe boundary
                return NetworkProbeResult(
                    proxy_ok=False,
                    proxy_message=f"proxy not checked: {redact_text(str(exc))}",
                    cookies_status=CheckStatus.FAIL,
                    cookies_message="cookies are not usable; refresh the file",
                )
            cookie_header = _cookie_header(cookie_file)
            target = "https://www.youtube.com/feed/subscriptions"
        else:
            target = "https://www.youtube.com/robots.txt"

        opener = _build_opener(proxy)
        request = Request(
            target,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "en-US,en;q=0.8",
                **({"Cookie": cookie_header} if cookie_header else {}),
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                final_url = response.geturl()
                status = getattr(response, "status", 200)
                body = response.read(64_000).decode("utf-8", errors="replace")
        except HTTPError as exc:
            final_url = exc.geturl()
            status = exc.code
            try:
                body = exc.read(64_000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - diagnostic boundary
                body = ""
        except (URLError, OSError, ValueError, UnicodeError) as exc:
            reason = getattr(exc, "reason", exc)
            return NetworkProbeResult(
                proxy_ok=False,
                proxy_message=(
                    f"connection failed via {redact_proxy(proxy)}: "
                    f"{redact_text(str(reason))}"
                ),
                cookies_status=CheckStatus.SKIP,
                cookies_message="cookies were not checked because the request failed",
            )

        proxy_ok = 100 <= status < 600
        if proxy is None:
            proxy_message = "direct connection works"
        else:
            proxy_message = f"proxy works: {redact_proxy(proxy)}"

        if cookie_file is None:
            if profile.cookies_from_browser:
                cookies_status = CheckStatus.SKIP
                cookies_message = (
                    "browser cookies cannot be inspected offline; yt-dlp will read them"
                )
            else:
                cookies_status = CheckStatus.SKIP
                cookies_message = "no cookies configured; public access only"
            return NetworkProbeResult(proxy_ok, proxy_message, cookies_status, cookies_message)

        accepted = _cookies_accepted(final_url, body)
        if status >= 400:
            return NetworkProbeResult(
                proxy_ok,
                proxy_message,
                CheckStatus.FAIL,
                f"YouTube returned HTTP {status}; cookies were not confirmed",
            )
        if accepted:
            return NetworkProbeResult(
                proxy_ok,
                proxy_message,
                CheckStatus.OK,
                "cookies accepted by YouTube",
            )
        return NetworkProbeResult(
            proxy_ok,
            proxy_message,
            CheckStatus.FAIL,
            "cookies were not accepted (login or consent page); refresh the file",
        )


def run_doctor(
    config: AppConfig,
    source: str = "youtube",
    *,
    env: Mapping[str, str] | None = None,
    check_network: bool = False,
    timeout: float = 10.0,
    probe: NetworkProbe | None = None,
) -> DoctorReport:
    """Run offline checks, plus an optional network probe."""
    return Doctor(
        config=config,
        source=source,
        env=dict(os.environ if env is None else env),
        check_network=check_network,
        timeout=timeout,
        probe=probe,
    ).run()


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _import_yandex_plugin() -> None:
    try:
        importlib.import_module("yt_dlp_plugins.extractor.yandex_music_v2")
        return
    except ModuleNotFoundError:
        pass
    importlib.import_module("yt_dlp_plugins.extractor.yandex_music")


def _build_opener(proxy: str | None):
    proxy_handler = ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    context = ssl.create_default_context()
    return build_opener(proxy_handler, _HTTPSHandler(context))


def _HTTPSHandler(context: ssl.SSLContext):
    from urllib.request import HTTPSHandler

    return HTTPSHandler(context=context)


def _cookie_header(path: Path) -> str:
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies: list[str] = []
    for cookie in jar:
        if _cookie_matches_youtube(cookie):
            cookies.append(f"{cookie.name}={cookie.value}")
    return "; ".join(cookies)


def _cookie_matches_youtube(cookie: Cookie) -> bool:
    domain = cookie.domain.lstrip(".").lower()
    return domain == "youtube.com" or domain.endswith(".youtube.com") or (
        domain == "google.com" or domain.endswith(".google.com")
    )


def _cookies_accepted(final_url: str, body: str) -> bool:
    try:
        host = (urlsplit(final_url).hostname or "").lower()
    except ValueError:
        host = ""
    if host in {"accounts.google.com", "consent.youtube.com"}:
        return False
    if host.endswith((".accounts.google.com", ".consent.youtube.com")):
        return False
    lowered = body.lower()
    compact = lowered.replace(" ", "")
    # YouTube includes the service-login URL in a logged-in page as well, so
    # that string alone is not evidence of a signed-out session.
    if '"logged_in":true' in compact or '"is_signed_in":true' in compact:
        return True
    markers = (
        "sign in to youtube",
        "consent.youtube.com",
        "before you continue to youtube",
    )
    return not any(marker in lowered for marker in markers)
