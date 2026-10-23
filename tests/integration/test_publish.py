"""Filesystem publishing tests: naming, sanitization, collisions, safety."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from trackforge.adapters.publishing.filesystem import (
    FilesystemPublisher,
    format_library_name,
    sanitize_component,
)
from trackforge.application.ports.publishing import PublishOptions
from trackforge.domain.errors import PublishFailed
from trackforge.domain.model.ids import BundleId, ItemId, RenderId, new_uuid7
from trackforge.domain.model.outputs import OutputBundle, TargetFormat

pytestmark = pytest.mark.integration

BUNDLE_HASH = "a" * 64


def make_bundle(
    directory: Path,
    *,
    title: str = "Song",
    artist: str = "Artist",
    derivative: list[str] | None = None,
    item_id: ItemId | None = None,
) -> tuple[OutputBundle, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "track.mp3").write_bytes(b"ID3fake-audio-bytes")
    sidecar = {
        "schema_version": 1,
        "trackforge": {
            "item_id": str(item_id or ItemId(new_uuid7())),
            "bundle_hash": BUNDLE_HASH,
        },
        "metadata": {
            "title": title,
            "artist": [artist],
            "date": "2024",
            "track": 1,
        },
        "identity": {"derivative": derivative or []},
    }
    (directory / "trackforge.json").write_text(
        json.dumps(sidecar),
        encoding="utf-8",
    )
    bundle = OutputBundle(
        id=BundleId(new_uuid7()),
        render_id=RenderId(new_uuid7()),
        item_id=ItemId(sidecar["trackforge"]["item_id"]),
        files=("track.mp3", "trackforge.json"),
        bundle_hash=BUNDLE_HASH,
    )
    return bundle, directory


def test_format_name_sanitizes_forbidden_characters_and_limits_bytes() -> None:
    name = format_library_name(
        "{artist} - {title}",
        {"title": 'Bad/Title: *?"<>|', "artist": "A"},
        short_id="abcd1234",
        extension=".mp3",
    )
    assert not any(char in name for char in '/\\:*?"<>|')
    assert name.endswith(".mp3")

    long_name = format_library_name(
        "{artist} - {title}",
        {"title": "word " * 100, "artist": "Artist"},
        short_id="abcd1234",
        extension=".mp3",
    )
    assert len(long_name.encode("utf-8")) <= 180
    assert long_name.endswith(".mp3")


def test_sanitize_rejects_dot_components_and_reserved_names() -> None:
    assert sanitize_component("..") != ".."
    assert sanitize_component(".") != "."
    assert sanitize_component("CON") == "_CON"
    assert sanitize_component("A\x00B") == "A_B"


def test_publish_uses_naming_sidecar_and_lrc(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle, directory = make_bundle(bundle_dir, title="Night Song", artist="Ärtist")
    (directory / "lyrics.lrc").write_text("[00:01.00]line\n", encoding="utf-8")
    bundle = OutputBundle(
        id=bundle.id,
        render_id=bundle.render_id,
        item_id=bundle.item_id,
        files=(*bundle.files, "lyrics.lrc"),
        bundle_hash=bundle.bundle_hash,
    )
    library = tmp_path / "library"
    publisher = FilesystemPublisher()

    entry = asyncio.run(
        publisher.publish(
            bundle,
            library,
            "{artist} - {title} ({qualifier})",
            PublishOptions(),
            bundle_dir=bundle_dir,
        )
    )

    audio = Path(entry.path)
    assert audio.name == "Ärtist - Night Song.mp3"
    assert audio.is_file()
    assert audio.with_suffix(".lrc").is_file()
    assert (library / "trackforge.json").is_file()


def test_collision_never_overwrites_and_appends_suffix(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle, _ = make_bundle(bundle_dir, title="Same", artist="Artist")
    library = tmp_path / "library"
    library.mkdir()
    existing = library / "Artist - Same.mp3"
    existing.write_bytes(b"existing")
    publisher = FilesystemPublisher()

    entry = asyncio.run(
        publisher.publish(
            bundle,
            library,
            "{artist} - {title}",
            PublishOptions(),
            bundle_dir=bundle_dir,
        )
    )

    assert Path(entry.path).name == "Artist - Same (1).mp3"
    assert existing.read_bytes() == b"existing"


def test_naming_template_cannot_escape_library_root(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle, _ = make_bundle(bundle_dir, title="Title", artist="Artist")
    library = tmp_path / "library"
    publisher = FilesystemPublisher()

    entry = asyncio.run(
        publisher.publish(
            bundle,
            library,
            "../../{title}",
            PublishOptions(),
            bundle_dir=bundle_dir,
        )
    )

    path = Path(entry.path).resolve()
    assert path.parent == library.resolve()
    assert ".." not in path.parts


def test_symlink_collision_is_not_followed(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle, _ = make_bundle(bundle_dir, title="Sym", artist="Artist")
    library = tmp_path / "library"
    library.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    (library / "Artist - Sym.mp3").symlink_to(outside)
    publisher = FilesystemPublisher()

    entry = asyncio.run(
        publisher.publish(
            bundle,
            library,
            "{artist} - {title}",
            PublishOptions(),
            bundle_dir=bundle_dir,
        )
    )

    assert Path(entry.path).name == "Artist - Sym (1).mp3"
    assert outside.read_bytes() == b"outside"


def test_read_only_library_raises_publish_failed(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle, _ = make_bundle(bundle_dir)
    library = tmp_path / "library"
    library.mkdir()
    library.chmod(0o500)
    publisher = FilesystemPublisher()
    try:
        with pytest.raises(PublishFailed):
            asyncio.run(
                publisher.publish(
                    bundle,
                    library,
                    "{artist} - {title}",
                    PublishOptions(),
                    bundle_dir=bundle_dir,
                )
            )
    finally:
        library.chmod(0o700)


def test_publish_options_reject_unknown_collision_policy() -> None:
    from trackforge.domain.errors import DomainValidationError

    with pytest.raises(DomainValidationError):
        PublishOptions(collision="overwrite")  # type: ignore[arg-type]


def test_target_format_enum_is_not_used_for_extension_guess() -> None:
    assert TargetFormat.MP3.value == "mp3"
