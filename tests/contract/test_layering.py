"""Architecture and license-boundary contract tests."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "trackforge"

GPL_OR_AGPL_DEPENDENCIES = {
    "mutagen",
    "essentia",
    "torch",
    "faster_whisper",
    "demucs",
    "onnxruntime",
}
DOMAIN_FORBIDDEN = GPL_OR_AGPL_DEPENDENCIES | {
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "httpx",
    "typer",
    "rich",
}
APPLICATION_FORBIDDEN = GPL_OR_AGPL_DEPENDENCIES | {
    "sqlalchemy",
    "httpx",
    "typer",
}
OUTER_LAYER_MODULES = {
    "trackforge.adapters",
    "trackforge.interfaces",
    "trackforge.bootstrap",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _violations(package: str, forbidden: set[str]) -> set[str]:
    found: set[str] = set()
    for path in (SRC_ROOT / package).rglob("*.py"):
        for imported in _imported_modules(path):
            for banned in forbidden:
                if imported == banned or imported.startswith(f"{banned}."):
                    found.add(f"{path.relative_to(PROJECT_ROOT)}: {imported}")
    return found


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("domain", DOMAIN_FORBIDDEN),
        ("application", APPLICATION_FORBIDDEN),
    ],
)
def test_core_does_not_import_forbidden_dependencies(
    package: str,
    forbidden: set[str],
) -> None:
    assert _violations(package, forbidden) == set()


@pytest.mark.parametrize("package", ["domain", "application"])
def test_core_does_not_import_outer_layers(package: str) -> None:
    assert _violations(package, OUTER_LAYER_MODULES) == set()


def test_import_linter_contracts_pass() -> None:
    lint_imports = Path(sys.executable).with_name("lint-imports")
    assert lint_imports.is_file(), "lint-imports is not installed next to the test interpreter"

    result = subprocess.run(
        [str(lint_imports), "--no-cache"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
