"""Tests for grouping yt-dlp formats into user-facing variants."""

# pyright: standard

from __future__ import annotations

from trackforge.acquire.loaders.base import default_recommendations
from trackforge.acquire.loaders.ytdlp_mapper import (
    best_audio_variant,
    best_video_variant,
    build_variants,
)
from trackforge.acquire.models import (
    MediaItem,
    MediaKind,
    Recommendations,
    StreamRole,
)


def video_format(
    format_id: str = "137",
    *,
    ext: str = "mp4",
    vcodec: str = "avc1.640028",
    height: int = 1080,
    fps: float = 30,
    **extra: object,
) -> dict[str, object]:
    return {
        "format_id": format_id,
        "ext": ext,
        "vcodec": vcodec,
        "acodec": "none",
        "height": height,
        "width": round(height * 16 / 9),
        "fps": fps,
        "url": f"https://media.example.test/{format_id}.{ext}",
        **extra,
    }


def audio_format(
    format_id: str = "140",
    *,
    ext: str = "m4a",
    acodec: str = "mp4a.40.2",
    abr: float = 128,
    language: str | None = None,
    **extra: object,
) -> dict[str, object]:
    fmt: dict[str, object] = {
        "format_id": format_id,
        "ext": ext,
        "vcodec": "none",
        "acodec": acodec,
        "abr": abr,
        "url": f"https://media.example.test/{format_id}.{ext}",
        **extra,
    }
    if language is not None:
        fmt["language"] = language
    return fmt


def progressive_format(
    format_id: str = "22",
    *,
    ext: str = "mp4",
    height: int = 720,
    **extra: object,
) -> dict[str, object]:
    return {
        "format_id": format_id,
        "ext": ext,
        "vcodec": "avc1.64001F",
        "acodec": "mp4a.40.2",
        "height": height,
        "width": round(height * 16 / 9),
        "url": f"https://media.example.test/{format_id}.{ext}",
        **extra,
    }


def test_video_and_audio_are_paired_by_compatible_container() -> None:
    item = {
        "id": "x",
        "title": "x",
        "formats": [
            video_format("137", filesize=48_000_000),
            audio_format("140", filesize=4_100_000),
            audio_format(
                "251",
                ext="webm",
                acodec="opus",
                abr=160,
                filesize=4_500_000,
            ),
        ],
    }

    variants = build_variants(item)
    video = next(v for v in variants if v.id == "1080p-mp4")

    assert video.kind is MediaKind.AUDIO_VIDEO
    assert video.label == "1080p MP4"
    assert [stream.format_id for stream in video.streams] == ["137", "140"]
    assert [stream.role for stream in video.streams] == [
        StreamRole.VIDEO,
        StreamRole.AUDIO,
    ]
    assert video.selector is not None
    assert video.selector.startswith("137+140/")
    assert "bv*[height=1080]+ba" in video.selector
    assert video.filesize == 52_100_000
    assert video.filesize_approx is None
    assert video.requires_merge is True
    assert video.requires_ffmpeg is True
    assert video.downloadable is True


def test_webm_video_gets_webm_audio() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("248", ext="webm", vcodec="vp9", filesize=45_000_000),
            audio_format("140", filesize=4_100_000),
            audio_format(
                "251",
                ext="webm",
                acodec="opus",
                abr=160,
                filesize=4_500_000,
            ),
        ],
    }
    video = next(v for v in build_variants(item) if v.id == "1080p-webm")
    assert [stream.format_id for stream in video.streams] == ["248", "251"]


def test_multiple_audio_languages_are_exposed_as_separate_variants() -> None:
    item = {
        "id": "x",
        "language": "en-US",
        "formats": [
            video_format("137"),
            audio_format("140", abr=128),
            audio_format("141", abr=256, language="ru"),
            audio_format("142", abr=192, language="en-US"),
        ],
    }
    variants = build_variants(item)
    video_variants = {
        variant.id: variant
        for variant in variants
        if variant.kind is MediaKind.AUDIO_VIDEO
    }

    assert set(video_variants) == {
        "1080p-mp4-en-us",
        "1080p-mp4-ru",
        "1080p-mp4-und",
    }
    assert [
        stream.format_id
        for stream in video_variants["1080p-mp4-en-us"].streams
    ] == ["137", "142"]
    assert [
        stream.format_id
        for stream in video_variants["1080p-mp4-ru"].streams
    ] == ["137", "141"]
    assert "en-US" in video_variants["1080p-mp4-en-us"].label
    assert video_variants["1080p-mp4-en-us"].streams[1].language_preference == 10.0

    audio_ids = {
        variant.id for variant in variants if variant.kind is MediaKind.AUDIO
    }
    assert audio_ids == {
        "audio-m4a-128-und",
        "audio-m4a-192-en-us",
        "audio-m4a-256-ru",
    }


def test_multi_language_recommendation_prefers_original_track() -> None:
    from trackforge.acquire.config import SourceProfile
    from trackforge.acquire.loaders.base import LoaderContext
    from trackforge.acquire.loaders.youtube import YouTubeLoader
    from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
    from trackforge.acquire.models import SourceRef

    item = {
        "id": "x",
        "language": "en",
        "formats": [
            video_format("137"),
            audio_format("141", abr=256, language="ru"),
            audio_format("142", abr=192, language="en-US"),
        ],
    }
    mapped = YtDlpMapper(SourceProfile(name="youtube")).map(
        item,
        SourceRef("youtube", "https://youtu.be/x"),
    ).items[0]
    recommendations = YouTubeLoader().recommend(
        mapped,
        ctx=LoaderContext(job_id="job-1", profile=SourceProfile(name="youtube")),
    )
    assert recommendations.video == "1080p-mp4-en-us"
    assert recommendations.audio == "audio-m4a-192-en-us"


def test_progressive_format_does_not_require_merge() -> None:
    item = {
        "id": "x",
        "formats": [progressive_format(filesize=30_000_000)],
    }
    variant = build_variants(item)[0]

    assert variant.id == "720p-mp4"
    assert variant.label == "720p MP4"
    assert variant.kind is MediaKind.AUDIO_VIDEO
    assert variant.filesize == 30_000_000
    assert variant.requires_merge is False
    assert variant.requires_ffmpeg is False
    assert variant.selector is not None
    assert variant.selector.startswith("22/")
    assert len(variant.streams) == 2
    assert variant.streams[0].height == 720
    assert variant.streams[1].height is None


def test_audio_only_variants_are_labelled_and_sized() -> None:
    item = {
        "id": "x",
        "formats": [
            audio_format("140", abr=128, filesize=4_100_000),
            audio_format(
                "251",
                ext="webm",
                acodec="opus",
                abr=160,
                filesize_approx=4_500_000,
            ),
        ],
    }
    variants = build_variants(item)
    by_id = {variant.id: variant for variant in variants}

    assert set(by_id) == {"audio-m4a-128", "audio-webm-160"}
    assert by_id["audio-m4a-128"].label == "Audio M4A 128k"
    assert by_id["audio-m4a-128"].kind is MediaKind.AUDIO
    assert by_id["audio-m4a-128"].filesize == 4_100_000
    assert by_id["audio-webm-160"].label == "Audio WEBM 160k"
    assert by_id["audio-webm-160"].filesize is None
    assert by_id["audio-webm-160"].filesize_approx == 4_500_000


def test_duplicate_audio_keeps_highest_bitrate() -> None:
    item = {
        "id": "x",
        "formats": [
            audio_format("140", abr=128),
            audio_format("140-hi", abr=256),
        ],
    }
    variants = build_variants(item)
    assert len(variants) == 1
    assert variants[0].streams[0].bitrate_kbps == 256


def test_direct_url_without_formats_becomes_progressive() -> None:
    variants = build_variants(
        {
            "id": "direct",
            "url": "https://example.test/file.mp4",
            "ext": "mp4",
            "height": 720,
        }
    )
    assert len(variants) == 1
    assert variants[0].kind is MediaKind.AUDIO_VIDEO
    assert variants[0].downloadable is True
    assert variants[0].filesize is None
    assert variants[0].filesize_approx is None


def test_direct_audio_url_without_formats() -> None:
    variants = build_variants(
        {
            "id": "direct",
            "url": "https://example.test/file.mp3",
            "ext": "mp3",
            "vcodec": "none",
            "acodec": "mp3",
            "abr": 320,
        }
    )
    assert len(variants) == 1
    assert variants[0].kind is MediaKind.AUDIO
    assert variants[0].id == "audio-mp3-320"


def test_hdr_is_preserved_and_does_not_collapse_with_sdr() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137", dynamic_range="SDR"),
            video_format("337", vcodec="vp09.02.10.10", dynamic_range="HDR10"),
            audio_format("140"),
        ],
    }
    variants = [v for v in build_variants(item) if v.kind is MediaKind.AUDIO_VIDEO]
    assert len(variants) == 2
    dynamic_ranges = {
        stream.dynamic_range
        for variant in variants
        for stream in variant.streams
        if stream.role is StreamRole.VIDEO
    }
    assert dynamic_ranges == {"SDR", "HDR10"}
    assert any("HDR10" in variant.label for variant in variants)


def test_drm_variants_are_not_downloadable() -> None:
    item = {
        "id": "x",
        "has_drm": True,
        "formats": [
            video_format("137"),
            audio_format("140"),
        ],
    }
    variants = build_variants(item)
    assert variants
    assert all(variant.downloadable is False for variant in variants)
    assert all(variant.unavailable_reason for variant in variants)
    assert all(
        stream.drm
        for variant in variants
        for stream in variant.streams
    )
    item_without_downloads = MediaItem(
        id="x",
        title="x",
        variants=tuple(variants),
    )
    assert default_recommendations(item_without_downloads) == Recommendations()


def test_has_drm_maybe_is_treated_as_drm() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137"),
            audio_format("140", has_drm="maybe"),
        ],
    }
    variants = build_variants(item)
    assert all(variant.downloadable is False for variant in variants)


def test_missing_sizes_are_not_guessed() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137"),
            audio_format("140"),
        ],
    }
    video = next(v for v in build_variants(item) if v.id == "1080p-mp4")
    assert video.filesize is None
    assert video.filesize_approx is None
    assert video.streams[0].filesize is None
    assert video.streams[1].filesize is None


def test_mixed_exact_and_approx_sizes_become_an_approximation() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137", filesize=48_000_000),
            audio_format("140", filesize_approx=4_100_000),
        ],
    }
    video = next(v for v in build_variants(item) if v.id == "1080p-mp4")
    assert video.filesize is None
    assert video.filesize_approx == 52_100_000


def test_missing_stream_url_is_not_downloadable() -> None:
    item = {
        "id": "x",
        "formats": [
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "height": 1080,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 128,
            },
        ],
    }
    variants = build_variants(item)
    assert variants
    assert all(variant.downloadable is False for variant in variants)
    assert all("no stream URL" in (variant.unavailable_reason or "") for variant in variants)


def test_private_availability_requires_authentication() -> None:
    item = {
        "id": "x",
        "availability": "private",
        "formats": [audio_format("140")],
    }
    assert build_variants(item)[0].downloadable is False
    assert build_variants(item, authenticated=True)[0].downloadable is True


def test_progressive_and_merged_duplicates_are_collapsed() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137", height=720, fps=30, tbr=2500),
            progressive_format("22", height=720, filesize=30_000_000),
            audio_format("140"),
        ],
    }
    variants = [v for v in build_variants(item) if v.kind is MediaKind.AUDIO_VIDEO]
    assert [variant.id for variant in variants] == ["720p-mp4"]
    # The higher-bitrate video-only stream wins the duplicate key.
    assert variants[0].requires_merge is True
    assert variants[0].streams[0].format_id == "137"


def test_higher_fps_duplicate_wins() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137", fps=30),
            video_format("137-60", fps=60),
            audio_format("140"),
        ],
    }
    variants = [v for v in build_variants(item) if v.kind is MediaKind.AUDIO_VIDEO]
    assert len(variants) == 1
    assert variants[0].streams[0].format_id == "137-60"


def test_best_variant_helpers_skip_drm_and_unavailable() -> None:
    item = {
        "id": "x",
        "formats": [
            video_format("137"),
            audio_format("140"),
        ],
    }
    variants = build_variants(item)
    assert best_video_variant(variants) is not None
    assert best_audio_variant(variants) is not None
    assert best_video_variant([]) is None
    assert best_audio_variant([]) is None


def test_storyboard_mhtml_formats_are_not_variants() -> None:
    item = {
        "id": "x",
        "formats": [
            progressive_format("18", height=360, filesize=12_000_000),
            {
                "format_id": "sb0",
                "format_note": "storyboard",
                "ext": "mhtml",
                "vcodec": "none",
                "acodec": "none",
                "height": 180,
                "fps": 0.1,
                "url": "https://media.example.test/sb0.mhtml",
            },
        ],
    }
    variants = build_variants(item)
    assert [variant.id for variant in variants] == ["360p-mp4"]
    assert all(variant.container != "mhtml" for variant in variants)


def test_progressive_audio_does_not_use_total_bitrate() -> None:
    item = {
        "id": "x",
        "formats": [
            progressive_format("18", height=360, tbr=195.019, filesize=12_000_000),
        ],
    }
    variant = build_variants(item)[0]
    audio = next(stream for stream in variant.streams if stream.role is StreamRole.AUDIO)
    assert audio.bitrate_kbps is None


def test_missing_stream_size_is_estimated_from_bitrate_and_duration() -> None:
    item = {
        "id": "x",
        "duration": 100.0,
        "formats": [
            video_format("137", vbr=1000),
            audio_format("140", abr=128),
        ],
    }
    video = next(v for v in build_variants(item) if v.id == "1080p-mp4")
    video_stream = video.streams[0]
    audio_stream = video.streams[1]

    assert video_stream.filesize is None
    assert video_stream.filesize_approx == 12_500_000
    assert audio_stream.filesize is None
    assert audio_stream.filesize_approx == 1_600_000
    assert video.filesize is None
    assert video.filesize_approx == 14_100_000


def test_audio_only_size_is_estimated_from_abr() -> None:
    item = {
        "id": "x",
        "duration": 60.0,
        "formats": [audio_format("140", abr=128)],
    }
    variant = build_variants(item)[0]
    assert variant.filesize is None
    assert variant.filesize_approx == 960_000
