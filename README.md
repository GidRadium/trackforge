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
uv run tfg doctor --source youtube
uv run tfg doctor --source youtube --check-network
uv run tfg inspect "https://www.youtube.com/watch?v=..." --json
uv run tfg inspect "https://www.youtube.com/playlist?list=..." --flat --limit 20 --json
uv run tfg inspect "https://soundcloud.com/.../sets/..." --flat --limit 20
uv run tfg inspect "https://www.youtube.com/watch?v=..." --cookies cookies.txt
uv run tfg "https://www.youtube.com/watch?v=..." --dry-run --json --yes
uv run tfg "https://www.youtube.com/watch?v=..." --video recommended --yes
uv run tfg "https://www.youtube.com/watch?v=..." --audio recommended --yes
uv run tfg "https://www.youtube.com/playlist?list=..." --flat --limit 5
uv run tfg sources --json
uv run pytest
```

`tfg inspect` is the public L1 acquisition command. It resolves metadata only:
it never downloads media and never asks interactive questions. `--json` prints
the frozen `SourceInfo` v2 object (`schema_version: 2`); the default output is a
human-readable summary of source/kind/title/creator/thumbnail/items/variants.
Each item also exposes `thumbnails`: an ordered best-to-worst candidate list.
`--flat` lists collection entries without resolving their formats and
`--limit` bounds that list; both are also available through
`AcquisitionService.inspect(url, flat=..., limit=..., source=...)`. Exit codes
are `0` success, `1` acquisition/configuration error, `2` invalid arguments.
The JSON contract deliberately contains no cookies, tokens, HTTP headers,
proxy settings, stream URLs or raw yt-dlp `info_dict`.

`tfg URL` (also `tfg input URL` and `tfg i URL`) is the L3 acquisition flow:
it inspects the URL, shows a numbered list of collection items when needed,
marks loader recommendations with `★`, lets the user pick a variant (`a`/`v`
select the recommended audio/video, empty input takes the recommendation or
the first variant), and downloads into the L2 staging area under
`general.output_dir` or `--out`. Registered subcommands such as `tfg info`,
`tfg set`, `tfg scan` and `tfg inspect` always take priority over this
default command. Human mode keeps a single status/progress line at the bottom
of the terminal: it shows what is happening during resolution and rewrites the
download percentage in place instead of appending a new line for every update.

For automation, `--variant ID`, `--audio recommended`,
`--video recommended`, `--yes`, `--json`, `--dry-run` and
`--events jsonl` are available. Without an explicit selection, a non-TTY
invocation prints the item/variant list and exits with code `2`; it never
silently picks the first result. `--yes` uses `recommendations.video`, then
`recommendations.audio`, then the first downloadable variant. A multi-item
playlist still requires an interactive item choice, and the CLI asks for
confirmation before downloading more than one item. `--json` prints the final
result on stdout (source, selections, plan and download result); progress
events go to stderr as JSONL with `--events jsonl`. Exit codes are `0` success,
`1` operational error (including partial playlist failures), `2` selection
required/invalid arguments, and `130` cancellation.

Thumbnail resolution is a common `MediaLoader` contract, not a yt-dlp or
YouTube special case. A flat inspect only builds the ordered candidate list
without network checks. A full inspect rebuilds that list, then tries the
candidates from best to worst and stops at the first working image. Bytes are
kept in an in-memory `ThumbnailCache` shared by inspect and download, so a
sidecar is written from those bytes instead of being fetched again. Proxy and
cookies come from the current source profile; an injected `ctx.http` client is
used when present. This works for YouTube, Yandex Music, SoundCloud and any
future loader using the base contract. Bots/GUIs can use
`AcquisitionService.fetch_thumbnail(...)` to get the same cached asset; the CLI
intentionally has no image preview. Video recommendations are quality-first:
resolution, FPS and dynamic range rank before compatibility preferences, so
H.265/HEVC, VP9 or AV1 can beat a lower-resolution H.264 stream; at equal
quality H.264 still wins as the compatibility tie-breaker.

Text search is not implemented in this checkpoint. `tfg "Artist - Track"` and
`tfg search "Artist - Track"` print an explicit "not implemented yet" message
and exit with code `1`; they never guess a first result or treat the query as a
URL. `tfg sources` lists loaders, their config profiles, readiness and offline
capabilities (`--json` for automation).

`tfg doctor` is offline by default. It checks the selected source profile,
cookie file permissions/format, proxy URL, yt-dlp, yt-dlp-ejs, Deno, ffmpeg and
the Yandex Music plugin without contacting the network. Network access happens
only with `--check-network`; `--cookies PATH` and `--proxy URL|env|none` are
one-shot overrides for that run. The configured cookie file is passed to
yt-dlp directly, so yt-dlp may refresh it in place.

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
