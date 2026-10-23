"""CLI import tests: end-to-end local file, JSON shape, cache reuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.cli.conftest import cli_paths, invoke, parse_json
from tests.fixtures.media import DEFAULT_TAGS, LRC_TEXT, generate_media, skip_without_media_tools


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_import_local_mp3_creates_library_file_sidecar_and_lrc(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    tags = dict(DEFAULT_TAGS)
    tags["lyrics"] = LRC_TEXT
    fixture = generate_media(tmp_path / "media", "mp3", tags=tags)
    original_hash = sha256(fixture.path)

    result = invoke(
        "import",
        str(fixture.path),
        *cli_paths(data, library),
        "--auto",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["status"] == "completed"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["status"] == "published"
    output = Path(item["output_path"])
    assert output.is_file()
    assert output.parent == library
    assert output.suffix == ".mp3"
    assert output.stat().st_size > 0
    assert sha256(fixture.path) == original_hash

    sidecars = sorted(library.glob("trackforge*.json"))
    assert sidecars, "managed library must contain a sidecar"
    sidecar = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == 1
    assert sidecar["trackforge"]["bundle_hash"]
    assert sidecar["metadata"]["title"] == "Fixture Song"

    lyrics = output.with_suffix(".lrc")
    assert lyrics.is_file()
    assert "[00:01.00]" in lyrics.read_text(encoding="utf-8")

    from trackforge.adapters.tagging.mutagen.reader import MutagenTagReader

    tags_read = MutagenTagReader().read(output)
    assert tags_read.title == "Fixture Song"
    assert tags_read.artists == ("Fixture Artist",)
    assert tags_read.album == "Fixture Album"


def test_repeat_import_reports_cache_hit_and_does_not_copy_again(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    fixture = generate_media(tmp_path / "media", "mp3")
    first = invoke(
        "import",
        str(fixture.path),
        *cli_paths(data, library),
        "--auto",
        "--json",
    )
    assert first.exit_code == 0, first.stderr
    first_payload = parse_json(first)
    first_item = first_payload["items"][0]["item_id"]
    files_before = sorted(path.name for path in library.iterdir())

    second = invoke(
        "import",
        str(fixture.path),
        *cli_paths(data, library),
        "--auto",
        "--json",
    )

    assert second.exit_code == 0, second.stderr
    second_payload = parse_json(second)
    assert second_payload["cache_hit"] is True
    assert second_payload["items"][0]["item_id"] == first_item
    assert second_payload["items"][0]["status"] == "published"
    assert sorted(path.name for path in library.iterdir()) == files_before
    assert any(warning.startswith("cache_hit:") for warning in second_payload["warnings"])


def test_dry_run_does_not_create_library_files(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    fixture = generate_media(tmp_path / "media", "mp3")

    result = invoke(
        "import",
        str(fixture.path),
        *cli_paths(data, library),
        "--auto",
        "--dry-run",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["blocked"] is False
    assert payload["candidates"]
    assert not data.exists()
    assert not library.exists() or not any(library.iterdir())


def test_plan_command_is_side_effect_free(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    fixture = generate_media(tmp_path / "media", "mp3")

    result = invoke(
        "plan",
        str(fixture.path),
        *cli_paths(data, library),
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["candidates"]
    assert not data.exists()
    assert not library.exists()
