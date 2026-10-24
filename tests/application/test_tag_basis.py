"""Tag-basis selection and field-mapping tests."""

from __future__ import annotations

import pytest

from trackforge.application.dto.collections import CollectionItemContext
from trackforge.application.services.tag_basis import (
    TagBasisApplication,
    choose_tag_basis,
    plan_tag_basis,
)
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.collection import (
    CollectionContextClaim,
    CollectionKind,
    TagBasis,
)
from trackforge.domain.model.ids import CollectionId
from trackforge.domain.model.outputs import ResolvedMetadata

pytestmark = pytest.mark.unit

OLD_COVER = "a" * 64
NEW_COVER = "b" * 64


def context(*, cover: str = NEW_COVER) -> CollectionItemContext:
    return CollectionItemContext(
        collection_id=CollectionId("01a16fd9-4c43-7e80-b021-9c3461aba081"),
        kind=CollectionKind.PLAYLIST,
        title="Friday Mix",
        owner="Fixture Owner",
        position=3,
        tag_basis=TagBasis.PLAYLIST_FIRST,
        context_claims=(
            CollectionContextClaim(field="date", value="2024-02-03"),
            CollectionContextClaim(field="cover", value=cover),
        ),
    )


def test_playlist_first_maps_collection_context_without_touching_artist() -> None:
    plan = plan_tag_basis(
        TagBasis.PLAYLIST_FIRST,
        context(),
        {
            "album": "Release Album",
            "albumartist": "Release Artist",
            "artist": "Track Artist",
            "track": 1,
            "date": "2020",
            "artwork": OLD_COVER,
        },
    )

    fields = plan.fields
    assert fields["album"] == "Friday Mix"
    assert fields["albumartist"] == "Fixture Owner"
    assert fields["track"] == 3
    assert fields["date"] == "2024-02-03"
    assert fields["artwork"] == NEW_COVER
    assert "artist" not in fields
    assert plan.degraded is False


def test_playlist_first_uses_various_artists_without_owner() -> None:
    ctx = CollectionItemContext(
        collection_id=context().collection_id,
        kind=CollectionKind.FOLDER,
        title="Folder",
        position=1,
        tag_basis=TagBasis.PLAYLIST_FIRST,
    )

    plan = plan_tag_basis(TagBasis.PLAYLIST_FIRST, ctx, {})

    assert plan.fields["albumartist"] == "Various Artists"
    assert "artist" not in plan.fields


def test_playlist_cover_url_is_not_treated_as_cas_artifact() -> None:
    plan = plan_tag_basis(
        TagBasis.PLAYLIST_FIRST,
        context(cover="https://example.invalid/cover.jpg"),
        {},
    )

    assert "artwork" not in plan.fields
    assert any("not a stored CAS artifact" in warning for warning in plan.warnings)


def test_album_first_is_unchanged_when_release_context_exists() -> None:
    plan = plan_tag_basis(
        TagBasis.ALBUM_FIRST,
        context(),
        {"album": "Release Album", "albumartist": "Release Artist", "track": 4},
    )

    assert plan.overrides == ()
    assert plan.degraded is False


def test_album_first_degrades_to_playlist_context_when_release_is_missing() -> None:
    plan = plan_tag_basis(
        TagBasis.ALBUM_FIRST,
        context(),
        {"album": None, "albumartist": None, "track": None, "date": None},
    )

    assert plan.degraded is True
    assert plan.fields["album"] == "Friday Mix"
    assert plan.fields["albumartist"] == "Fixture Owner"
    assert plan.fields["track"] == 3
    assert any("album_first_degraded" in warning for warning in plan.warnings)


def test_ask_defaults_to_album_first_in_non_interactive_modes() -> None:
    auto = choose_tag_basis(TagBasis.ASK, auto=True)
    json_mode = choose_tag_basis(TagBasis.ASK, json_output=True)

    assert auto.basis is TagBasis.ALBUM_FIRST
    assert json_mode.basis is TagBasis.ALBUM_FIRST
    assert auto.warnings and json_mode.warnings


def test_ask_accepts_interactive_choice_and_rejects_unknown() -> None:
    choice = choose_tag_basis(TagBasis.ASK, prompt=lambda _: "2")
    assert choice.basis is TagBasis.PLAYLIST_FIRST

    with pytest.raises(DomainValidationError):
        choose_tag_basis(TagBasis.ASK, prompt=lambda _: "quality")
    with pytest.raises(DomainValidationError):
        choose_tag_basis("invalid")


def test_application_dto_carries_sidecar_alternatives() -> None:
    application = TagBasisApplication(
        basis=TagBasis.PLAYLIST_FIRST,
        resolved=ResolvedMetadata(
            item_id="item",
            decision_record_id="record",
            fields={"album": "Friday Mix"},
        ),
        alternatives={"album": ["Release Album", "Friday Mix"]},
    )
    assert application.alternatives["album"][0] == "Release Album"
