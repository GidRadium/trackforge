# L4 — ingest handoff

**Предусловие:** [L3_SELECTION_CLI.md](./L3_SELECTION_CLI.md)  
**Следующий чекпоинт:** [L5_EXTRA_SOURCES.md](./L5_EXTRA_SOURCES.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../METADATA_RESEARCH.md](../METADATA_RESEARCH.md)

---

## Контекст для нового чата

L2 скачивает в staging, L3 даёт выбор. Теперь нужно передать файл в существующий metadata-слой, получить финальный путь и sidecars. Это **единственный** слой, который пишет теги/финальные имена.

**Не делать:** сетевой resolve, скачивание, VK/Pinterest. Идентификация MusicBrainz/AcoustID — следующий проект после L4; здесь только handoff.

---

## Цель

Из `DownloadedFile` получить:

- финальный файл с корректным именем;
- записанные метаданные (через `trackforge.metadata`);
- sidecars `.lrc`/`.elrc`/`cover`/`.info.json` — по политике;
- атомарный перенос из staging;
- отчёт/warnings.

---

## Задачи

### 1. `IngestPipeline`

`src/trackforge/ingest/pipeline.py`:

- вход `DownloadedFile` + `MediaItem` + `IngestOptions`;
- `MediaFile.open(staging_path)`;
- при необходимости probe;
- финальные метаданные из source hints (пока без AcoustID);
- `MediaFile.save()` — единственный писатель;
- `OutputPlanner` → финальный путь;
- `StagingArea.commit_group(media, sidecars)`;
- `IngestResult(media, sidecars, warnings, write_report)`.

### 2. Финальные имена

`src/trackforge/ingest/naming.py`:

- `NamingMode.FLAT` — default;
- `NamingMode.TEMPLATE` — opt-in;
- `DEFAULT_FLAT = "{artist} - {title}"`;
- `DEFAULT_TEMPLATE = "{artist}/{album}/{track:02d} - {title}"`;
- `CollisionPolicy.SUBFOLDER` default (`1/`, `2/`);
- sanitize: Windows reserved, `..`, absolute, trailing dots/spaces, NFC, max length;
- fallback: `title` → `suggested_stem` → `id`;
- никаких `(1)`.

### 3. Sidecars

`src/trackforge/ingest/sidecars.py`:

- `<stem>.lrc`, `<stem>.elrc`;
- `<stem>.cover.jpg`;
- `<stem>.info.json` только по `--keep-info-json`;
- sidecars переезжают в ту же `1/`, `2/`, что и медиа;
- `.lrc` — line timestamps; `.elrc` — только при word timings;
- plain lyrics — в тег, не в фейковый `.lrc`.

### 4. Metadata mapping

Использовать существующий `trackforge.metadata`:

- title/artists/album/album_artist/track/disc/date/genre/comment;
- `MediaFile.save()` atomic;
- не перезаписывать пользовательские правки (если они есть);
- при неподдерживаемом поле — `WriteReport.skipped`, warning;
- не писать ID3v1 без флага.

### 5. CLI/API

- `--no-rename`, `--no-tag`, `--dry-run`;
- `--flat`/`--template`;
- `--keep-info-json`;
- `tfg URL` после download автоматически ingest;
- `--json` — финальный путь/sidecars/warnings.

### 6. Тесты

- `test_output_planner.py` — flat/template/sanitize/collision;
- `test_sidecars.py` — lrc/elrc/cover/info, group move;
- `test_ingest_pipeline.py` — staging → final, tags written;
- `test_no_rename.py` / `--no-tag`;
- `test_collision_group.py` — media+sidecars в `1/`;
- `test_cli_ingest.py`.

---

## Ручная проверка на реальных данных

1. Скачать один файл через CLI, проверить финальный путь:

```bash
uv run tfg "https://..." --audio recommended --yes
```

Ожидаемо:

- файл `Author - Title.ext` в output dir;
- sidecars с тем же stem;
- теги записаны;
- при повторе — `1/Author - Title.ext`, не `(1)`.

2. `--template`:

```bash
uv run tfg "https://..." --template "{artist}/{album}/{track:02d} - {title}"
```

3. `--no-tag`/`--no-rename`/`--dry-run`.

4. Проверить `MediaFile.open(final_path)` — title/artist/album.

5. Проверить, что staging очищен/сохранён по политике.

---

## Критерии готовности

- [ ] staging → final работает.
- [ ] default flat `Author - Title`.
- [ ] template opt-in.
- [ ] коллизия `1/`, sidecars вместе.
- [ ] теги пишет только metadata.
- [ ] `.lrc`/`.elrc`/cover/info по политике.
- [ ] `--no-rename`/`--no-tag`/`--dry-run`.
- [ ] `uv run pytest -q` зелёный.
- [ ] ручной smoke на YouTube/Yandex.

---

## Что не делать

- Не делать AcoustID/MusicBrainz (следующий слой).
- Не скачивать.
- Не добавлять БД/историю.
- Не писать `(1)`.
- Не использовать yt-dlp embed-metadata/thumbnail.

---

## Передача дальше

Следующий чат получает:

- финальные файлы/теги/sidecars;
- naming/collision policy;
- ingest report.

Дальше — [L5_EXTRA_SOURCES.md](./L5_EXTRA_SOURCES.md).
