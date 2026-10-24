"""CLI tests for playlist/folder planning and collection commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import FAKE_DIR, read_log
from tests.cli.conftest import cli_paths, invoke, parse_json
from tests.fixtures.media import generate_media, skip_without_media_tools

PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-five"


@pytest.fixture
def fake_ytdlp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{__import__('os').environ.get('PATH', '')}")
    monkeypatch.setenv("TRACKFORGE_SOURCE_COVER_ENABLED", "false")
    log = tmp_path / "ytdlp-cli.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(log))
    return log


def test_plan_playlist_json_is_side_effect_free(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs

    result = invoke("plan", PLAYLIST_URL, *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["blocked"] is False
    assert payload["tag_basis"] == "album_first"
    assert payload["collection"]["title"] == "Fixture Playlist Five"
    assert payload["selected_positions"] == [1, 2, 3, 4, 5]
    assert not data.exists()
    assert not library.exists()
    calls = read_log(fake_ytdlp)
    assert any("--flat-playlist" in call for call in calls)
    assert not any("-o" in call for call in calls)


def test_plan_filters_positions(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs

    result = invoke(
        "plan",
        PLAYLIST_URL,
        *cli_paths(data, library),
        "--playlist-start",
        "2",
        "--playlist-end",
        "4",
        "--playlist-items",
        "2,4",
        "--skip",
        "3",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    assert parse_json(result)["selected_positions"] == [2, 4]
    calls = read_log(fake_ytdlp)
    assert any("--playlist-start" in call for call in calls)
    assert any("--playlist-end" in call for call in calls)
    assert any("--playlist-items" in call for call in calls)


def test_add_playlist_dry_run_does_not_download_or_create_data(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs

    result = invoke(
        "add",
        PLAYLIST_URL,
        *cli_paths(data, library),
        "--auto",
        "--dry-run",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["collection"]["title"] == "Fixture Playlist Five"
    assert len(payload["selected_items"]) == 5
    assert not data.exists()
    assert not library.exists()
    assert not any("-o" in call for call in read_log(fake_ytdlp))


def test_invalid_tag_basis_exits_two(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs

    result = invoke(
        "collection",
        "plan",
        PLAYLIST_URL,
        *cli_paths(data, library),
        "--tag-basis",
        "quality_first",
        "--json",
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["type"] == "DomainValidationError"
    assert not data.exists()


def test_more_than_500_folder_items_exits_two(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs
    folder = tmp_path / "Large Folder"
    folder.mkdir()
    for index in range(501):
        (folder / f"{index:04d}.mp3").write_bytes(b"x")

    result = invoke("plan", str(folder), *cli_paths(data, library), "--json")

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["type"] == "CollectionTooLarge"
    assert not data.exists()


def test_collection_add_one_item_and_show(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs

    result = invoke(
        "collection",
        "add",
        PLAYLIST_URL,
        *cli_paths(data, library),
        "--playlist-items",
        "1",
        "--tag-basis",
        "playlist_first",
        "--min-sleep",
        "0",
        "--auto",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["status"] == "completed"
    assert payload["processed"] == 1
    folder = library / "Fixture Playlist Five"
    audio = [
        path
        for path in folder.iterdir()
        if path.suffix in {".mp3", ".m4a", ".flac", ".ogg", ".opus"}
    ]
    assert len(audio) == 1
    sidecar = next(folder.glob("trackforge*.json"))
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["tag_basis"] == "playlist_first"
    assert document["metadata"]["album"] == "Fixture Playlist Five"
    assert document["metadata"]["albumartist"] == "Fixture Playlist Owner"

    listed = invoke("collection", "list", *cli_paths(data, library), "--json")
    assert listed.exit_code == 0, listed.stderr
    listed_payload = json.loads(listed.stdout)
    assert listed_payload[0]["title"] == "Fixture Playlist Five"
    collection_id = payload["collection_id"]
    shown = invoke(
        "collection",
        "show",
        collection_id,
        *cli_paths(data, library),
        "--json",
    )
    assert shown.exit_code == 0, shown.stderr
    statuses = {item["status"] for item in parse_json(shown)["items"]}
    assert statuses == {"processed", "planned"}


def test_root_add_local_folder_routes_to_collection(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    folder = tmp_path / "Folder Collection"
    generate_media(folder, "mp3", name="only.mp3")

    result = invoke(
        "add",
        str(folder),
        *cli_paths(data, library),
        "--auto",
        "--min-sleep",
        "0",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["status"] == "completed"
    assert payload["processed"] == 1
    audio = [
        path
        for path in (library / "Folder Collection").iterdir()
        if path.suffix in {".mp3", ".m4a", ".flac", ".ogg", ".opus"}
    ]
    assert len(audio) == 1
