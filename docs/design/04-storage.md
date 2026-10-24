# 04. Storage

> Статус: **черновик v0.3**. Фиксирует, где и как хранятся байты, артефакты, каталог, решения и feedback.
> Цель — чтобы resume, дедупликация, аудит и переносимость не зависели от «памяти процесса».

## 1. Принципы

1. **Байты неизменяемы.** CAS хранит содержимое по SHA-256; ничего не перезаписывается.
2. **Артефакты — тоже контент.** Наблюдения, claims, decisions, bundles имеют манифест и хэш.
3. **Каталог — индекс, а не единственный источник правды.** Артефакты и sidecar можно перечитать.
4. **Item workspace отделён от общего кэша.** Одинаковые байты могут быть разными item'ами с разным источником/решениями.
5. **Append-only там, где важна история.** Claims, decisions, feedback не перезаписываются; новые версии добавляются.
6. **Атомарность.** Temp + fsync + rename; read-back для тегов.
7. **Секреты — вне БД и вне артефактов.**
8. **Пути — только внутри разрешённых root'ов.**

## 1.1. Зачем нужна БД

Коротко: **SQLite — это операционный индекс и журнал состояния, а не хранилище музыки, обложек или артефактов.**

Она нужна там, где файлы/JSON начинают требовать «своей маленькой базы»:

| Задача | Без БД | С SQLite |
|---|---|---|
| Resume после падения | сканировать все item/step JSON, вручную определять, что уже сделано | `runs`/`steps`/`artifacts` + индексы: найти незавершённые за один запрос |
| Locks и overrides | отдельные файлы, ручная синхронизация, риск потерять при рестарте | `locks` с уникальностью `(item_id, field)` |
| Feedback/corrections | JSONL только для экспорта; статистика — скриптами | `feedback_events` + индексы по item/field/времени |
| «Покажи всё, что нужно review» | обойти тысячи JSON-файлов | SQL-запрос по `items.status`/`decisions` |
| Cache hit | искать файл по `cache_key` вручную | `artifacts.cache_key UNIQUE` |
| Атомарность acquire/publish/decision | несколько JSON-файлов могут разъехаться при падении | транзакция + WAL |
| Конкурентные CLI-процессы | самодельный file lock на каждый файл | WAL + file lock на data dir |
| Библиотека/дубликаты | сканировать все sidecar | `library_entries`, `media_objects.content_hash` |
| Миграции схемы | версионировать JSON вручную | Alembic |

### Что БД **не** хранит

- аудио-байты — они в CAS;
- обложки/крупные payload'ы — в `artifacts/` (в БД только путь/хэш);
- секреты — в `.env`/keyring;
- полный текст лирики/транскрипта — в артефактах; в БД — ссылки/поля для поиска.

### Почему SQLite, а не «просто файлы»

- Встроенная, без сервера и отдельного процесса.
- ACID-транзакции, WAL, `busy_timeout`; чтение не блокирует запись.
- SQL-запросы и индексы вместо ручного обхода JSON.
- Один файл `catalog.sqlite`, легко бэкапить/переносить.
- Для 1k–10k треков — с большим запасом; 100k тоже возможны, если индексы и scan продуманы.

### Границы

- БД **восстановима**: CAS + артефакты + sidecar + feedback JSONL позволяют пересобрать индекс. Поэтому она не «единственный источник правды».
- Если БД потеряна, музыка и выходные файлы не теряются; теряются только быстрые индексы/история, которые можно частично восстановить.
- Если хочется «совсем без БД», это возможно, но придётся писать свой слой состояния/локов/запросов — больше кода и больше багов. Для текущего дизайна SQLite — самый дешёвый способ получить надёжный resume и locks.

## 2. Директории

Используем XDG через `platformdirs` (Linux-first), с env-override для переносимого режима.

```text
~/.config/trackforge/                 # config_dir
  config.toml
  .env                                # секреты, 0600
  cookies/
    youtube.txt                       # 0600
    soundcloud.txt

~/.local/share/trackforge/            # data_dir
  catalog.sqlite
  cas/
    ab/cd/<sha256>
  artifacts/
    <cache_key>/
      manifest.json
      payload.json                    # или payload.bin
  items/
    <item_id>/
      item.json
      intake.json
      claims.json
      decisions/
        <record_id>.json
      renders/
        <render_id>/
          track.m4a
          cover.jpg                 # optional; по умолчанию embed
          lyrics.lrc                # optional; LRC sidecar
          trackforge.json
  feedback/
    events.jsonl
    corrections/
      <correction_id>.json

~/Music/TrackForge/                   # library_dir (default, настраивается)
  <artist> - <title> (<qualifier>).<ext>
  <collection-title>/
    <artist> - <title> (<qualifier>).<ext>
  ...

~/.cache/trackforge/                  # cache_dir
  models/
  http/
  tmp/

~/.local/state/trackforge/            # state_dir
  logs/
  runs/
```

`library_dir` по умолчанию — `~/Music/TrackForge` (или XDG music dir + `TrackForge`), а не подпапка `data_dir`. Это user-visible библиотека; `data_dir` — внутренние CAS/артефакты/каталог. Путь можно изменить в `config.toml`.

Env-override:

- `TRACKFORGE_CONFIG_DIR`
- `TRACKFORGE_DATA_DIR`
- `TRACKFORGE_CACHE_DIR`
- `TRACKFORGE_STATE_DIR`
- `TRACKFORGE_LIBRARY_DIR`

Это нужно для тестов, портативного режима и будущего GUI.

## 3. CAS (content-addressed store)

### 3.1. Путь

```text
cas/<first2>/<next2>/<sha256>
```

Пример: `cas/ab/cd/abcdef...`.

### 3.2. Запись

1. Писать во временный файл в `cache_dir/tmp`.
2. Считать SHA-256 потоком (без загрузки всего файла в память).
3. Проверить ожидаемый размер/хэш.
4. `fsync` файла.
5. Атомарно `rename` в CAS (если файла ещё нет; если есть — проверить хэш).
6. `fsync` директории (по возможности).

### 3.3. Правила

- Один и тот же хэш = один объект, независимо от источника.
- Оригинальные байты не изменяются.
- Права: `0644` для CAS (данные), `0700` для директорий.
- Большие файлы не читаются целиком.
- Осиротевшие объекты допускаются; GC — позже.

### 3.4. Ссылки

- `media_objects.content_hash` → CAS.
- `file_instances` могут ссылаться на один `media_object` из разных мест.
- `intakes` хранят происхождение; один media object может иметь несколько intakes.
- Удаление item не удаляет CAS сразу; GC анализирует ссылки.

## 4. Артефакты

### 4.1. Layout

```text
artifacts/<cache_key>/
  manifest.json
  payload.json          # для JSON-артефактов
  payload.bin           # для бинарных (если нужно)
```

`cache_key` — SHA-256 от канонического JSON входов/версий/параметров (см. `03-pipeline.md`).

### 4.2. Манифест

```json
{
  "artifact_id": "01J...",
  "type": "audio.probe.v1",
  "schema_version": 1,
  "producer": {"id": "ffprobe", "version": "6.1"},
  "inputs": [{"type": "media", "hash": "..."}],
  "params": {},
  "params_hash": "...",
  "payload_hash": "...",
  "warnings": [],
  "created_at": "2026-10-22T13:00:00Z",
  "expires_at": null,
  "license": null
}
```

### 4.3. Схемы

- JSON Schema или pydantic-модель на тип; версия в `schema_version`.
- Совместимые изменения (добавление optional полей) — та же версия; ломающие — новая.
- Валидация при записи и чтении.
- Canonical JSON: сортировка ключей, UTF-8, без `NaN`/`Infinity`, стабильные числа.

### 4.4. Общие поля

Все артефакты имеют:

- `schema_version`;
- `producer` (id/version);
- `inputs`;
- `params` (нормализованные параметры);
- `warnings`;
- `created_at`;
- `payload_hash`.

Это позволяет строить граф provenance без доверия к файловой структуре.

## 5. Item workspace

```text
items/<item_id>/
  item.json            # снимок Item + ссылки
  intake.json          # основной Intake + source snapshot ref
  claims.json          # снимок claims на момент последнего resolve
  decisions/
    <record_id>.json   # DecisionRecord + decisions
  renders/
    <render_id>/
      ...
```

Зачем дублировать БД:

- portability/debug;
- возможность восстановить контекст без SQLite;
- sidecar/export.

`item_id` — ULID/UUIDv7 (sortable, opaque). Не content hash: одинаковые байты из разных источников могут иметь разные item'ы и решения.

## 6. SQLite-каталог

### 6.1. Общие настройки

- WAL mode, `busy_timeout`, foreign keys ON.
- Один writer; читатели параллельны.
- Миграции — Alembic.
- JSON-поля — TEXT + pydantic-валидация (или SQLite JSON1 для запросов).
- Секреты не храним.

### 6.2. Таблицы (концептуально)

```text
schema_migrations
  version, applied_at

items
  id PK
  media_id FK
  primary_intake_id FK?
  collection_id FK?
  kind
  status
  identity_artifact_id FK?
  current_decision_record_id FK?
  created_at, updated_at

media_objects
  id PK
  content_hash UNIQUE
  size_bytes
  cas_path
  container, codec, bitrate, sample_rate, channels, duration
  probe_artifact_id FK?
  created_at

file_instances
  id PK
  media_id FK
  path
  role
  size_bytes, mtime
  intake_id FK?
  created_at

intakes
  id PK
  item_id FK
  source_ref_json
  candidate_json
  source_snapshot_artifact_id FK?
  downloader_id, downloader_version
  media_id FK
  status
  created_at

collections
  id PK
  kind
  source_ref_json
  title, owner, description
  context_claims_json
  created_at

collection_items
  collection_id FK
  position
  candidate_json
  item_id FK?
  status
  PRIMARY KEY (collection_id, position)

artifacts
  id PK
  type
  schema_version
  producer_id, producer_version
  cache_key UNIQUE
  payload_path
  payload_hash
  inputs_json
  params_json
  params_hash
  warnings_json
  license?
  created_at
  expires_at

claims
  id PK
  item_id FK
  subject_type
  subject_id
  field
  value_json
  role
  order_index
  locale
  source
  method_id, method_version
  confidence
  evidence_json
  derived_from_json
  status                  # текущая проекция; история — в claim_events
  notes
  created_at

claim_events
  id PK
  claim_id FK
  status                  # accepted | rejected | superseded
  reason
  created_at

decision_records
  id PK
  item_id FK
  version
  policy_versions_json
  llm_model
  prompt_version
  input_claim_ids_json
  created_at

decisions
  id PK
  record_id FK
  subject_type
  subject_id
  field
  value_json
  chosen_claim_id FK?
  alternatives_json
  derived_from_json
  policy_id
  mode
  decided_by
  confidence
  rationale
  locked
  created_at

locks
  id PK
  item_id FK
  field TEXT NOT NULL DEFAULT ''   # '' = item-level lock
  value_json
  source_claim_id FK?
  created_at, updated_at
  UNIQUE(item_id, field)

feedback_events
  id PK
  kind
  item_id FK
  field
  system_decision_id FK?
  system_value_json
  user_claim_id FK?
  user_value_json
  scope
  reason_code
  reason_text
  review_session_id FK?
  context_snapshot_json
  created_at
  applied

review_sessions
  id PK
  started_at, finished_at
  fields_json
  notes

review_session_items
  session_id FK
  item_id FK
  PRIMARY KEY (session_id, item_id)

runs
  id PK
  profile, mode
  status
  started_at, finished_at
  error_json
  config_snapshot_hash

run_items
  run_id FK
  item_id FK
  status
  PRIMARY KEY (run_id, item_id)

steps
  id PK
  run_id FK
  item_id FK
  capability_id, capability_version
  cache_key
  status
  attempts
  started_at, finished_at
  error_json
  warnings_json

renders
  id PK
  item_id FK
  decision_record_id FK
  target_format
  naming_template
  tag_mapping_version
  options_json
  status
  created_at

output_bundles
  id PK
  item_id FK
  render_id FK
  bundle_hash
  files_json
  created_at

library_entries
  id PK
  item_id FK
  bundle_id FK
  path
  status
  published_at

model_registry
  id PK
  provider
  model
  version
  path
  checksum
  license
  size_bytes
  downloaded_at
```

### 6.2.1. Identity-сущности в v1

`Recording`, `Release`, `ReleaseGroup`, `Artist`, `Work`, `Derivative`, `IdentityHypothesis` есть в домене, но в v1 **не нормализуются в отдельные таблицы**. Они живут в:

- `identity.hypotheses.v1` artifact;
- claims (`subject_type`/`subject_id` + field);
- sidecar/decision record.

Почему: MVP — один трек; нормализованные таблицы нужны для duplicate/library UX и join'ов, но их можно добавить позже без изменения публичного API. Это осознанное упрощение, а не забытая сущность.

### 6.2.2. Семантика таблиц

- `claims` — append-only; `status` — текущая проекция, история в `claim_events`.
- `locks` — source of truth для пользовательских фиксаций; `decisions.locked` — снимок на момент record. Locks — только item/field; source/artist/profile/global overrides — это `PreferenceRule` (future), не locks.
- `decision_records`/`decisions` — append-only; новый resolve = новый record.
- `renders` — операция рендера; `output_bundles` — результат; `library_entries` — публикация.
- `feedback_events` — append-only; `system_decision_id`/`user_claim_id`/`review_session_id` связывают контекст.
- `review_sessions.fields_json` — какие поля пользователь смотрел.
- `preference_rules` — будущее; в v1 нет таблицы и автообучения.

### 6.3. Индексы

- `media_objects(content_hash)` unique;
- `artifacts(cache_key)` unique;
- `claims(item_id, field)`;
- `claim_events(claim_id, created_at)`;
- `decisions(record_id, field)`;
- `locks(item_id, field)` unique;
- `feedback_events(item_id, field, created_at)`;
- `feedback_events(review_session_id)`;
- `runs(status)`;
- `steps(run_id, status)`;
- `renders(item_id, status)`;
- `library_entries(path)`;
- `collection_items(collection_id, position)`.

### 6.4. Транзакции

- **Acquire:** CAS-файл → DB-запись. Если DB упала, CAS-объект остаётся сиротой; GC позже.
- **Observe:** artifact-файл → DB-запись. Если DB упала, артефакт можно найти/проверить.
- **Resolve:** новый `decision_record` + decisions; старые не удаляются.
- **Publish:** файл в library → DB-запись. Если DB упала, `library` можно просканировать.
- Транзакции короткие; тяжёлые I/O — вне транзакции.

### 6.5. Восстановление

- `trackforge doctor --repair` (позже): проверить CAS/артефакты/БД, найти сирот.
- `trackforge library scan`: найти файлы без `library_entries` и наоборот.
- `trackforge gc`: удалить unreferenced CAS/artifacts (dry-run по умолчанию).

## 7. Sidecar

`trackforge.json` рядом с выходным файлом:

```json
{
  "schema_version": 1,
  "trackforge": {
    "item_id": "01J...",
    "content_hash": "...",
    "decision_record_id": "01J...",
    "bundle_hash": "...",
    "created_at": "2026-10-22T13:00:00Z"
  },
  "source": {
    "url": "...",
    "extractor": "youtube",
    "source_id": "..."
  },
  "metadata": {
    "title": "...",
    "artist": ["..."],
    "album": "...",
    "date": "...",
    "genre": ["..."],
    "bpm": 172,
    "key": "F#m",
    "language": "en"
  },
  "identity": {
    "kind": "music",
    "derivative": ["nightcore"],
    "recording_mbid": "...",
    "release_mbid": "..."
  },
  "artifacts": {
    "probe": "artifact-id",
    "features": "artifact-id",
    "lyrics": "artifact-id"
  },
  "warnings": [],
  "unknowns": ["label", "catalog_number"]
}
```

Правила:

- Не содержит секретов/куки/токенов.
- Может быть большим; optional pretty-print.
- Используется для `import --sidecar`, диагностики и переносимости.
- Не является заменой БД.

## 8. Managed library

### 8.1. Layout

По умолчанию — **flat для одиночного трека** и **подпапка коллекции** для плейлиста/альбома:

```text
library_dir/                          # default: ~/Music/TrackForge
  <artist> - <title> (<qualifier>).<ext>              # одиночный трек
  <collection-title>/
    <artist> - <title> (<qualifier>).<ext>            # плейлист/альбом
  Unknown Artist - <title> [<short-id>].<ext>         # если артист неизвестен
```

Правила:

- `qualifier` опционален: `nightcore`, `remix`, `sped up`, ...; если title уже содержит его, не дублируем.
- `artist` — основной артист; для нескольких — первый/`Various Artists` (детали в `08-metadata.md`).
- `collection-title` — название плейлиста/альбома; если пусто — `Collection [<short-id>]`.
- Для локальной папки: если пользователь импортирует папку как коллекцию — подпапка; если отдельные файлы — flat.
- Шаблон настраивается: `{artist}`, `{title}`, `{qualifier}`, `{collection}`, `{year}`, `{source_id}`, `{short_id}`, опционально `{track}`, `{disc}`.

### 8.2. Правила

- Только внутри `library_root`.
- Санитизация: запрещённые символы, длина, reserved names (Windows — позже), `.`/`..`.
- Коллизия: ` (1)`, ` (2)` или короткий id; никогда не перезаписывать чужой файл.
- Атомарная публикация: temp в том же root → rename.
- Sidecar рядом.
- `library_entries` хранит путь и bundle hash; при сканировании проверяем hash/наличие.

### 8.3. Режимы

- `copy` (default) — копия из bundle.
- `move` — перемещение (только если bundle в temp).
- `hardlink` — позже.
- `in_place` — позже, явная опция; с backup.

## 9. Feedback storage

- `feedback/events.jsonl` — append-only, одна строка = событие; удобно для экспорта/анализа.
- `feedback/corrections/<id>.json` — полный снимок контекста (claims, alternatives, versions).
- `feedback_events` в SQLite — для запросов/статистики.
- Дублирование JSONL + DB допустимо: JSONL — источник для экспорта, DB — индекс.
- Секреты/аудио не храним; только метаданные/цитаты.
- Удаление feedback — только явное, с предупреждением; по умолчанию append-only.

## 10. Модели и кэш

```text
cache/models/<provider>/<model>/<version>/
  manifest.json
  weights...
  LICENSE
```

- `model_registry` в SQLite: checksum, license, size.
- Скачивание — по явной команде/первому использованию; прогресс; проверка checksum.
- `TRACKFORGE_OFFLINE=1` запрещает скачивание/сеть.
- HTTP-кэш (`hishel`) — в `cache/http`; TTL/refresh.
- Temp — `cache/tmp`; очистка при старте/по возрасту.

## 11. Права и безопасность

- `config_dir` — `0700`; `.env`/cookies — `0600`.
- `data_dir` — `0700`; `catalog.sqlite` — `0600`.
- `library` — по умолчанию `0755` (пользователь может изменить).
- Не логировать содержимое `.env`/cookies.
- SQL — только параметризованные запросы.
- Пути — нормализация + проверка, что итог внутри root.
- Симлинки: не следовать за пределы root без явного разрешения.

## 12. Backup / export / retention

- `trackforge export` — JSONL/JSON каталога, decisions, feedback (без CAS).
- `trackforge backup` — каталог + config (без моделей); CAS — опционально.
- Sidecar + CAS позволяют восстановить output.
- Retention: по умолчанию не удаляем; `gc` — позже, с dry-run.
- Debug-артефакты — TTL/ручная очистка.

## 13. Пример записи

1. URL → `items/01J.../intake.json`, CAS `ab/cd/...`.
2. `ffprobe` → `artifacts/<key>/payload.json`, `artifacts` row.
3. AcoustID → `artifacts/<key>/payload.json`, `acoustid.lookup.v1`.
4. Claims → `claims` rows + `items/.../claims.json`.
5. DecisionRecord → `decision_records` + `decisions` + `items/.../decisions/<id>.json`.
6. Render → `renders` row + `items/.../renders/<id>/...` + `output_bundles`.
7. Publish → `library_dir/...` + `library_entries`.
8. Correction → `feedback_events` + `feedback/corrections/<id>.json`.

## 14. Открытые вопросы

- `item_id`: ULID vs UUIDv7. Предложение: UUIDv7 (sortable, стандарт).
- Глобальный `artifacts/` vs артефакты внутри item. Предложение: глобальный кэш + ссылки; item snapshots для portability.
- SQLAlchemy vs чистый `sqlite3`. Предложение: SQLAlchemy 2 + Alembic.
- `library layout`: решено — flat для одиночных треков, подпапка коллекции для плейлистов/альбомов; `library_dir` по умолчанию `~/Music/TrackForge`.
- Нужен ли `hardlink` режим. Позже.
- Политика GC и retention. Позже, с dry-run.
- Шифрование cookies/keyring. Позже; сейчас 0600.
- Мультибиблиотечность. Позже.
