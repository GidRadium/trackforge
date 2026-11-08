# trackforge

Tooling to collect, verify, and write metadata for audio and video files.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync
uv run tfg --help
uv run pytest
```

The `tfg` command is declared in `pyproject.toml` (`[project.scripts]`), so once
the project is installed it is available directly, without `uv run`.

## Metadata API

```python
from trackforge.metadata import MediaFile

media = MediaFile.open("track.mp3")
print(media.title, media.artists)
print(media.audio.album, media.audio.track_number)
print(media.artwork.front)
```

`MediaFile.open` detects the container by magic bytes. Audio, video and
artwork are optional facets, so `media.audio` / `media.video` may be `None`.
