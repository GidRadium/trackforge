# Plan 06: Local ingest + probe

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 05
- **Разблокирует:** Plan 07, 09
- **Оценка:** L
- **Риск:** средний
## Роль

Ты — senior Python engineer, отвечающий за первый вертикальный срез данных. Работаешь в `trackforge` после pipeline. Задача — локальный файл: план, копирование в CAS, `ffprobe`, чтение существующих тегов и слабая структура пути. Никакого resolve/render.

## Цель

Сценарий «локальный файл → `MediaObject` в CAS → `audio.probe.v1` + `initial.tags.v1` + `path.structure.v1`» работает через application use cases и pipeline, не изменяя оригинальный файл. Это первый наблюдаемый срез TrackForge.

## Контекст и инварианты

- Оригинал не изменяется; acquire копирует байты в CAS (`01-domain-model.md` §4.5, `ADR-0003`).
- `MediaObject.content_hash` — SHA-256 исходных байтов; `Intake` хранит происхождение.
- Probe — первый обязательный observer, не отдельная top-level стадия (`13-consistency-review.md` §3, п.1).
- `ffprobe` не доверяем тегам; теги — отдельное `initial.tags.v1`.
- Существующие теги — слабое evidence, не «истина»; исходник не очищается.
- Поддерживаемые расширения MVP: `.mp3`, `.m4a`, `.flac`, `.ogg`, `.opus`.
- Сеть не используется; fingerprint/AcoustID — Plan 12.
- Папка как `Collection` — Plan 18; здесь только один файл.
## Прочитать перед началом

- `docs/design/05-sources.md` — §3.
- `docs/design/03-pipeline.md` — §2.2–2.4, §3.3, §11.
- `docs/design/06-analysis.md` — §3.1–3.3.
- `docs/design/01-domain-model.md` — §4.4–4.6, §6.1.
- `docs/design/04-storage.md` — §3, §6.4.
- `docs/design/adr/0003-content-addressed-storage.md`.
- `src/trackforge/pipeline/`, `src/trackforge/application/ports/`, `src/trackforge/adapters/persistence/`.
- `docs/plan/05-pipeline-engine.md` (handoff).

## Предусловия

```bash
uv --version
ffmpeg -version
ffprobe -version
uv sync --extra tagging
uv run ruff check .
uv run mypy src
uv run pytest tests/pipeline -q
uv run lint-imports
```

Если `ffmpeg`/`ffprobe` нет — стоп: integration-тесты и acceptance невозможны. Если `mutagen` extra не установлен — установи extra, это не системный пакет.
## Scope

- `adapters/sources/local.py`: `LocalFileSource`.
- `adapters/acquisition/local_copy.py`: копирование в CAS.
- `adapters/analysis/ffprobe.py`: subprocess `ffprobe`.
- `adapters/analysis/path_structure.py`: `path.structure.v1`.
- `adapters/tagging/mutagen/reader.py`: read-only теги.
- `application/use_cases/plan_item.py`, `acquire_item.py`, `observe_item.py`.
- `application/dto/plan.py`, `application/dto/observations.py`.
- Capability-регистрация в bootstrap.
- `[project.optional-dependencies] tagging = ["mutagen>=1.47"]`; лицензия GPL-2.0-or-later, только адаптер.
- `tests/fixtures/media.py` — генерация 1–5-секундных файлов через ffmpeg.
- Тесты unit/integration.
## Non-goals

- Resolve/decisions/render/publish.
- Fingerprint/AcoustID/MusicBrainz/cover/lyrics.
- Папки/коллекции, плейлисты.
- URL/yt-dlp.
- Запись тегов (Plan 08); здесь только чтение.
- Транскодирование/remux.
## Deliverables

```text
src/trackforge/adapters/sources/local.py
src/trackforge/adapters/acquisition/local_copy.py
src/trackforge/adapters/analysis/ffprobe.py
src/trackforge/adapters/analysis/path_structure.py
src/trackforge/adapters/tagging/mutagen/reader.py
src/trackforge/application/ports/{sources,acquisition,observations}.py
src/trackforge/application/use_cases/plan_item.py
src/trackforge/application/use_cases/acquire_item.py
src/trackforge/application/use_cases/observe_item.py
src/trackforge/application/dto/plan.py
src/trackforge/application/dto/observations.py
tests/fixtures/media.py
tests/adapters/{test_local_source,test_ffprobe,test_initial_tags}.py
tests/application/test_local_ingest.py
tests/integration/test_local_e2e_observe.py
```
## Контракты
### LocalFileSource

```text
LocalFileSource:
  id = "local_file"
  version
  supports(ref: SourceRef) -> bool
  resolve(ref: SourceRef, opts) -> CandidateList
```

- `supports` true только для `LocalPath` с существующим regular file и расширением из MVP-набора.
- `resolve` не копирует файл; возвращает `Candidate` с `source_ref=LocalPath`, `external_id=path string`, `title=stem`, `duration=None`, `formats=[{container: ext, size_bytes}]`.
- Symlink: разрешён, если цель — regular file; путь не должен быть directory.
- Unsupported extension → `UnsupportedSource` с `user_action`.

### Acquire

```text
Acquirer.acquire(candidate, opts) -> IntakeResult
IntakeResult(intake, media, file_instance, source_snapshot_ref?)
```

- Читает файл потоком, считает SHA-256, кладёт в CAS через `ContentAddressedStore.put_file`.
- Создаёт `MediaObject` (container/codec/bitrate/sample_rate/channels/duration — из probe
  или `None` до observe), `Intake(status=succeeded, downloader_id="local_copy")`,
  `FileInstance(role=source, path=original, size, mtime)`, `Item(status=acquired)`.
- Идемпотентность: если `MediaObject` с таким hash уже есть — переиспользовать; новый `Intake`/`Item` для другого источника/пути допустим (item_id не hash).
- Оригинал не открывается на запись; mtime/hash не меняются.
- Ошибки: `AcquisitionFailed` (нет прав, файл исчез), `UnsupportedSource`.

### Probe

`FfprobeProbe` — capability:

```text
ffprobe -v error -show_format -show_streams -of json <path>
```

- Timeout 30 с; stderr ограничен; нет shell.
- `audio.probe.v1` payload: `container`, `format_name`, `duration_seconds`, `size_bytes`, `bit_rate`, `streams[]` (`codec_type`, `codec_name`, `sample_rate`, `channels`, `bit_rate`, `duration_seconds`), `chapters[]`, `attached_pictures[]`.
- Теги из probe не используются как `initial.tags`; они могут быть сохранены в probe payload, но не как решения.
- `ffprobe` отсутствует → `ProviderUnavailable` / `Failed`, не тихий `None`.
- Capability manifest: requires media; provides `audio.probe.v1`; deterministic; cacheable; cost low.

### Initial tags

`InitialTagsObserver` — capability:

- Читает через `mutagen` заголовки: title, artist, album, albumartist, date, genre, track/disc, comment, lyrics, MBIDs, embedded artwork.
- `initial.tags.v1` payload: нормализованный словарь + `raw` (тип файла/формат) + `reader` id/version; без байтов аудио.
- Не изменяет файл; если mutagen не установлен → `skipped` + warning `tag_reader_missing`.
- Capability: requires media; provides `initial.tags.v1`; deterministic; cacheable.

### Path structure

`path.structure.v1` payload: `path`, `parent_names[]`, `stem`, `suffix`; не парсит `Artist/Album/Track` в claims (это Plan 07), только наблюдение.

### Use cases

```text
PlanItem.plan(source: SourceRef, options, resolvers: list[SourceResolver]) -> PlanView
AcquireItem.acquire(candidate, options, acquirers) -> ItemView
ObserveItem.observe(item_id: ItemId, profile) -> ObservationView
```

- `PlanView`: source, candidates, steps, estimated_cost, warnings, blocked_reason.
- `AcquireItem` создаёт run/step? Достаточно item/intake; run создаётся в Plan 09 `ProcessItem`.
- `ObserveItem` строит DAG на `audio.probe.v1`, `initial.tags.v1`, `path.structure.v1`; выполняет через executor; сохраняет artifact IDs в item; статус `observed`.
- Все методы async; ошибки типизированы.
## Порядок работ

1. Добавь `mutagen` extra и reader; тесты на временных файлах.
2. Реализуй `LocalFileSource` и DTO `PlanView`.
3. Реализуй `LocalCopyAcquirer` через CAS и репозитории.
4. Реализуй `FfprobeProbe`, `InitialTagsObserver`, `PathStructureObserver` как capability.
5. Реализуй `PlanItem`/`AcquireItem`/`ObserveItem`.
6. Подключи capability в bootstrap; настрой desired outputs для `observe`.
7. Добавь генератор медиа-фикстур и integration-тест.
8. Прогони quality gates; закоммить.
## Тесты

- **unit:** source supports/extension/symlink; candidate fields; path structure; ffprobe JSON parser; initial tags mapping; idempotent acquire.
- **contract:** `LocalFileSource`/`Acquirer`/`Capability` соответствуют протоколам; `initial.tags.v1` schema; `audio.probe.v1` schema.
- **integration:** 5 форматов (MP3/M4A/FLAC/OGG/Opus) через ffmpeg: import → CAS → probe → tags; original hash/mtime unchanged; повторный acquire не создаёт второй CAS-файл.
- **fixtures:** `tests/fixtures/media.py` генерирует sine/silence, mono/stereo, с тегами и без; `tmp_path`.
- **negative:** unsupported extension; corrupt file; missing ffprobe; mutagen missing; file disappears between plan/acquire; path is directory.
## Проверка

```bash
uv sync --extra tagging
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters tests/application/test_local_ingest.py -q
uv run pytest tests/integration/test_local_e2e_observe.py -q
uv run lint-imports
```

Ожидается: 5 форматов проходят; `audio.probe.v1` содержит duration/codec; `initial.tags.v1` содержит title, если тег был; оригинал не изменён.
## Acceptance criteria

- [ ] `uv run pytest tests/adapters tests/application/test_local_ingest.py tests/integration/test_local_e2e_observe.py -q` зелёный.
- [ ] Локальный MP3/M4A/FLAC/OGG/Opus проходит plan → acquire → observe.
- [ ] `MediaObject.content_hash` совпадает с `sha256sum` исходника; в CAS ровно один объект на hash.
- [ ] Оригинальный файл не изменён (hash/mtime до/после).
- [ ] `audio.probe.v1` и `initial.tags.v1` — валидные артефакты с manifest; повторный observe — cache hit.
- [ ] Существующие теги читаются, но не стираются и не становятся решениями.
- [ ] `ffprobe` отсутствует → понятная ошибка, не пустой observation.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- `LocalFileSource`, `LocalCopyAcquirer`.
- `PlanItem`/`AcquireItem`/`ObserveItem`.
- `audio.probe.v1`, `initial.tags.v1`, `path.structure.v1`.
- `MediaObject`/`Intake`/`FileInstance`/`Item` в каталоге.
- `[tagging]` extra и read-only mutagen adapter.
- Медиа-фикстуры.

## Stop conditions / decision gates

- Нет `ffmpeg`/`ffprobe` — стоп; не подменяй probe фейком в acceptance.
- Нужно писать теги/renaming — Plan 08/09.
- Нужны папки/плейлисты — Plan 18.
- `mutagen` нельзя ставить как extra — стоп и зафиксируй D5; не добавляй его в домен.
- Если файл — symlink за пределы разрешённого root, политика пока: только явный путь пользователя; не следуй цепочке без ограничения.
