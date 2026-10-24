"""Interpretation tests from observation artifacts to claims."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from trackforge.application.use_cases.interpret_item import (
    CLAIMS_ARTIFACT,
    IDENTITY_ARTIFACT,
    ArtifactPayload,
    Interpreter,
    InterpreterInput,
    derivative_detector,
    identity_resolver,
    initial_tags_interpreter,
    kind_classifier,
)
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import ObservationFailed
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.identity import ContentKind
from trackforge.domain.model.ids import ItemId
from trackforge.domain.model.item import ItemStatus

pytestmark = pytest.mark.asyncio


def _field_values(view: Any) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for claim in view.claims:
        result.setdefault(claim.field, []).append(claim.value)
    return result


async def test_initial_tags_and_path_become_claims(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song",
        artists=("Artist",),
        album="Album",
        albumartist=("Album Artist",),
        date="2024-05",
        genre=("Pop",),
        track=3,
        track_total=12,
        mbids={"recording": ["11111111-1111-1111-1111-111111111111"]},
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    fields = _field_values(view)

    assert view.status is ItemStatus.INTERPRETED
    assert "Song" in fields["title"]
    assert set(fields["artist"]) == {"Artist"}
    assert set(fields["album"]) == {"Album"}
    assert fields["albumartist"] == ["Album Artist"]
    assert fields["date"] == ["2024-05"]
    assert fields["genre"] == ["Pop"]
    assert set(fields["track"]) == {1, 3}
    assert fields["tracktotal"] == [12]
    assert fields["kind"] == ["music"]
    assert fields["mb_recording_id"] == ["11111111-1111-1111-1111-111111111111"]
    assert view.identity is not None
    assert view.identity["status"] == "proposed"
    assert {payload["schema_version"] for payload in view.artifact_payloads} == {1}
    assert any(
        claim.source == "initial_tags" and float(claim.confidence) <= 0.4 for claim in view.claims
    )
    async with catalog_context.unit_of_work() as uow:
        stored = await uow.claims.list_by_item(item_id)
    assert len(stored) == len(view.claims)


async def test_path_fallback_supplies_weak_title_artist_album_and_track(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="",
        artists=(),
        album=None,
        path="/music/Artist/Album/01 - Song.mp3",
        stem="01 - Song",
        parent_names=("music", "Artist", "Album"),
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    fields = _field_values(view)
    assert "title" in fields
    assert fields["artist"] == ["Artist"]
    assert fields["album"] == ["Album"]
    assert fields["track"] == [1]
    assert resolved.fields["title"] == "Song"
    assert resolved.fields["artist"] == "Artist"
    assert resolved.fields["album"] == "Album"
    assert resolved.fields["track"] == 1


async def test_topic_and_vevo_are_not_artists(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song",
        artists=("ArtistVEVO", "Artist - Topic"),
        path=None,
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert [claim.value for claim in view.claims if claim.field == "artist"] == ["Artist"]
    assert resolved.fields["artist"] == "Artist"
    source_noise = {claim.value for claim in view.claims if claim.field == "source_noise"}
    assert "vevo" in source_noise
    assert "topic_channel" in source_noise


async def test_derivative_title_is_derived_without_duplicate(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song",
        artists=("Artist",),
        comment="nightcore remix",
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    transformations = [
        claim
        for claim in view.claims
        if claim.subject is Subject.DERIVATIVE and claim.field == "transformation"
    ]
    assert {claim.value for claim in transformations} >= {"nightcore", "remix"}
    assert resolved.fields["title"] == "Song (Nightcore)"
    assert resolved.fields["artist"] == "Artist"


async def test_no_derivative_is_claimed_without_evidence(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert not any(claim.subject is Subject.DERIVATIVE for claim in view.claims)
    assert resolved.fields["title"] == "Song"


async def test_identity_without_external_ids_is_ambiguous(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)

    assert view.identity is not None
    assert view.identity["status"] == "ambiguous"
    assert view.identity["hypotheses"] == []
    assert "no_identity_evidence" in view.identity["warnings"]
    assert not any(claim.subject.value == "recording" for claim in view.claims)


async def test_interpret_is_idempotent(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)

    await catalog_context.interpret_item().interpret(item_id, observed)
    async with catalog_context.unit_of_work() as uow:
        first_count = len(await uow.claims.list_by_item(item_id))
    await catalog_context.interpret_item().interpret(item_id, observed)
    async with catalog_context.unit_of_work() as uow:
        second_count = len(await uow.claims.list_by_item(item_id))

    assert second_count == first_count


async def test_kind_classifier_flags_audiobook_from_chapters(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Book",
        artists=("Author",),
        path=None,
        duration=2000.0,
        chapters=3,
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)

    kind_claims = [claim for claim in view.claims if claim.field == "kind"]
    assert kind_claims[0].value == ContentKind.AUDIOBOOK.value
    async with catalog_context.unit_of_work() as uow:
        item = await uow.items.get(item_id)
    assert item is not None
    assert item.kind is ContentKind.AUDIOBOOK


async def test_source_noise_is_preserved_not_removed(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song (Official Video)",
        artists=("Artist",),
        path=None,
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert resolved.fields["title"] == "Song (Official Video)"
    assert any(
        claim.field == "source_noise" and claim.value == "official_video" for claim in view.claims
    )
    assert any("source_noise:official_video" in warning for warning in resolved.warnings)


async def test_missing_initial_tags_is_a_typed_failure(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations()
    observed.pop("initial.tags.v1")

    with pytest.raises(ObservationFailed):
        await catalog_context.interpret_item().interpret(item_id, observed)


async def test_interpreters_are_manifest_declared_capabilities() -> None:
    interpreters = (
        initial_tags_interpreter,
        kind_classifier,
        derivative_detector,
        identity_resolver,
    )

    for interpreter in interpreters:
        assert isinstance(interpreter, Interpreter)
        assert interpreter.manifest.kind.value == "interpreter"
        assert interpreter.manifest.version == "1"
    assert "initial.tags.v1" in {str(item) for item in initial_tags_interpreter.manifest.requires}
    assert initial_tags_interpreter.manifest.provides == (CLAIMS_ARTIFACT,)
    assert identity_resolver.manifest.provides == (IDENTITY_ARTIFACT,)


async def test_interpreter_returns_claims_v1_payload() -> None:
    item_id = ItemId("01930000-0000-7000-8000-000000000001")
    observed = {
        "initial.tags.v1": _artifact("initial.tags.v1", b"{}"),
        "path.structure.v1": _artifact(
            "path.structure.v1",
            b'{"path":"/music/Artist/Song.mp3","parent_names":["music","Artist"],'
            b'"stem":"Song","suffix":".mp3"}',
        ),
        "audio.probe.v1": _artifact(
            "audio.probe.v1",
            b'{"container":"mp3","format_name":"mp3","duration_seconds":180.0,'
            b'"streams":[{"codec_type":"audio"}]}',
        ),
    }
    # The empty tags payload is not valid; use the real schema for a clean test.
    observed["initial.tags.v1"] = _artifact(
        "initial.tags.v1",
        b'{"tags":{"title":"Song"},"raw":{"format":"MP3"},"reader":{"id":"mutagen","version":"1"}}',
    )
    result = initial_tags_interpreter.interpret(
        InterpreterInput(item_id=item_id, artifacts=observed)
    )

    assert result.artifact_type == CLAIMS_ARTIFACT
    assert result.payload["schema_version"] == 1
    assert result.payload["item_id"] == item_id
    assert result.payload["claims"][0]["field"] == "title"


def _artifact(artifact_type: str, payload: bytes) -> ArtifactPayload:
    return ArtifactPayload(type=artifact_type, payload=payload, artifact_id="artifact-1")


async def test_asr_and_plain_lyrics_merge_into_separate_fields(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    from trackforge.application.dto.lyrics import (
        AsrModelInfo,
        AsrSegment,
        AsrTranscript,
        AsrWord,
    )
    from trackforge.application.dto.providers import LyricsCandidate, LyricsCandidatesPayload

    item_id = await create_item()
    observed = make_observations(lyrics=None)
    observed["lyrics.candidates.v1"] = (
        LyricsCandidatesPayload(
            candidates=[
                LyricsCandidate(
                    source="lrclib",
                    title="Song",
                    artist="Artist",
                    duration=180.0,
                    plain="hello world",
                    score=0.8,
                )
            ]
        )
        .model_dump_json()
        .encode("utf-8")
    )
    observed["asr.transcript.v1"] = (
        AsrTranscript(
            language="en",
            segments=[
                AsrSegment(
                    start=1.0,
                    end=2.0,
                    text="hello world",
                    words=[
                        AsrWord(word="hello", start=1.0, end=1.4, probability=0.9),
                        AsrWord(word="world", start=1.5, end=2.0, probability=0.9),
                    ],
                )
            ],
            avg_logprob=-0.2,
            no_speech_prob=0.05,
            model=AsrModelInfo(id="test/whisper", version="1"),
            duration_seconds=180.0,
        )
        .model_dump_json()
        .encode("utf-8")
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    fields = _field_values(view)

    assert fields["lyrics"] == ["hello world"]
    assert len(fields["synced_lyrics"]) == 1
    assert fields["synced_lyrics"][0].startswith("[00:01.00]hello world")
    assert fields["elrc"][0].startswith("[00:01.00]<00:01.00>hello")
    assert fields["language"] == ["en"]
    assert "[00:" not in fields["lyrics"][0]
