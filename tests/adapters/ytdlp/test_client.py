"""YtDlpClient subprocess contract tests using a fake executable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import YOUTUBE_URL, read_log

from trackforge.adapters.sources.ytdlp.client import (
    DownloadOptions,
    DumpOptions,
    YtDlpClient,
)
from trackforge.domain.errors import (
    CookiesPermissionsError,
    DownloaderNotFound,
    DownloadTimeout,
    ExtractorBroken,
)

pytestmark = pytest.mark.unit


def test_version_and_dump_json(fake_client: YtDlpClient, fake_log: Path) -> None:
    assert fake_client.version() == "2026.10.21"
    document = fake_client.dump_json(YOUTUBE_URL)
    assert document["id"] == "fixture-yt-1"
    assert document["title"].startswith("Fixture YouTube Song")
    calls = read_log(fake_log)
    assert calls
    dump_args = calls[-1]
    assert "--dump-single-json" in dump_args
    assert "--no-download" in dump_args
    assert "--no-playlist" in dump_args
    assert dump_args[-1] == YOUTUBE_URL


def test_shell_metacharacters_stay_one_argument(
    fake_client: YtDlpClient,
    fake_log: Path,
) -> None:
    url = "https://www.youtube.com/watch?v=demo; rm -rf /tmp/trackforge-never"
    fake_client.dump_json(url)
    args = read_log(fake_log)[-1]
    assert args.count(url) == 1
    assert "rm" not in args
    assert "-rf" not in args


def test_invalid_json_is_extractor_broken(fake_client: YtDlpClient) -> None:
    with pytest.raises(ExtractorBroken):
        fake_client.dump_json("https://www.youtube.com/watch?v=invalidjson")


def test_timeout_is_typed_and_retryable(tmp_path: Path, fake_path: Path) -> None:
    client = YtDlpClient(
        str(fake_path),
        timeout_metadata=0.2,
        timeout_download=0.2,
        env={"TRACKFORGE_FAKE_YTDLP_SLEEP": "2"},
    )
    with pytest.raises(DownloadTimeout) as exc_info:
        client.dump_json("https://www.youtube.com/watch?v=slow")
    assert exc_info.value.retryable is True


def test_missing_binary_is_typed(tmp_path: Path) -> None:
    client = YtDlpClient(str(tmp_path / "does-not-exist"))
    with pytest.raises(DownloaderNotFound):
        client.version()


def test_cookie_file_permissions_are_enforced(
    tmp_path: Path,
    fake_client: YtDlpClient,
) -> None:
    cookie = tmp_path / "cookies" / "youtube.com.txt"
    cookie.parent.mkdir()
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie.chmod(0o644)
    with pytest.raises(CookiesPermissionsError):
        fake_client.dump_json(
            YOUTUBE_URL,
            DumpOptions(cookies_path=cookie),
        )


def test_download_returns_file_and_info_json(
    tmp_path: Path,
    fake_client: YtDlpClient,
) -> None:
    result = fake_client.download(
        YOUTUBE_URL,
        None,
        tmp_path / "download",
        DownloadOptions(format_id="140"),
    )
    assert result.path.is_file()
    assert result.path.suffix == ".m4a"
    assert result.container == "m4a"
    assert result.size_bytes == result.path.stat().st_size
    assert result.size_bytes > 0
    assert result.duration == 2.0
    assert result.info_json["id"] == "fixture-yt-1"
    info_sidecar = result.path.with_suffix(".info.json")
    assert info_sidecar.is_file()
    assert json.loads(info_sidecar.read_text(encoding="utf-8"))["id"] == "fixture-yt-1"


def test_webm_audio_is_not_mistaken_for_video(
    tmp_path: Path,
    fake_client: YtDlpClient,
) -> None:
    result = fake_client.download(
        YOUTUBE_URL,
        None,
        tmp_path / "download",
        DownloadOptions(format_id="251"),
    )
    assert result.path.suffix == ".webm"
    assert result.container == "webm"
    assert result.size_bytes > 0


def test_progress_callback_receives_events(
    tmp_path: Path,
    fake_client: YtDlpClient,
) -> None:
    events = []
    fake_client.download(
        YOUTUBE_URL,
        None,
        tmp_path / "download",
        DownloadOptions(format_id="140", progress=events.append),
    )
    assert events
    assert events[-1].percent == 1.0


def test_site_proxy_and_direct_route_are_passed_to_ytdlp(
    tmp_path: Path,
    fake_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trackforge.application.services.proxy_routing import ProxyRouter

    log = tmp_path / "proxy-argv.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(log))
    router = ProxyRouter.from_mapping(
        None,
        {
            "youtube.com": "socks5h://user:pass@proxy.example:1080",
            "music.yandex.ru": "",
        },
    )
    client = YtDlpClient(str(fake_path), proxy_selector=router.proxy_for)

    client.dump_json(YOUTUBE_URL)
    youtube_args = read_log(log)[-1]
    assert youtube_args[youtube_args.index("--proxy") + 1] == (
        "socks5h://user:pass@proxy.example:1080"
    )

    client.dump_json("https://music.yandex.ru/album/1/track/2")
    yandex_args = read_log(log)[-1]
    assert yandex_args[yandex_args.index("--proxy") + 1] == ""


def test_ambient_proxy_environment_is_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "yt-dlp-env"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "print(json.dumps({k: v for k, v in os.environ.items() if 'proxy' in k.casefold()}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.example:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://ambient.example:1080")
    monkeypatch.setenv("https_proxy", "http://ambient.example:3128")
    client = YtDlpClient(str(script))

    assert client.dump_json("https://www.youtube.com/watch?v=env") == {}


def test_dash_proxy_direct_is_default_without_router(
    fake_client: YtDlpClient,
    fake_log: Path,
) -> None:
    fake_client.dump_json(YOUTUBE_URL)
    args = read_log(fake_log)[-1]
    assert args[args.index("--proxy") + 1] == ""
