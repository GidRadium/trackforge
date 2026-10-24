# Plan 08: Render + tag writer

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 04, 07
- **Разблокирует:** Plan 09
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за материализацию результата. Работаешь в `trackforge` после resolve. Задача — собрать output-файл с каноническими тегами, обложкой, plain lyrics и sidecar, проверить чтением; без публикации в библиотеку.

## Цель

Появляется `RenderItem`: `DecisionRecord` + `MediaObject` → `OutputBundle` в `items/<item_id>/renders/<render_id>/` для MP3/M4A/FLAC/OGG/Opus. Теги пишутся в копию, оригинал в CAS не изменяется; LRC/ELRC — только sidecar; `trackforge.json` валиден; read-back подтверждает канонические поля.

## Контекст и инварианты

- Оригинал не изменяется; render работает с копией/транскодом (`01-domain-model.md` §10, `ADR-0003`).
- Каноническая модель — одна; формат — отображение (`08-metadata.md` §1).
- LRC/ELRC — только sidecar; plain lyrics — в тег, если формат поддерживает (Q30).
- Обложка встраивается; `cover.jpg` sidecar — опционально, по умолчанию нет.
- Read-back обязателен; mismatch → `RenderFailed`, bundle не публикуется.
- Temp + fsync + rename; не писать в исходный файл.
- ID3v2.4 по умолчанию, ID3v2.3 — опция; MP4 freeform для MBID; Vorbis multi-value.
- `mutagen` — только адаптер, extra `[tagging]`; ядро его не импортирует.

## Прочитать перед началом

- `docs/design/08-metadata.md` — полностью.
- `docs/design/03-pipeline.md` — §2.7, §3.3.
- `docs/design/01-domain-model.md` — §10.
- `docs/design/04-storage.md` — §5, §7.
- `docs/design/07-resolution.md` — §11.
- `docs/design/02-architecture.md` — §5, §9.5.
- `src/trackforge/application/use_cases/resolve_item.py`, `src/trackforge/domain/model/outputs.py`.
- `docs/plan/07-resolution-core.md` (handoff).

## Предусловия

```bash
uv sync --extra tagging
ffmpeg -version
ffprobe -version
uv run ruff check .
uv run mypy src
uv run pytest tests/application/test_resolve.py -q
uv run lint-imports
```

Зелёные. `ffmpeg`/`ffprobe` обязательны. `mutagen` extra установлен; без него render не закрывает acceptance.

## Scope

- `application/ports/rendering.py`: `Renderer`, `TagReader`, `TagWriter`.
- `application/use_cases/render_item.py`.
- `application/dto/rendering.py`: `RenderOptions`, `RenderResult`, `TagSet`.
- `adapters/rendering/ffmpeg.py`: remux/transcode.
- `adapters/tagging/mutagen/writer.py`: запись/удаление полей, cover, lyrics.
- `adapters/tagging/mutagen/reader.py` — расширить read-back.
- `domain/policies/naming.py`? Нет: имя в workspace фиксированное; финальный naming — Plan 09.
- `sidecar` builder в `application/services/sidecar.py`.
- Workspace layout, `renders`/`output_bundles` rows.
- Тесты round-trip 5 форматов.

## Non-goals

- Publish/managed library/renaming/collisions — Plan 09.
- Сеть/cover providers/lyrics providers — Plan 12.
- ASR/Demucs — Plans 16/17.
- In-place тегирование, backup/undo.
- ReplayGain, sort-поля, style/mood — optional; не в MVP.
- Транслитерация имён файлов.

## Deliverables

```text
src/trackforge/application/ports/rendering.py
src/trackforge/application/use_cases/render_item.py
src/trackforge/application/dto/rendering.py
src/trackforge/application/services/sidecar.py
src/trackforge/adapters/rendering/__init__.py
src/trackforge/adapters/rendering/ffmpeg.py
src/trackforge/adapters/tagging/mutagen/__init__.py
src/trackforge/adapters/tagging/mutagen/writer.py
src/trackforge/adapters/tagging/mutagen/reader.py
tests/adapters/tagging/test_roundtrip.py
tests/adapters/tagging/test_cover_lyrics.py
tests/application/test_render.py
tests/integration/test_render_formats.py
```

## Контракты

### RenderOptions / TagSet

```text
RenderOptions:
  target_format: mp3 | m4a | flac | ogg | opus | None   # None = source if supported, иначе flac
  id3_version: 3 | 4 = 4
  cover_embed: bool = True
  cover_sidecar: bool = False
  lyrics_sidecar: bool = True
  preserve_unknown_tags: bool = False

TagSet: canonical fields из `08-metadata.md` §2.1 (title, artists, albumartist,
  album, date, originaldate, track/disc, genre, bpm, key, language, label,
  catalog, barcode, isrc, compilation, comment, description, lyrics, source,
  mbids, trackforge). `None`/пустая строка не пишутся; synced lyrics — sidecar.
```

- `artists` — main/featured join для тега `artist`; полный список — в sidecar.

### Renderer

```text
Renderer.render(media: MediaObject, decisions: ResolvedMetadata, options: RenderOptions,
                workdir: Path) -> RenderedAudio
```

- Если source container == target: remux/copy без перекодирования; иначе `ffmpeg` transcode в целевой формат.
- Никогда не писать в `cas_path`; output — `workdir/track.<ext>`.
- Кодек/битрейт: не ухудшать без необходимости; для transcode — разумный default, параметры в options (не расширять сверх нужного).
- Ошибка ffmpeg → `RenderFailed` с stderr (redacted, ограниченный).

### TagWriter / TagReader

```text
TagWriter.write(path, tagset, cover: CoverArt | None, options) -> None
TagWriter.verify(path, tagset, cover: CoverArt | None) -> list[TagMismatch]
TagReader.read(path) -> TagSet
```

- `write`: удалить конфликтующие старые поля (artwork, lyrics, MBIDs, genre, artists), затем записать новые; unknown tags — сохранять только при `preserve_unknown_tags=True`.
- `verify`: read-back и сравнение; mismatch — список; render fail, если непустой.
- Cover: JPEG/PNG, front, MIME; MP3 APIC, M4A `covr`, FLAC picture block type 3, Ogg/Opus `METADATA_BLOCK_PICTURE`.
- Plain lyrics: MP3 `USLT`, M4A `©lyr`, FLAC/OGG/Opus `LYRICS`/`UNSYNCEDLYRICS`; LRC в тег не писать.
- ID3v2.4 multi-value null-separated; v2.3 — разделитель с caution (тесты).
- MP4 freeform: `----:com.apple.iTunes:MusicBrainz Track Id` и т.п.
- Vorbis: uppercase keys, repeated `ARTIST`/`GENRE`.
- `trackforge` custom tags: `TRACKFORGE_SOURCE_URL`, `TRACKFORGE_ITEM_ID`, `TRACKFORGE_DECISION_ID`, `TRACKFORGE_DERIVATIVE`; без огромного description.

### OutputBundle / sidecar

Workspace: `items/<item_id>/renders/<render_id>/` с `track.<ext>`, optional `cover.jpg`/`lyrics.lrc`, `trackforge.json`.
`trackforge.json` (schema_version 1):
  trackforge: item_id, content_hash, decision_record_id, bundle_hash, created_at
  source: url/extractor/source_id
  metadata: canonical values (null/[] допустимы)
  identity: kind, derivative[], recording_mbid, release_mbid
  artifacts: probe/features/lyrics refs; warnings[]; unknowns[]

- `metadata` — канонические значения; `null`/пустые списки допустимы.
- Секретов/куки/URL с токенами нет.
- `bundle_hash` = SHA-256 канонического JSON файлов (relative path + sha256) + sidecar hash.
- `OutputBundle.files[]` содержит audio, optional cover, optional lrc, sidecar; `bundle_hash`; `created_at`.
- `renders`/`output_bundles` сохраняются в SQLite; при read-back mismatch строки не создаются.

## Порядок работ

1. Реализуй `TagSet`/`RenderOptions` и маппинг canonical → формат.
2. Реализуй `TagReader` (read-back) и тесты на существующих файлах.
3. Реализуй `FfmpegRenderer` (copy/remux/transcode) с temp+rename.
4. Реализуй `MutagenTagWriter` для 5 форматов; удаление конфликтов; cover; lyrics.
5. Реализуй `verify` и `RenderItem` (workspace, bundle, sidecar, DB rows).
6. Добавь тесты round-trip и negative read-back.
7. Прогони quality gates; закоммить.
## Тесты

- **unit:** TagSet → формат mapping; ID3 frame names; MP4 atoms; Vorbis keys; sidecar JSON schema; bundle hash stability; `None` не пишется.
- **contract:** `TagWriter`/`TagReader`/`Renderer` fake; mutagen adapter соответствует протоколу; `domain` не импортирует mutagen.
- **integration:** 5 форматов × (title/artist/album/date/track/genre/BPM/key/language/comment/lyrics/cover/MBID) write→read→compare; ID3v2.3 и 2.4; MP4 freeform; Ogg picture; Unicode.
- **fixtures:** 1–5-секундные MP3/M4A/FLAC/OGG/Opus, PNG 100×100, LRC с 2–3 строками; `tmp_path`.
- **negative:** corrupt audio; read-only dir; cover > limit; LRC в plain lyrics; mismatch (подменить expected); unsupported format; original CAS hash after render unchanged.

## Проверка

```bash
uv sync --extra tagging
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/tagging tests/application/test_render.py -q
uv run pytest tests/integration/test_render_formats.py -q
uv run lint-imports
```

Ожидается: 5 форматов проходят round-trip; read-back mismatch даёт `RenderFailed`; LRC только sidecar; `trackforge.json` валиден.

## Acceptance criteria

- [ ] `uv run pytest tests/adapters/tagging tests/integration/test_render_formats.py -q` зелёный.
- [ ] MP3/M4A/FLAC/OGG/Opus: title/artist/album/date/track/genre/BPM/key/cover читаются обратно.
- [ ] ID3v2.4 и 2.3 проходят; MP4 freeform MBID читается; Ogg picture block читается.
- [ ] Plain lyrics в теге; LRC/ELRC только `.lrc` sidecar; LRC не встроен в plain.
- [ ] `trackforge.json` соответствует schema_version 1 и не содержит секретов.
- [ ] `bundle_hash` стабилен; повторный render тех же decisions даёт тот же hash (при фиксированных IDs/времени).
- [ ] Read-back mismatch → `RenderFailed`, `output_bundles` не создаётся.
- [ ] Оригинальный CAS-объект не изменён; render пишет только в workspace.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- `RenderItem`/`RenderOptions`/`TagSet`.
- 5 форматов tag writer/read-back; cover; plain lyrics; LRC sidecar.
- `OutputBundle` + `trackforge.json` + `bundle_hash`.
- `renders`/`output_bundles` записи.
- Round-trip тесты и медиа-фикстуры.

## Stop conditions / decision gates

- `mutagen` не установлен — установи extra; не заменяй writer самописным ID3.
- Нужен publish/renaming — Plan 09.
- Нужны cover/lyrics провайдеры — Plan 12.
- LRC требуется встроить — это противоречит Q30; стоп.
- Формат не поддерживается mutagen — не добавляй новый формат в v1; зафиксируй `Deferred`.
