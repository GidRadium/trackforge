# trackforge

Tooling to collect, verify, and write metadata for audio and video files.

## Design docs

- [Project idea and roadmap](docs/IDEA.md)
- [Metadata formats and limitations](docs/METADATA_RESEARCH.md)
- [Download research: yt-dlp, sources, cookies, progress](docs/INGESTION_RESEARCH.md)
- [Ingestion architecture: input → variants → download → ingest](docs/INGESTION_ARCHITECTURE.md)
- [Implementation plan: layers, vertical slices, test strategy](docs/INGESTION_IMPLEMENTATION_PLAN.md)

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync
uv run tfg --help
uv run tfg info track.mp3
uv run tfg info --json track.mp3
uv run tfg set track.mp3 --title "New title" --album "New album"
uv run tfg set track.mp3 --cover cover.jpg
uv run tfg set track.mp3 --rating 4
uv run tfg set track.mp3 --clear comment --clear genre
uv run tfg scan music/ --json
uv run tfg scan music/ --progress
uv run tfg scan music/ --limit 100
uv run pytest
```

The `tfg` command is declared in `pyproject.toml` (`[project.scripts]`), so once
the project is installed it is available directly, without `uv run`.

## Known limitations

See [docs/METADATA_LIMITATIONS.md](docs/METADATA_LIMITATIONS.md) for Matroska
native tags, AVI/FLV artwork, ID3v2.3 dates and external tool requirements.

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
DSF/DFF, ASF/WMA/WMV, APE/WavPack/Musepack/TTA, MKV, AVI and FLV. Writes are
atomic by default: tags are applied to a temporary copy, verified by
re-reading, and only then replace the original. Pass
`WritePolicy(remove_missing=True)` to delete fields instead of leaving them
untouched, `WritePolicy(write_legacy=True)` to also write an ID3v1
compatibility block for MP3/AAC, `WritePolicy(id3_version=3)` for older
players that only understand ID3v2.3, or `WritePolicy(write_extra=True)` to
write unknown keys from `media.extra`.

Matroska/WebM, AVI, FLV and MPEG-TS/PS are read through `ffprobe` when it is
installed; MKV/AVI/FLV writes additionally use `ffmpeg`. MPEG-TS/PS are
read-only. Matroska cover art is stored as an attachment; AVI/FLV artwork is
not implemented. These are optional external tools, not Python dependencies.
