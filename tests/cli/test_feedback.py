"""CLI feedback export/stats tests and review scope validation."""

from __future__ import annotations

import json
from pathlib import Path

from tests.cli.conftest import cli_paths, invoke, parse_json
from tests.fixtures.media import generate_media, skip_without_media_tools


def _import_item(tmp_path: Path, data: Path, library: Path) -> str:
    fixture = generate_media(tmp_path / "media", "mp3")
    result = invoke("import", str(fixture.path), *cli_paths(data, library), "--auto", "--json")
    assert result.exit_code == 0, result.stderr
    return parse_json(result)["items"][0]["item_id"]


def test_feedback_export_and_stats_after_review(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id = _import_item(tmp_path, data, library)

    reviewed = invoke(
        "review",
        item_id,
        "--field",
        "title",
        "--set",
        "Reviewed Title",
        "--lock",
        "--reason-code",
        "wrong_title",
        "--reason-text",
        "noise",
        *cli_paths(data, library),
        "--json",
    )
    assert reviewed.exit_code == 0, reviewed.stderr
    reviewed_payload = parse_json(reviewed)
    assert reviewed_payload["correction"]["reason_code"] == "wrong_title"
    assert reviewed_payload["session"]["id"]

    output = tmp_path / "feedback.jsonl"
    exported = invoke(
        "feedback",
        "export",
        "--format",
        "jsonl",
        "--output",
        str(output),
        *cli_paths(data, library),
        "--json",
    )
    assert exported.exit_code == 0, exported.stderr
    export_payload = parse_json(exported)
    assert export_payload["event_count"] >= 1
    assert Path(export_payload["output_path"]) == output
    events = [json.loads(line) for line in output.read_text().splitlines() if line]
    assert any(event["kind"] == "correction" for event in events)
    assert any(event["field"] == "title" for event in events)

    stats = invoke(
        "feedback",
        "stats",
        "--group-by",
        "field",
        *cli_paths(data, library),
        "--json",
    )
    assert stats.exit_code == 0, stats.stderr
    stats_payload = parse_json(stats)
    assert stats_payload["corrections"] >= 1
    assert stats_payload["correction_rate"] > 0
    assert "title" in stats_payload["by_field"]
    assert stats_payload["by_field"]["title"]["corrections"] >= 1

    by_source = invoke(
        "feedback",
        "stats",
        "--group-by",
        "source",
        *cli_paths(data, library),
        "--json",
    )
    assert by_source.exit_code == 0, by_source.stderr
    assert parse_json(by_source)["group_by"] == "source"


def test_feedback_export_csv_and_empty_stats(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id = _import_item(tmp_path, data, library)
    invoke(
        "review",
        item_id,
        "--set",
        "title=CSV Title",
        *cli_paths(data, library),
        "--json",
    )

    output = tmp_path / "feedback.csv"
    exported = invoke(
        "feedback",
        "export",
        "--format",
        "csv",
        "--output",
        str(output),
        *cli_paths(data, library),
        "--json",
    )
    assert exported.exit_code == 0, exported.stderr
    header = output.read_text().splitlines()[0]
    assert header.startswith("id,kind,item_id,field,")

    stats = invoke("feedback", "stats", *cli_paths(data, library), "--json")
    assert stats.exit_code == 0, stats.stderr
    payload = parse_json(stats)
    assert payload["total_events"] >= 1
    assert payload["group_by"] == "field"


def test_review_global_scope_is_rejected_without_preference_rule(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    item_id = _import_item(tmp_path, data, library)

    reviewed = invoke(
        "review",
        item_id,
        "--field",
        "title",
        "--set",
        "Wide Scope",
        "--scope",
        "global",
        *cli_paths(data, library),
        "--json",
    )

    assert reviewed.exit_code == 2
    assert reviewed.stdout == ""
    error = json.loads(reviewed.stderr)
    assert error["type"] == "UnsupportedScope"
    assert "scope" in error["context"]
