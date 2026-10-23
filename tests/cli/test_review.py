"""CLI review, explain, lock, and re-render tests."""

from __future__ import annotations

import json
from pathlib import Path

from tests.cli.conftest import cli_paths, invoke, parse_json
from tests.fixtures.media import generate_media, skip_without_media_tools


def _import_item(
    tmp_path: Path,
    data: Path,
    library: Path,
) -> tuple[str, str]:
    fixture = generate_media(tmp_path / "media", "mp3")
    result = invoke("import", str(fixture.path), *cli_paths(data, library), "--auto", "--json")
    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    return payload["items"][0]["item_id"], payload["run_id"]


def test_review_set_and_lock_creates_user_claim_and_feedback(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id, _ = _import_item(tmp_path, data, library)

    reviewed = invoke(
        "review",
        item_id,
        "--field",
        "title",
        "--set",
        "Reviewed Title",
        "--lock",
        *cli_paths(data, library),
        "--json",
    )

    assert reviewed.exit_code == 0, reviewed.stderr
    payload = parse_json(reviewed)
    assert payload["action"] == "edit"
    field = next(entry for entry in payload["fields"] if entry["field"] == "title")
    assert field["current"] == "Reviewed Title"
    assert field["locked"] is True
    assert "title" in payload["locked_fields"]

    explained = invoke(
        "explain",
        item_id,
        "--field",
        "title",
        *cli_paths(data, library),
        "--json",
    )
    assert explained.exit_code == 0, explained.stderr
    explanation = parse_json(explained)
    assert explanation["decision"] == "Reviewed Title"
    assert explanation["locked"] is True
    assert explanation["chosen_claim"]["source"] == "user"
    assert explanation["chosen_claim"]["confidence"] == 1.0


def test_review_lock_then_render_publish_creates_new_bundle_and_entry(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id, _ = _import_item(tmp_path, data, library)
    original_files = sorted(path.name for path in library.iterdir() if path.is_file())

    reviewed = invoke(
        "review",
        item_id,
        "--set",
        "title=Second Title",
        "--lock",
        *cli_paths(data, library),
        "--json",
    )
    assert reviewed.exit_code == 0, reviewed.stderr

    rendered = invoke(
        "render",
        item_id,
        *cli_paths(data, library),
        "--json",
    )
    assert rendered.exit_code == 0, rendered.stderr
    render_payload = parse_json(rendered)
    assert Path(render_payload["audio_path"]).is_file()

    published = invoke(
        "publish",
        item_id,
        *cli_paths(data, library),
        "--json",
    )
    assert published.exit_code == 0, published.stderr
    publish_payload = parse_json(published)
    assert publish_payload["cache_hit"] is False
    new_path = Path(publish_payload["path"])
    assert new_path.is_file()
    assert "Second Title" in new_path.name
    # The original entry is never overwritten.
    assert all((library / name).is_file() for name in original_files)


def test_review_unlock_and_unknown_actions(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id, _ = _import_item(tmp_path, data, library)
    invoke(
        "review",
        item_id,
        "--field",
        "album",
        "--set",
        "Locked Album",
        "--lock",
        *cli_paths(data, library),
        "--json",
    )

    unlocked = invoke(
        "review",
        item_id,
        "--field",
        "album",
        "--unlock",
        *cli_paths(data, library),
        "--json",
    )
    assert unlocked.exit_code == 0, unlocked.stderr
    payload = parse_json(unlocked)
    assert "album" not in payload["locked_fields"]

    unknown = invoke(
        "review",
        item_id,
        "--field",
        "album",
        "--unknown",
        *cli_paths(data, library),
        "--json",
    )
    assert unknown.exit_code == 0, unknown.stderr
    field = next(entry for entry in parse_json(unknown)["fields"] if entry["field"] == "album")
    assert field["current"] is None


def test_review_invalid_field_is_user_error_with_json_stderr(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id, _ = _import_item(tmp_path, data, library)

    result = invoke(
        "review",
        item_id,
        "--field",
        "definitely_not_a_field",
        "--set",
        "x",
        *cli_paths(data, library),
        "--json",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["type"] in {"RecordNotFound", "DomainValidationError"}
