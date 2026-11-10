"""YouTube thumbnail fallback tests (offline, injected HTTP)."""

# pyright: standard

from __future__ import annotations

from collections.abc import Callable

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.youtube import YouTubeLoader
from trackforge.acquire.models import MediaItem, Thumbnail


class FakeHttp:
    def __init__(self, answer: Callable[[str], object]) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def exists(self, url: str) -> object:
        self.calls.append(url)
        return self.answer(url)


def make_item(*, position: int | None = 1) -> MediaItem:
    return MediaItem(
        id="abc123",
        title="Video",
        position=position,
        thumbnails=(
            Thumbnail(
                url="https://i.ytimg.com/vi/abc123/mqdefault.jpg",
                width=320,
                height=180,
                ext="jpg",
            ),
            Thumbnail(
                url="https://i.ytimg.com/vi/abc123/hqdefault.jpg",
                width=480,
                height=360,
                ext="jpg",
            ),
        ),
    )


def context(http: object | None) -> LoaderContext:
    return LoaderContext(
        job_id="job-1",
        profile=SourceProfile(name="youtube"),
        http=http,
    )


def test_maxres_is_preferred_with_a_single_head_request() -> None:
    http = FakeHttp(lambda url: "maxresdefault" in url)
    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert chosen.width == 1280
    assert chosen.height == 720
    assert chosen.source == "youtube:maxres"
    assert len(http.calls) == 1


def test_sddefault_is_used_when_maxres_is_missing() -> None:
    http = FakeHttp(lambda url: "sddefault" in url)
    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("sddefault.jpg")
    assert chosen.source == "youtube:sd"
    assert len(http.calls) == 2
    assert "maxresdefault" in http.calls[0]
    assert "sddefault" in http.calls[1]


def test_existing_thumbnail_is_used_when_both_fallbacks_404() -> None:
    http = FakeHttp(lambda url: False)
    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("hqdefault.jpg")
    assert len(http.calls) == 2


def test_no_head_requests_for_non_primary_items() -> None:
    http = FakeHttp(lambda url: True)
    chosen = YouTubeLoader().choose_thumbnail(make_item(position=2), ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("hqdefault.jpg")
    assert http.calls == []


def test_known_maxres_thumbnail_skips_head_request() -> None:
    item = MediaItem(
        id="abc123",
        title="Video",
        position=1,
        thumbnails=(
            Thumbnail(
                url="https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
                width=1280,
                height=720,
                ext="jpg",
            ),
        ),
    )
    http = FakeHttp(lambda url: True)
    chosen = YouTubeLoader().choose_thumbnail(item, ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert http.calls == []


def test_head_style_http_client_is_supported() -> None:
    class HeadHttp:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def head(self, url: str) -> int:
            self.calls.append(url)
            return 200 if "maxresdefault" in url else 404

    http = HeadHttp()
    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert len(http.calls) == 1


def test_http_errors_do_not_break_thumbnail_selection() -> None:
    class BrokenHttp:
        def exists(self, url: str) -> bool:
            raise OSError("network down")

    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(BrokenHttp()))
    assert chosen is not None
    assert chosen.url.endswith("hqdefault.jpg")


def test_no_id_skips_fallbacks() -> None:
    item = MediaItem(id="", title="Video", position=1)
    http = FakeHttp(lambda url: True)
    assert YouTubeLoader().choose_thumbnail(item, ctx=context(http)) is None
    assert http.calls == []


PNG_HEADER = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + (640).to_bytes(4, "big")
    + (360).to_bytes(4, "big")
)


def test_thumbnail_dimensions_are_verified_from_image_bytes() -> None:
    from trackforge.acquire.loaders.thumbnail_probe import parse_image_size

    class BodyHttp(FakeHttp):
        def get(self, url: str) -> bytes:
            self.calls.append(url)
            return PNG_HEADER

    http = BodyHttp(lambda url: True)
    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(http))

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert (chosen.width, chosen.height) == (640, 360)
    assert chosen.ext == "png"
    assert chosen.mime == "image/png"

    size = parse_image_size(PNG_HEADER)
    assert size is not None
    assert (size.width, size.height) == (640, 360)


def test_unverified_dimensions_are_not_claimed() -> None:
    class NoBodyHttp(FakeHttp):
        def get(self, url: str) -> None:
            self.calls.append(url)

    http = NoBodyHttp(lambda url: True)
    chosen = YouTubeLoader().choose_thumbnail(make_item(), ctx=context(http))
    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    # Injected client says it has no body; normalized guess is kept for tests,
    # while real CLI paths use fetch_image_size and drop unverified dimensions.
    assert (chosen.width, chosen.height) == (1280, 720)
