"""Exit-code contract for the L3 CLI: 0, 1, 2 and 130."""

# pyright: standard

from __future__ import annotations

from pathlib import Path

import pytest
from cli_fakes import URL, DownloadingFakeLoader, install_input
from fakes import FakeLoader, make_source_info
from typer.testing import CliRunner

from trackforge.acquire.config import AppConfig
from trackforge.acquire.errors import CancelledError, ConfigError
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.models import (
    DownloadFailure,
    DownloadRequest,
    DownloadResult,
)
from trackforge.cli import app
from trackforge.cli.commands import input as input_command

runner = CliRunner()


def test_success_is_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output


def test_unsupported_url_is_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = FakeLoader(name="fake", supports=False)
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "UnsupportedUrl" in result.output
    assert "hint:" in result.output


def test_config_error_is_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def broken_config(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        raise ConfigError("broken config")

    monkeypatch.setattr(input_command, "load_config", broken_config)

    result = runner.invoke(
        app,
        ["input", URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "ConfigError" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["input", "ftp://example.test/media"],
        ["input", ""],
        ["input"],
        ["input", URL, "--variant", "missing"],
        ["input", URL],
    ],
)
def test_usage_and_selection_errors_are_two(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(app, [*args, "--out", str(tmp_path)])

    assert result.exit_code == 2, result.output


def test_interactive_cancel_is_130(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(input_command, "stdin_is_tty", lambda: True)
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--out", str(tmp_path)],
        input="q\n",
    )

    assert result.exit_code == 130


def test_partial_failure_is_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(
        info=make_source_info(),
        result_factory=lambda request: DownloadResult(
            files=(),
            failures=(
                DownloadFailure(
                    item_id=request.selections[0].item_id,
                    message="simulated failure",
                    retryable=True,
                ),
            ),
        ),
    )
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "simulated failure" in result.output
    assert "retryable" in result.output


def test_cancelled_download_is_130(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class CancellingLoader(DownloadingFakeLoader):
        def download(
            self,
            request: DownloadRequest,
            *,
            ctx: LoaderContext,
        ) -> DownloadResult:
            del request, ctx
            raise CancelledError("cancelled by test")

    loader = CancellingLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 130
    assert "cancelled" in result.output
