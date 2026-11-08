# trackforge

Tooling to collect, verify, and write metadata for audio and video files.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync
uv run tfg --help
uv run tfg info track.mp3
uv run tfg info --json track.mp3
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

```python
media.title = "New title"
media.audio.album = "New album"
report = media.save()
print(report.written, report.skipped)
```

Writing is currently supported for MP3/AAC, FLAC, OGG, MP4/M4A, WAV, AIFF,
DSF/DFF, MKV, AVI and FLV. Writes are atomic by default: tags are applied to a
temporary copy, verified by re-reading, and only then replace the original.
Pass `WritePolicy(remove_missing=True)` to delete fields instead of leaving
them untouched, or `WritePolicy(write_legacy=True)` to also write an ID3v1
compatibility block for MP3/AAC.

Matroska/WebM, AVI, FLV and MPEG-TS/PS are read through `ffprobe` when it is
installed; MKV/AVI/FLV writes additionally use `ffmpeg`. MPEG-TS/PS are
read-only. These are optional external tools, not Python dependencies.
