# Handoff: Plan 04 — SQLite-каталог и миграции

- **Статус:** complete
- **Коммит реализации:** `14ad6f7` (`feat(catalog): add SQLite catalog, migrations, repositories, and file lock`)
- **Alembic revision:** `0001`
- **`docs/design/`:** не изменялся

## Что готово для следующих планов

### Каталог

```python
from pathlib import Path
from trackforge.adapters.persistence.sqlite.engine import Catalog, initialize_catalog

catalog = initialize_catalog(Path(data_dir))   # создаёт data_dir 0700, catalog.sqlite 0600
catalog.engine                                  # SQLAlchemy Engine
catalog.session_factory                         # sessionmaker[Session]
catalog.connect()                               # короткоживущее соединение для диагностики
catalog.close()
```

- `initialize_catalog()` идемпотентен, удерживает `FileLock` на время миграции.
- Pragmas на каждом соединении: `journal_mode=WAL`, `foreign_keys=ON`,
  `busy_timeout=5000`, `synchronous=NORMAL`.
- `alembic upgrade head` из CLI также поддерживается: при
  `TRACKFORGE_DATA_DIR`/`TRACKFORGE_CORE_DATA_DIR` он использует приложенческий
  engine (WAL/pragmas), а не отдельный SQLAlchemy-engine из `alembic.ini`.

### Unit of Work и репозитории

```python
from trackforge.adapters.persistence.sqlite.unit_of_work import (
    SqliteUnitOfWork,
    UnitOfWork,
)

async with SqliteUnitOfWork(catalog) as uow:
    await uow.items.save(item)
    ...
# commit при успешном выходе, rollback при исключении
```

Доступны: `items`, `media`, `file_instances`, `intakes`, `artifacts`, `claims`,
`decisions`, `locks`, `feedback`, `runs`, `renders`, `library`.

- Все методы async; sync SQLAlchemy выполняется через `asyncio.to_thread`.
- `IntegrityError` транслируется в типизированные ошибки:
  `DuplicateContentHash`, `DuplicateCacheKey`, `DuplicateLock`,
  `DuplicateRecord`, `ForeignKeyViolation`.
- После `IntegrityError` сессия автоматически откатывается; UoW можно
  использовать дальше, но предыдущие записи этой транзакции потеряны.
- UoW не предназначен для параллельного использования несколькими tasks:
  один `async with` на use case.
- `SQLite`-репозитории возвращают доменные объекты/DTO, не ORM-модели.

### DTO для сущностей без доменной модели

`trackforge.application.dto.db`:

- `ArtifactRecord` (+ `from_manifest()`/`to_manifest()`);
- `RunRecord`, `RunStatus`;
- `StepRecord`, `StepStatus`, `RunItemRecord`, `RunItemStatus`;
- `LockRecord`, `ClaimEventRecord`, `FeedbackStats`.

`StepRecord.warnings` принимает JSON-объекты и строковые warning-коды.

### File lock

```python
from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.domain.errors import CatalogLocked

with FileLock(data_dir):          # или FileLock(data_dir / ".lock")
    ...                           # второй процесс получает CatalogLocked
```

- `fcntl.flock(LOCK_EX | LOCK_NB)`, без бесконечного ожидания.
- `CatalogLocked.user_action` заполнен; в контексте есть `owner_pid`.
- `initialize_catalog()` и Alembic CLI тоже берут этот lock.

### Bootstrap

```python
from trackforge.bootstrap import (
    build_catalog,
    build_catalog_context,
    build_unit_of_work,
)
```

- `AppContext.open_catalog()` — явное открытие каталога; read-only
  config-команды по-прежнему не создают директории.
- `CatalogContext` — sync/async context manager: lock + `close()`.

## Решения реализации, важные для следующих планов

- `MediaRepository.save()` — insert-only: `MediaObject` иммутабелен, повторный
  insert даёт `DuplicateContentHash`/`DuplicateRecord`.
- `ItemRepository.save()` — upsert по `id`, чтобы агрегат мог менять статус и
  ссылки.
- `RenderRepository.save_render()` — upsert по `id` (жизненный цикл статуса
  рендера); `save_bundle()` — insert.
- `ClaimRepository`, `DecisionRepository`, `FeedbackRepository` не имеют
  `update`/`delete`. `append_status_event()` пишет `claim_events` и обновляет
  проекцию `claims.status`.
- `locks.upsert()` идемпотентен по `(item_id, field)`; `field=''` — item-level.
- `FeedbackEvent.reason` хранится в `reason_text`; `applied` доступен только
  на записи (в доменном `FeedbackEvent` поля нет).

## Известные ограничения / задел

1. `items.identity_artifact_id` есть в схеме, но в `Item` из Plan 02 нет
   соответствующего поля. Plan 07 (identity resolver) потребуется небольшое
   расширение домена или репозитория, чтобы заполнять эту колонку.
2. `review_sessions`/`review_session_items` созданы, но репозитория для них нет —
   это Plan 15.
3. `collections`, `collection_items`, `model_registry` и нормализованные
   identity-таблицы намеренно отсутствуют (Non-goals Plan 04).
4. `FileInstanceRepository` добавлен сверх перечисленных в плане репозиториев,
   потому что `file_instances` создаётся в этой же схеме и нужен Plan 06.

## Проверки на момент handoff

```text
uv run ruff check .                  All checks passed
uv run ruff format --check .         144 files already formatted
uv run mypy src                      Success: 64 source files
uv run pytest tests/storage -q       93 passed
uv run pytest tests -q               274 passed
uv run lint-imports                  6 contracts kept, 0 broken
```

Smoke из плана:

```text
DATA_DIR=$(mktemp -d)
TRACKFORGE_DATA_DIR="$DATA_DIR" uv run python -c \
  "from pathlib import Path; from trackforge.adapters.persistence.sqlite.engine import initialize_catalog; c=initialize_catalog(Path('$DATA_DIR')); print(c); c.close()"
```

Дополнительно проверено: второй процесс с `FileLock` получает `CatalogLocked`;
`alembic upgrade head` с `TRACKFORGE_DATA_DIR` создаёт 20 таблиц, БД в WAL.
