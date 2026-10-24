"""Per-site proxy routing used by yt-dlp."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.application.services.proxy_routing import ProxyRoute, ProxyRouter
from trackforge.domain.errors import DomainValidationError

pytestmark = pytest.mark.unit

PROXY = "socks5h://user:pass@proxy.example:1080"


def test_site_route_overrides_component_default() -> None:
    router = ProxyRouter.from_mapping(
        PROXY,
        {
            "music.yandex.ru": "",
            "youtube.com": PROXY,
        },
    )

    assert router.proxy_for("https://www.youtube.com/watch?v=1") == PROXY
    assert router.proxy_for("https://youtu.be/abc") == PROXY
    assert router.proxy_for("https://music.yandex.ru/album/1/track/2") is None
    assert router.proxy_for("https://soundcloud.com/a/b") == PROXY


def test_more_specific_route_wins() -> None:
    router = ProxyRouter.from_mapping(
        None,
        {
            "youtube.com": "http://generic.example:3128",
            "music.youtube.com": "http://music.example:3128",
        },
    )

    assert router.proxy_for("https://music.youtube.com/watch?v=1") == ("http://music.example:3128")
    assert router.proxy_for("https://www.youtube.com/watch?v=1") == ("http://generic.example:3128")


def test_no_route_and_no_default_is_direct() -> None:
    router = ProxyRouter.from_mapping(None, {})

    assert router.proxy_for("https://example.com") is None
    assert router.configured is False


def test_route_with_empty_proxy_is_configured_but_direct() -> None:
    router = ProxyRouter.from_mapping(PROXY, {"music.yandex.ru": ""})

    assert router.proxy_for("https://music.yandex.ru/album/1") is None
    assert router.configured is True


def test_wildcard_suffix_is_normalized() -> None:
    route = ProxyRoute("*.YouTube.com.", PROXY)

    assert route.host_suffix == "youtube.com"
    assert route.matches("www.youtube.com")
    assert route.matches("youtube.com")
    assert not route.matches("notyoutube.com")


@pytest.mark.parametrize(
    "host",
    ["https://youtube.com", "youtube.com:443", "", "you tube.com", "you*tube.com"],
)
def test_invalid_route_host_is_rejected(host: str) -> None:
    with pytest.raises(DomainValidationError):
        ProxyRoute(host)


def test_invalid_proxy_type_is_rejected() -> None:
    with pytest.raises(DomainValidationError):
        ProxyRouter(default=123)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_source_resolver_uses_configured_site_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.adapters.ytdlp.conftest import FAKE_YTDLP, read_log

    from trackforge.adapters.sources.ytdlp.source import YtDlpSource
    from trackforge.bootstrap.container import build_source_resolvers
    from trackforge.config import TrackForgeConfig
    from trackforge.domain.model.source import Url

    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(log))
    config = TrackForgeConfig(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        library_dir=tmp_path / "library",
        ytdlp_binary=str(FAKE_YTDLP),
        ytdlp_proxy="socks5h://default.example:1080",
        ytdlp_site_proxies={
            "youtube.com": "socks5h://youtube.example:1080",
            "music.yandex.ru": "",
        },
    )
    source = next(
        resolver for resolver in build_source_resolvers(config) if isinstance(resolver, YtDlpSource)
    )

    await source.resolve(Url("https://www.youtube.com/watch?v=demo"))
    youtube_args = read_log(log)[-1]
    assert youtube_args[youtube_args.index("--proxy") + 1] == ("socks5h://youtube.example:1080")

    await source.resolve(Url("https://music.yandex.ru/album/1/track/2"))
    yandex_args = read_log(log)[-1]
    assert yandex_args[yandex_args.index("--proxy") + 1] == ""
