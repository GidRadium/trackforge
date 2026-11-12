"""Text-search CLI stub tests (search is intentionally deferred)."""

# pyright: standard

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trackforge.cli import app
from trackforge.cli.commands import input as input_command

runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["Artist - Track"],
        ["input", "Artist - Track"],
        ["i", "Artist - Track"],
        ["search", "Artist - Track"],
    ],
)
def test_text_search_is_an_explicit_stub(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("search stub must not load config or touch network")

    monkeypatch.setattr(input_command, "load_config", fail_if_called)

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert "text search is not implemented yet" in result.output
    assert "pass a direct URL" in result.output
    assert result.stdout == ""


def test_search_json_still_explains_the_stub() -> None:
    result = runner.invoke(app, ["search", "Artist - Track", "--json"])

    assert result.exit_code == 1
    assert "text search is not implemented yet" in result.stderr
    assert result.stdout == ""


def test_search_help_lists_automation_options() -> None:
    result = runner.invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    for option in ("--limit", "--source", "--json"):
        assert option in result.output
