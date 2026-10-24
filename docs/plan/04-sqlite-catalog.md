# Plan 04: SQLite-каталог и миграции

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 02
- **Разблокирует:** Plan 05
- **Оценка:** L
- **Риск:** средний
## Роль

Ты — senior Python engineer, отвечающий за операционный каталог. Работаешь в `trackforge` после домена. Задача — SQLite-схема, миграции, репозитории и блокировки; без бизнес-правил и без CAS-логики (она в Plan 03).

## Цель

Появляется каталог `data_dir/catalog.sqlite`: схема MVP, Alembic-миграции, типизированные репозитории и file lock на data dir. Два процесса не портят каталог; миграция с пустой БД идемпотентна; claims/decisions/feedback append-only.

## Контекст и инварианты

- БД — операционный индекс, не единственный источник правды (`04-storage.md` §1.1).
- WAL, `busy_timeout`, foreign keys ON; один writer (`04-storage.md` §6.1).
- `media_objects.content_hash` unique; `artifacts.cache_key` unique; `locks(item_id, field)` unique.
- `claims.status` — проекция; история — `claim_events`; claims append-only.
- `decision_records`/`decisions` append-only; новый resolve — новый record.
- `feedback_events` append-only; широкие scope — future `PreferenceRule`, не таблица v1.
- `Collection` не `Recording`; identity-сущности в v1 — artifact-only, без таблиц (`13-consistency-review.md` §4).
- Секреты/куки в БД не хранятся; SQL — только параметризованный.
- SQLAlchemy 2 sync + Alembic (MIT); async-порты оборачивают sync-адаптер через `asyncio.to_thread` (D3).

## Прочитать перед началом

- `docs/design/04-storage.md` — §1–2, §5–6, §11.
- `docs/design/03-pipeline.md` — §6, §8.
- `docs/design/02-architecture.md` — §3, §5, §11.
- `docs/design/01-domain-model.md` — §3–8, §11.
- `docs/design/13-consistency-review.md` — §3–4.
- `src/trackforge/domain/model/` (Plan 02), `src/trackforge/config/` (Plan 01).
- `docs/plan/02-domain-core.md` (handoff).

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/domain -q
uv run lint-imports
```

Зелёные. `uv` и Python 3.12 доступны; SQLite входит в Python.
## Scope

- `adapters/persistence/sqlite/engine.py`: engine, pragmas, `initialize`, `close`.
- `adapters/persistence/sqlite/models.py`: SQLAlchemy 2 declarative-модели.
- `adapters/persistence/sqlite/migrations/` + `alembic.ini`: initial migration.
- `adapters/persistence/sqlite/repositories/`: репозитории MVP.
- `adapters/persistence/sqlite/unit_of_work.py`.
- `adapters/persistence/sqlite/file_lock.py`.
- `application/ports/repositories.py`: протоколы.
- `application/dto/db.py` при необходимости (pydantic на границе).
- `bootstrap/container.py`: создание каталога/репозиториев.
- Тесты на `tmp_path` и subprocess для file lock.
## Non-goals

- Коллекции/плейлисты (`collections`, `collection_items`) — Plan 18.
- `model_registry` — Plan 14.
- GC/backup/repair/scan — Plan 20.
- Нормализованные identity-таблицы — Deferred.
- Бизнес-логика resolve/render/publish.
## Deliverables

```text
src/trackforge/application/ports/repositories.py
src/trackforge/adapters/persistence/sqlite/__init__.py
src/trackforge/adapters/persistence/sqlite/engine.py
src/trackforge/adapters/persistence/sqlite/models.py
src/trackforge/adapters/persistence/sqlite/unit_of_work.py
src/trackforge/adapters/persistence/sqlite/file_lock.py
src/trackforge/adapters/persistence/sqlite/repositories/{__init__,items,media,intakes,artifacts,claims,decisions,locks,feedback,runs,renders,library}.py
alembic.ini
migrations/env.py
migrations/versions/0001_initial.py
tests/storage/test_migrations.py
tests/storage/test_repositories.py
tests/storage/test_file_lock.py
```
## Контракты

### Схема initial migration

Таблицы и ключевые колонки (полный набор — `04-storage.md` §6.2):

```text
items(id PK, media_id FK, primary_intake_id FK?, collection_id?, kind, status,
      identity_artifact_id FK?, current_decision_record_id FK?, created_at, updated_at)
media_objects(id PK, content_hash UNIQUE, size_bytes, cas_path, container, codec,
              bitrate, sample_rate, channels, duration, probe_artifact_id FK?, created_at)
file_instances(id PK, media_id FK, path, role, size_bytes, mtime, intake_id FK?, created_at)
intakes(id PK, item_id FK, source_ref_json, candidate_json, source_snapshot_artifact_id FK?,
        downloader_id, downloader_version, media_id FK, status, created_at)
artifacts(id PK, type, schema_version, producer_id, producer_version, cache_key UNIQUE,
          payload_path, payload_hash, inputs_json, params_json, params_hash, warnings_json,
          license, created_at, expires_at)
claims(id PK, item_id FK, subject_type, subject_id, field, value_json, role, order_index,
       locale, source, method_id, method_version, confidence, evidence_json,
       derived_from_json, status, notes, created_at)
claim_events(id PK, claim_id FK, status, reason, created_at)
decision_records(id PK, item_id FK, version, policy_versions_json, llm_model, prompt_version,
                 input_claim_ids_json, created_at)
decisions(id PK, record_id FK, subject_type, subject_id, field, value_json, chosen_claim_id FK?,
          alternatives_json, derived_from_json, policy_id, mode, decided_by, confidence,
          rationale, locked, created_at)
locks(id PK, item_id FK, field TEXT NOT NULL DEFAULT '', value_json, source_claim_id FK?,
      created_at, updated_at, UNIQUE(item_id, field))
feedback_events(id PK, kind, item_id FK, field, system_decision_id FK?, system_value_json,
                user_claim_id FK?, user_value_json, scope, reason_code, reason_text,
                review_session_id FK?, context_snapshot_json, created_at, applied)
review_sessions(id PK, started_at, finished_at, fields_json, notes); review_session_items(session_id, item_id, PK)
runs(id PK, profile, mode, status, started_at, finished_at, error_json, config_snapshot_hash)
run_items(run_id FK, item_id FK, status, PRIMARY KEY(run_id, item_id))
steps(id PK, run_id FK, item_id FK, capability_id, capability_version, cache_key, status,
      attempts, started_at, finished_at, error_json, warnings_json)
renders(id PK, item_id FK, decision_record_id FK, target_format, naming_template,
        tag_mapping_version, options_json, status, created_at)
output_bundles(id PK, item_id FK, render_id FK, bundle_hash, files_json, created_at)
library_entries(id PK, item_id FK, bundle_id FK, path, status, published_at)
```

- `created_at`/`updated_at` — ISO 8601 UTC строки (`Z`), лексикографически сортируемые.
- `field=''` в `locks` — item-level lock; `UNIQUE(item_id, field)`.
- `claims`/`decisions`/`feedback_events` не имеют update-методов в репозиториях.
- `review_sessions`/`review_session_items` создаются сразу, чтобы FK был валиден; UI/export — Plan 15.
- Индексы из `04-storage.md` §6.3; `alembic_version` — единственная служебная таблица миграций.

### Engine / pragmas

- `create_engine("sqlite:///<data_dir>/catalog.sqlite")`; `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.
- `initialize_catalog(data_dir) -> Catalog`: создать data_dir (0700), `alembic upgrade head`; `Catalog.close()`.
- Никаких глобальных engine; engine инжектится.

### Репозитории

```text
ItemRepository: get, save, update_status, find_by_media, list_by_status
MediaRepository: get, find_by_hash, save
IntakeRepository: get, find_by_item, save
ArtifactRepository: get, find_by_cache_key, save
ClaimRepository: append, list_by_item, list_by_field, append_status_event
DecisionRepository: save_record, get_record, list_records, list_decisions
LockRepository: get, list_by_item, upsert, delete
FeedbackRepository: append_event, list_by_item, list_by_field, stats
RunRepository: create_run, get_run, update_run, add_item, set_item_status,
               create_step, update_step, list_steps, find_resumable
RenderRepository: save_render, save_bundle, get_bundle, list_by_item
LibraryRepository: save_entry, find_by_path, list_entries, update_status
```

- Все методы async; внутри — sync SQLAlchemy через `asyncio.to_thread` (или один выделенный thread executor).
- Возвращают доменные объекты/DTO, не ORM-модели.
- `UnitOfWork`: `async with uow:`; commit/rollback; короткие транзакции; тяжёлый I/O вне.
- `append` для claims/decisions/feedback; конфликт уникальности → типизированная ошибка.

### File lock

- `FileLock(data_dir/.lock)`: `fcntl.flock(LOCK_EX | LOCK_NB)`; второй процесс → `CatalogLocked` с `user_action`, без бесконечного ожидания.
- Lock удерживается на run/команду и освобождается в `finally`; Linux-only v1.
## Порядок работ

1. Настрой Alembic и `engine.py`; реализуй `initialize()` и pragmas.
2. Создай ORM-модели и initial migration; проверь upgrade с пустой БД и повторный `upgrade head`.
3. Реализуй репозитории и UoW; добавь маппинг domain ↔ row.
4. Реализуй file lock и subprocess-тест.
5. Подключи в bootstrap; при старте CLI/каталога — `initialize()`.
6. Добавь тесты миграций/репозиториев/lock; прогони quality gates.
7. Закоммить.
## Тесты

- **unit:** row↔domain mapping; ISO-время; JSON-поля; confidence; `locks` unique; repository methods.
- **contract:** каждый репозиторий соответствует протоколу; fake UoW; domain не импортирует SQLAlchemy.
- **integration:** migration empty→head; re-run; WAL/FK pragmas; round-trip item/media/intake/claim/decision/lock/feedback/run/step/render/library; transaction rollback; concurrent readers; subprocess file lock.
- **fixtures:** `tmp_path` DB; `make_item`/`make_claim` factories; subprocess script holding lock.
- **negative:** FK violation; duplicate content_hash; duplicate cache_key; duplicate lock; claim update attempt; second process lock; migration on corrupt DB.
## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/storage -q
uv run lint-imports
DATA_DIR=$(mktemp -d)
TRACKFORGE_DATA_DIR="$DATA_DIR" uv run python -c "from pathlib import Path; from trackforge.adapters.persistence.sqlite.engine import initialize_catalog; c=initialize_catalog(Path('$DATA_DIR')); print(c)"
```

Ожидается: миграция применяется; тесты зелёные; повторный `upgrade` не падает; второй процесс получает `CatalogLocked`.
## Acceptance criteria

- [ ] `uv run pytest tests/storage -q` зелёный.
- [ ] `alembic upgrade head` на пустом `data_dir` создаёт все таблицы; повторный запуск идемпотентен.
- [ ] WAL, `foreign_keys=ON`, `busy_timeout=5000` подтверждены тестом.
- [ ] `media_objects.content_hash` и `artifacts.cache_key` уникальны; duplicate даёт типизированную ошибку.
- [ ] `locks(item_id, field)` уникален; item-level lock (`field=''`) работает.
- [ ] Claims/decisions/feedback append-only: репозитории не предоставляют update/delete; `claim_events` пишется.
- [ ] Второй процесс с file lock получает понятную ошибку, первый не падает.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён; identity-таблицы не созданы.
## Handoff

Следующие планы могут считать готовыми:

- `catalog.sqlite`, Alembic, pragmas, `initialize_catalog()`.
- Async-репозитории и `UnitOfWork`.
- `FileLock`/`CatalogLocked`.
- Схема MVP и тесты миграций.
- Bootstrap-фабрика каталога.

## Stop conditions / decision gates

- D3 не выбран: используй SQLAlchemy 2 sync + Alembic; не добавляй `aiosqlite`.
- Миграция требует изменить `docs/design/04-storage.md` — стоп; зафиксируй расхождение в plan README.
- Нужны identity-таблицы/collections/model_registry — это другие планы, не расширяй.
- `alembic` несовместим с текущей схемой — стоп, не делай ручной `CREATE TABLE` в обход миграций.
