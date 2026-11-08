"""Tests for the normalized metadata value objects."""

from __future__ import annotations

import pytest

from trackforge.metadata import (
    Artwork,
    ArtworkCollection,
    ArtworkKind,
    AudioMetadata,
    CommonMetadata,
    Lyrics,
    MediaKind,
    MediaMetadata,
    PartialDate,
    VideoMetadata,
)


class TestPartialDate:
    def test_year_only(self) -> None:
        value = PartialDate(2024)
        assert str(value) == "2024"

    def test_year_month(self) -> None:
        value = PartialDate(2024, 5)
        assert str(value) == "2024-05"

    def test_full_date(self) -> None:
        value = PartialDate(2024, 5, 17)
        assert str(value) == "2024-05-17"

    @pytest.mark.parametrize(
        ("year", "month", "day"),
        [
            (0, None, None),
            (10000, None, None),
            (2024, 0, None),
            (2024, 13, None),
            (2024, 2, 30),
        ],
    )
    def test_invalid_values(self, year: int, month: int | None, day: int | None) -> None:
        with pytest.raises(ValueError):
            _ = PartialDate(year, month, day)

    def test_day_requires_month(self) -> None:
        with pytest.raises(ValueError, match="day cannot be set without month"):
            _ = PartialDate(2024, day=17)


class TestArtwork:
    def test_defaults_to_front(self) -> None:
        artwork = Artwork(data=b"jpeg", mime="image/jpeg")
        assert artwork.kind is ArtworkKind.FRONT
        assert artwork.description is None

    def test_accepts_string_kind(self) -> None:
        artwork = Artwork(data=b"png", mime="image/png", kind="back")
        assert artwork.kind is ArtworkKind.BACK

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"data": b"", "mime": "image/jpeg"},
            {"data": b"x", "mime": ""},
            {"data": b"x", "mime": "image/jpeg", "width": 0},
            {"data": b"x", "mime": "image/jpeg", "height": -1},
        ],
    )
    def test_invalid_values(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            _ = Artwork(**kwargs)  # pyright: ignore[reportArgumentType]

    def test_invalid_kind(self) -> None:
        with pytest.raises(ValueError):
            _ = Artwork(data=b"x", mime="image/jpeg", kind="poster")


class TestArtworkCollection:
    def test_add_iterate_and_front(self) -> None:
        collection = ArtworkCollection()
        back = Artwork(data=b"back", mime="image/jpeg", kind=ArtworkKind.BACK)
        front = Artwork(data=b"front", mime="image/jpeg")
        collection.add(back)
        collection.add(front)

        assert len(collection) == 2
        assert list(collection) == [back, front]
        assert collection.front == front
        assert collection.to_list() == [back, front]
        assert bool(collection)

    def test_empty(self) -> None:
        collection = ArtworkCollection()
        assert not collection
        assert collection.front is None
        assert collection.to_list() == []

    def test_replace_and_remove_front(self) -> None:
        collection = ArtworkCollection()
        back = Artwork(data=b"back", mime="image/jpeg", kind="back")
        old_front = Artwork(data=b"old", mime="image/jpeg")
        new_front = Artwork(data=b"new", mime="image/png")
        collection.extend([back, old_front])

        collection.replace_front(new_front)

        assert collection.front == new_front
        assert len(collection) == 2
        assert collection[0] == back

        assert collection.remove_front() is True
        assert collection.front is None
        assert collection.to_list() == [back]
        assert collection.remove_front() is False


class TestMetadata:
    def test_defaults_are_not_shared(self) -> None:
        first = CommonMetadata()
        second = CommonMetadata()
        first.genre.append("Rock")
        first.artists.append("Artist")

        assert second.genre == []
        assert second.artists == []

    def test_media_kind(self) -> None:
        assert MediaMetadata().kind is MediaKind.UNKNOWN
        assert MediaMetadata(audio=AudioMetadata()).kind is MediaKind.AUDIO
        assert MediaMetadata(video=VideoMetadata()).kind is MediaKind.VIDEO
        assert (
            MediaMetadata(audio=AudioMetadata(), video=VideoMetadata()).kind
            is MediaKind.AUDIO_VIDEO
        )

    def test_audio_and_video_facets(self) -> None:
        metadata = MediaMetadata(
            title="Track",
            artist="Artist",
            audio=AudioMetadata(album="Album", track_number=1),
            video=VideoMetadata(show="Show", season=2, episode=3),
        )

        assert metadata.title == "Track"
        assert metadata.audio is not None
        assert metadata.audio.album == "Album"
        assert metadata.video is not None
        assert metadata.video.season == 2
        assert metadata.kind is MediaKind.AUDIO_VIDEO

    def test_lyrics(self) -> None:
        lyrics = Lyrics(text="line", language="eng", synced=False)
        assert lyrics.text == "line"
        assert lyrics.language == "eng"
        assert lyrics.synced is False
