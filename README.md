# trackforge

Tooling to collect, verify, and write metadata for audio and video files.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync
uv run tfg --help
```

The `tfg` command is declared in `pyproject.toml` (`[project.scripts]`), so once
the project is installed it is available directly, without `uv run`.
