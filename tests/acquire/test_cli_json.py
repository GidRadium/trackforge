"""Stable, secret-free JSON output for ``tfg input``."""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cli_fakes import URL, DownloadingFakeLoader, install_input, write_media
from fakes import make_source_info
from typer.testing import CliRunner

from trackforge.acquire.config import AppConfig, SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.models import (
    DownloadedFile,
    DownloadRequest,
    DownloadResult,
)
from trackforge.acquire.progress import ProgressEvent, ProgressStage
from trackforge.acquire.serialization import assert_schema_shape
from trackforge.cli import app

runner = CliRunner()


class ProgressFakeLoader(DownloadingFakeLoader):
    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult:
        ctx.progress.emit(
            ProgressEvent(
                stage=ProgressStage.DOWNLOAD,
                job_id=ctx.job_id,
                item_index=1,
                item_count=1,
                downloaded_bytes=5,
                total_bytes=10,
                speed=1.5,
                eta=3.0,
            )
        )
        return super().download(request, ctx=ctx)


def make_result(request: DownloadRequest, tmp_path: Path) -> DownloadResult:
    item = request.source_info.items[0]
    media = write_media(tmp_path / "staging" / "Fake media.m4a")
    sidecar = write_media(tmp_path / "staging" / "Fake media.jpg")
    return DownloadResult(
        files=(
            DownloadedFile(
                item_id=item.id,
                path=media,
                item=item,
                sidecars=(sidecar,),
                warnings=("sidecar warning",),
            ),
        ),
        warnings=("source warning",),
    )


def test_json_result_has_stable_shape_and_no_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = ProgressFakeLoader(
        info=make_source_info(),
        result_factory=lambda request: make_result(request, tmp_path),
    )
    config = AppConfig(
        sources={
            "fake": SourceProfile(
                name="fake",
                cookies_file=tmp_path / "SECRET-COOKIE.txt",
                proxy="http://user:SECRET-PASSWORD@proxy.test:8080",
            )
        }
    )
    install_input(monkeypatch, loader, config=config)
    raw = URL + "?token=SECRET-TOKEN"

    result = runner.invoke(
        app,
        [
            "input",
            raw,
            "--variant",
            "audio-best",
            "--json",
            "--out",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "SECRET" not in result.stdout
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "schema_version",
        "input",
        "dry_run",
        "source",
        "selections",
        "plan",
        "result",
    }
    assert payload["schema_version"] == 1
    assert payload["input"]["kind"] == "url"
    assert payload["input"]["raw"].endswith("token=<redacted>")
    assert payload["dry_run"] is False
    assert payload["source"]["schema_version"] == 2
    assert payload["source"]["items"][0]["thumbnails"] == [
        {
            "url": "https://example.test/cover.jpg",
            "width": 1280,
            "height": 720,
        }
    ]
    assert_schema_shape(payload["source"])
    assert payload["selections"] == [
        {"item_id": "item-1", "variant_id": "audio-best"}
    ]
    assert payload["plan"][0]["selector"]
    assert payload["result"]["files"][0]["item_id"] == "item-1"
    assert payload["result"]["files"][0]["sidecars"]
    assert payload["result"]["failures"] == []


def test_dry_run_json_returns_plan_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        [
            "input",
            URL,
            "--variant",
            "audio-best",
            "--dry-run",
            "--json",
            "--out",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["result"] is None
    assert payload["plan"][0]["output_stem"] == "Fake media"
    assert loader.download_calls == []


def test_events_jsonl_goes_to_stderr_and_keeps_stdout_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = ProgressFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        [
            "input",
            URL,
            "--variant",
            "audio-best",
            "--json",
            "--events",
            "jsonl",
            "--out",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    events = [
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.strip()
    ]
    assert len(events) == 1
    assert events[0]["stage"] == "download"
    assert events[0]["downloaded_bytes"] == 5
    assert events[0]["total_bytes"] == 10


def test_interactive_json_keeps_selection_ui_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from trackforge.cli.commands import input as input_command

    monkeypatch.setattr(input_command, "stdin_is_tty", lambda: True)
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["input", URL, "--json", "--out", str(tmp_path)],
        input="\n",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["selections"][0]["variant_id"] == "audio-best"
    assert "Select [" in result.stderr
    assert "Variants:" in result.stderr
