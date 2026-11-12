"""Interactive ``tfg input`` selection tests."""

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

from trackforge.cli import app
from trackforge.cli.commands import input as input_command

runner = CliRunner()


def make_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(input_command, "stdin_is_tty", lambda: True)


def test_interactive_empty_input_uses_recommendation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    make_interactive(monkeypatch)
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="\n",
    )

    assert result.exit_code == 0, result.output
    assert "Select [" in result.output
    assert "a=recommended audio" in result.output
    assert loader.download_calls[0].selections[0].variant_id == "audio-best"


def test_interactive_number_selects_variant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    make_interactive(monkeypatch)
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="2\n",
    )

    assert result.exit_code == 0, result.output
    assert loader.download_calls[0].selections[0].variant_id == "audio-best"


def test_interactive_a_and_v_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    make_interactive(monkeypatch)
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    audio_result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="a\n",
    )
    video_result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="v\n",
    )

    assert audio_result.exit_code == 0, audio_result.output
    assert video_result.exit_code == 0, video_result.output
    assert loader.download_calls[0].selections[0].variant_id == "audio-best"
    assert loader.download_calls[1].selections[0].variant_id == "1080p-mp4"


def test_interactive_q_cancels_with_130(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    make_interactive(monkeypatch)
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="q\n",
    )

    assert result.exit_code == 130, result.output
    assert loader.download_calls == []


def test_collection_requires_explicit_selection_without_tty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cli_fakes import make_flat_collection

    collection = make_flat_collection()
    loader = DownloadingFakeLoader(info=collection)
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--yes", "--out", str(tmp_path)],
    )

    assert result.exit_code == 2, result.output
    assert "Items:" in result.output
    assert "One" in result.output
    assert loader.download_calls == []


def test_interactive_playlist_resolves_selected_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cli_fakes import PlaylistFakeLoader, make_flat_collection

    make_interactive(monkeypatch)
    loader = PlaylistFakeLoader(make_flat_collection())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="1\n\n",
    )

    assert result.exit_code == 0, result.output
    assert loader.get_info_calls == [
        URL,
        "https://example.test/media/one",
    ]
    request = loader.download_calls[0]
    assert request.selections[0].item_id == "one"
    assert request.selections[0].variant_id == "audio-best"


def test_interactive_playlist_all_items_confirms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cli_fakes import PlaylistFakeLoader, make_flat_collection

    make_interactive(monkeypatch)
    loader = PlaylistFakeLoader(make_flat_collection())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="a\n\n\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "Item 1/2:" in result.output
    assert "Item 2/2:" in result.output
    assert "Download 2 item(s)?" in result.output
    assert [selection.item_id for selection in loader.download_calls[0].selections] == [
        "one",
        "two",
    ]


def test_interactive_playlist_decline_cancels_with_130(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cli_fakes import PlaylistFakeLoader, make_flat_collection

    make_interactive(monkeypatch)
    loader = PlaylistFakeLoader(make_flat_collection())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="a\n\n\nn\n",
    )

    assert result.exit_code == 130, result.output
    assert loader.download_calls == []


def test_flat_and_limit_are_forwarded_before_item_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cli_fakes import PlaylistFakeLoader, make_flat_collection

    make_interactive(monkeypatch)
    loader = PlaylistFakeLoader(make_flat_collection())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        [
            "input",
            URL,
            "--flat",
            "--limit",
            "2",
            "--variant",
            "audio-best",
            "--out",
            str(tmp_path),
        ],
        input="1\n",
    )

    assert result.exit_code == 0, result.output
    ref, flat, limit = loader.info_requests[0]
    assert ref.url == URL
    assert flat is True
    assert limit == 2
    assert loader.info_requests[1][0].url.endswith("/one")
