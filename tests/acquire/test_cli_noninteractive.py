"""Non-interactive ``tfg input`` selection tests (no TTY)."""

# pyright: standard

from __future__ import annotations

from pathlib import Path

import pytest
from cli_fakes import (
    URL,
    DownloadingFakeLoader,
    install_input,
    make_video_source,
)
from fakes import make_source_info
from typer.testing import CliRunner

from trackforge.acquire.models import ItemSelection
from trackforge.cli import app

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


def test_no_tty_without_selection_lists_variants_and_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke("input", URL, "--out", str(tmp_path))

    assert result.exit_code == 2, result.output
    assert "selection required" in result.output
    assert "audio-best" in result.output
    assert "★" in result.output
    assert "Select [" not in result.output
    assert loader.download_calls == []


def test_explicit_variant_works_without_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke(
        "input",
        URL,
        "--variant",
        "audio-best",
        "--out",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    request = loader.download_calls[0]
    assert request.selections == (ItemSelection("item-1", "audio-best"),)


def test_yes_uses_audio_recommendation_for_audio_only_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke("input", URL, "--yes", "--out", str(tmp_path))

    assert result.exit_code == 0, result.output
    request = loader.download_calls[0]
    assert request.selections[0].variant_id == "audio-best"


def test_yes_prefers_video_recommendation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    result = invoke("input", URL, "--yes", "--out", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert loader.download_calls[0].selections[0].variant_id == "1080p-mp4"


def test_audio_and_video_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    audio_result = invoke(
        "input",
        URL,
        "--audio",
        "recommended",
        "--yes",
        "--out",
        str(tmp_path),
    )
    video_result = invoke(
        "input",
        URL,
        "--video",
        "recommended",
        "--yes",
        "--out",
        str(tmp_path),
    )

    assert audio_result.exit_code == 0, audio_result.output
    assert video_result.exit_code == 0, video_result.output
    assert loader.download_calls[0].selections[0].variant_id == "audio-best"
    assert loader.download_calls[1].selections[0].variant_id == "1080p-mp4"


def test_variant_recommended_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    result = invoke(
        "input",
        URL,
        "--variant",
        "recommended:video",
        "--out",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert loader.download_calls[0].selections[0].variant_id == "1080p-mp4"


def test_resolution_alias_selects_matching_video(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    result = invoke(
        "input",
        URL,
        "--variant",
        "1080p",
        "--out",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert loader.download_calls[0].selections[0].variant_id == "1080p-mp4"


def test_missing_recommended_video_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke(
        "input",
        URL,
        "--video",
        "recommended",
        "--yes",
        "--out",
        str(tmp_path),
    )

    assert result.exit_code == 2, result.output
    assert "recommended video" in result.output
    assert loader.download_calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["--variant", "audio-best", "--audio", "recommended"],
        ["--audio", "recommended", "--video", "recommended"],
        ["--variant", "missing"],
        ["--pick", "1"],
        ["--events", "text"],
    ],
)
def test_invalid_noninteractive_arguments_exit_two(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke("input", URL, *args, "--yes", "--out", str(tmp_path))

    assert result.exit_code == 2, result.output
    assert loader.download_calls == []


def test_json_stdout_stays_clean_when_selection_is_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke(
        "input",
        URL,
        "--json",
        "--out",
        str(tmp_path),
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "audio-best" in result.stderr
    assert "selection required" in result.stderr


def test_human_mode_reports_current_activity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = invoke(
        "input",
        URL,
        "--variant",
        "audio-best",
        "--out",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "[resolve] inspecting source..." in result.stderr
    assert "[download] starting..." in result.stderr
