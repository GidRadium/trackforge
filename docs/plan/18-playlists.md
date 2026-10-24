# Plan 18: Playlists / collections

- **Статус:** conditional
- **Тип:** Post-MVP
- **Зависит от:** Plan 09, Plan 11, Plan 12
- **Разблокирует:** —
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за пакетную обработку. Работаешь в `trackforge` после URL ingest. Задача — коллекции, выбор треков, очередь с паузами/resume, subfolder и `tag_basis`; без параллельного «качаем всё сразу».

## Цель

Работает `trackforge add <playlist-url>` (и локальная папка): playlist → `Collection` с позициями/контекстом → выбор/диапазон → поштучная обработка с rate limits → файлы в подпапке коллекции. Resume продолжает с необработанных; один упавший трек не ломает остальные; до 500 треков.

## Контекст и инварианты

- `Collection` — контейнер, не трек; `context_claims` — отдельные claims (`01-domain-model.md` §4.3).
- `Collection(kind=album)` ≠ `Release`; не смешивать.
- Playlist enumeration: `--flat-playlist --dump-single-json`, без скачивания (`05-sources.md` §4.7).
- Скачивание по одному; лимиты/паузы; не параллелить aggressively; `--ignore-errors` на уровне коллекции.
- Целевой максимум — 500 треков; больше — Deferred.
- `tag_basis`: `album_first` default (D9), `playlist_first`, `ask`; пользователь может переопределить для коллекции.
- Layout: подпапка коллекции для playlist/album; одиночный — flat (`Q31`).
- Partial failures: item status `failed`/`skipped`; run продолжается.
- Resume: `CollectionItem.status` + step state; не качать/не анализировать повторно.
- Ban avoidance: sleep/rate limits, cookies, no infinite retries.

## Прочитать перед началом

- `docs/design/05-sources.md` — §4.7, §8.
- `docs/design/01-domain-model.md` — §4.3, §4.6.
- `docs/design/03-pipeline.md` — §2.8, §6, §9.
- `docs/design/07-resolution.md` — §8.
- `docs/design/04-storage.md` — §8.
- `docs/design/09-interfaces.md` — §3.
- `docs/plan/11-url-ingest-ytdlp.md`, `docs/plan/12-metadata-providers.md`, `docs/plan/09-publish-cli-local-mvp.md`.
- `docs/plan/artifacts/10-spike-ytdlp.md`.

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/cli/test_add_url.py tests/integration/test_local_e2e.py tests/integration/test_metadata_resolve.py -q
uv run lint-imports
```

Зелёные. Plan 11 работает (URL) или как минимум local folder; если Plan 11 no-go, Plan 18 может делать только local folder (conditional).

## Scope

- Migration `0004_collections.py`: `collections`, `collection_items`.
- `adapters/sources/local_folder.py`: `LocalFolderSource` → `Collection(kind=folder)`.
- `adapters/sources/ytdlp/playlist.py` — расширение `YtDlpSource`.
- `application/use_cases/plan_collection.py`, `process_collection.py`, `resume_collection.py`.
- `application/services/collection_queue.py`: очередь, паузы, per-item status.
- `application/services/tag_basis.py`: album_first/playlist_first/ask.
- CLI `add`/`plan` для коллекций; `--playlist-start/end/items`, `--skip`, `--dry-run`, `--resume`.
- Tests fake playlist + folder.

## Non-goals

- Параллельная обработка нескольких item (позже).
- >500 треков, channels, albums из API (Deferred).
- GUI/HTTP collection UI.
- Автоматический выбор «лучшего» трека по качеству beyond format selection.
- Ban evasion/обход ToS.
- Мультибиблиотечность.

## Deliverables

```text
src/trackforge/adapters/sources/local_folder.py
src/trackforge/adapters/sources/ytdlp/playlist.py
src/trackforge/application/use_cases/plan_collection.py
src/trackforge/application/use_cases/process_collection.py
src/trackforge/application/use_cases/resume_collection.py
src/trackforge/application/services/collection_queue.py
src/trackforge/application/services/tag_basis.py
src/trackforge/interfaces/cli/commands/collection.py
migrations/versions/0003_collections.py
tests/adapters/test_local_folder.py
tests/application/test_collection_queue.py
tests/application/test_tag_basis.py
tests/integration/test_playlist_resume.py
tests/cli/test_collection_cli.py
```

## Контракты

### Collection

```text
Collection(id, kind: playlist|album|channel|folder|other, source_ref,
           title, owner, description, context_claims[], items[], created_at)
CollectionItem(position, candidate, item_id?, status: planned|acquired|processed|skipped|failed)
```

- `Collection.id` — UUIDv7; `CollectionItem` PK `(collection_id, position)`.
- `context_claims` — title/owner/description/cover/date; не заменяют recording claims.
- `CollectionItem.status` — единственный источник resume для коллекции; обновляется после каждого item.
- `Collection` хранится в SQLite; `item_id` появляется после acquire.

### Enumeration / plan

- `YtDlpSource.resolve` для playlist URL: `--flat-playlist --dump-single-json --no-download`.
- `LocalFolderSource`: расширения MVP; сортировка по имени; `context_claims` из имён папок — только гипотезы, confidence низкая.
- `PlanCollection`: `Collection` + `PlanView` (items, estimated cost, warnings, limits); не скачивает.
- `--playlist-start/end/items` — фильтр; `--skip` — позиции; max 500; больше → `CollectionTooLarge` (или явный cap с warning).
- `--dry-run` — список/план без acquire.
- `tag_basis` — параметр коллекции: `album_first` (D9), `playlist_first`, `ask`; default из config.
- `ask` в `--auto` → `album_first`; interactive — вопрос; `--json` без вопроса → default + warning.

### Queue / process

- `ProcessCollection`: по одному item; `ProcessItem`; pause между items (`sleep`/rate limiter), не параллельно.
- Per-item failure: `CollectionItem.status=failed`, run продолжается; `--fail-fast` — опция.
- `--resume`: найти `CollectionItem.status in planned/acquired`, проверить артефакты; обработанные не повторять.
- `--force`: пересчитать выбранные steps, не удалять артефакты.
- Pause/cancel: cooperative; после текущего item; состояние в SQLite.
- Rate limits: per-host; `--min-sleep`/config; не агрессивно.
- `LibraryEntry` — subfolder `<collection-title>/`; sanitize; collision suffix.
- Sidecar хранит `collection` и `tag_basis`; альтернативные значения (album vs playlist) — в sidecar.

### tag_basis

| tag_basis | album | albumartist | track | date | cover |
|---|---|---|---|---|---|
| album_first (default) | release | release artist | release track | release date | release cover |
| playlist_first | playlist title | owner/Various Artists | playlist position | playlist date | playlist cover |
| ask | спросить | спросить | спросить | спросить | спросить |

- `album_first` при отсутствии release деградирует до playlist/unknown с warning.
- `playlist_first` не выдаёт playlist owner за артиста трека; `Various Artists` для compilation.
- Tag decisions фиксируют `tag_basis` и альтернативы.

## Порядок работ

1. Миграция collections/collection_items + репозитории.
2. `LocalFolderSource` и playlist enumeration на fixtures.
3. `PlanCollection`/`CollectionQueue`.
4. `ProcessCollection`/resume/partial failures.
5. `tag_basis` и subfolder publish.
6. CLI `collection`/`add`/`plan`.
7. Тесты resume/failure/limits; прогони quality gates.
8. Закоммить.

## Тесты

- **unit:** collection/item status; enumeration limit 500; filters; tag_basis mapping; subfolder sanitize; queue pause.
- **contract:** `Collection` schema; `CollectionItem` uniqueness; playlist JSON fixture; local folder sorting.
- **integration (fake yt-dlp):** 5-item playlist → 5 outputs в подпапке; resume after 2; one failed doesn't stop; no duplicate downloads; `--dry-run` no acquire.
- **fixtures:** `playlist_5.json`, local folder 5 files, failed candidate, long collection.
- **negative:** >500; duplicate position; missing item id; invalid tag_basis; failed download; pause/cancel; path traversal collection title.

## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/test_local_folder.py tests/application/test_collection_queue.py tests/application/test_tag_basis.py tests/cli/test_collection_cli.py -q
uv run pytest tests/integration/test_playlist_resume.py -q
uv run lint-imports
uv run trackforge plan "<playlist-url>" --json   # fake yt-dlp из тестов
```

Ожидается: 5-item fake playlist планируется; resume после 2 не повторяет первые; один failed не ломает остальные; подпапка коллекции; `tag_basis` влияет на решения.

## Acceptance criteria

- [ ] `uv run pytest tests/adapters/test_local_folder.py tests/application/test_collection_queue.py tests/application/test_tag_basis.py tests/cli/test_collection_cli.py tests/integration/test_playlist_resume.py -q` зелёный.
- [ ] Playlist 5 треков (fake) → 5 файлов в `<library>/<collection>/`; sidecar рядом.
- [ ] Resume после 2 обработанных не скачивает/не рендерит их снова.
- [ ] Один failed item не останавливает остальные; статус `failed` виден.
- [ ] Max 500 enforced; >500 — понятная ошибка/warning.
- [ ] `tag_basis=album_first/playlist_first` меняет album/albumartist/track/cover; sidecar хранит альтернативу.
- [ ] `--dry-run` не скачивает; `--playlist-start/end/items` работают.
- [ ] Path traversal в collection title отклоняется.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.

## Handoff

Следующие планы могут считать готовыми:

- `Collection`/`CollectionItem`/миграция.
- Playlist/folder enumeration; queue/resume/partial failures.
- `tag_basis`; subfolder publish.
- CLI collection commands.
- Fake playlist fixtures.

## Stop conditions / decision gates

- D9 не выбран — `album_first` с fallback; зафиксируй.
- Plan 11 no-go — только local folder; playlist URL `deferred`.
- Нужно >500/параллельно — Deferred; не расширяй.
- Нужен ban evasion — стоп; только rate limits/cookies.
- Один item ломает весь run — стоп и исправь обработку ошибок.
- Нужна album/release нормализация — Plan 12 claims; не создавай identity-таблицы.
