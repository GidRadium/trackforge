# 09. Interfaces

> Статус: **черновик v0.3**. Python API, CLI, будущий GUI/HTTP и plugin API.
> Принцип: вся логика — в application services; интерфейсы — тонкие клиенты.

## 1. Принципы

1. **API-first.** Python API — первичный контракт; CLI и GUI не содержат бизнес-логики.
2. **Один сценарий — один use case.** Интерфейс вызывает сервис, а не собирает пайплайн вручную.
3. **DTO, а не доменные объекты.** Пользователь не может случайно сломать состояние.
4. **Ошибки типизированы.** `retryable`, `user_action`, `context`.
5. **Прогресс и отмена — часть API**, а не только CLI.
6. **Dry-run и explain — первоклассные операции.**
7. **Версионирование.** Публичный API — semver; внутренние модули — без обещаний.

## 2. Python API

### 2.1. Фасады

- `AsyncTrackForge` — async-first, для CLI/встраивания/будущего HTTP.
- `TrackForge` — sync-обёртка для простых скриптов; не работает внутри активного event loop (явная ошибка).

Оба используют один composition root и одни application services.

### 2.2. Конфигурация

```python
from trackforge import TrackForgeConfig

config = TrackForgeConfig.from_file("config.toml")
# или
config = TrackForgeConfig(
    data_dir="...",
    library_dir="...",
    profile="balanced",
    mode="policy",
)
```

- Секреты — из env/keyring, не из аргументов.
- Валидация — pydantic; ошибки с ключом.
- Профили/режимы можно переопределять на уровне вызова.

### 2.3. Основные методы

| Метод | Что делает | Возвращает |
|---|---|---|
| `plan(source, options?)` | Разбирает вход, находит кандидатов, строит DAG | `PlanView` |
| `run(plan_or_source, options?)` | Полный путь: acquire → ... → publish | `RunResult` |
| `add(source, options?)` | Удобный alias для plan+run | `RunResult` |
| `import_path(path, options?)` | Локальный файл/папка | `RunResult` |
| `analyze(item_id, options?)` | observe + interpret | `AnalysisView` |
| `resolve(item_id, options?)` | policy/LLM/ranker → decisions | `DecisionView[]` |
| `render(item_id, options?)` | decisions → output bundle | `BundleView` |
| `publish(item_id, options?)` | bundle → library | `LibraryEntryView` |
| `review(item_id, corrections?)` | diff + правки + locks | `ReviewView` |
| `explain(item_id, field)` | почему такое значение | `ExplanationView` |
| `feedback.export(...)` | JSONL/CSV | path |
| `feedback.stats(...)` | метрики | `FeedbackStats` |
| `doctor()` | проверка окружения | `DoctorReport` |
| `plugins.list()` | список плагинов | `PluginView[]` |
| `models.list()` | модели/статус | `ModelView[]` |

`run` умеет принимать `SourceRef`, `PlanView` или `item_id` (для повторного запуска).

### 2.4. Sync-пример

```python
from trackforge import TrackForge, TrackForgeConfig, RunOptions

config = TrackForgeConfig.from_file("config.toml")

with TrackForge(config) as tf:
    plan = tf.plan("https://www.youtube.com/watch?v=...")
    print(plan.candidates)

    result = tf.run(
        plan,
        RunOptions(profile="balanced", mode="policy", auto=True),
    )
    print(result.status, result.output_paths)
```

### 2.5. Async-пример

```python
from trackforge import AsyncTrackForge, TrackForgeConfig, RunOptions

async with AsyncTrackForge(TrackForgeConfig.from_file("config.toml")) as tf:
    plan = await tf.plan("https://www.youtube.com/watch?v=...")
    result = await tf.run(plan, RunOptions(profile="balanced"))
```

### 2.6. Прогресс и события

```python
async for event in tf.events(run_id):
    if event.kind == "step.progress":
        print(event.step, event.percent)
```

- События in-process; для CLI — `rich`.
- Для будущего GUI — SSE/WebSocket поверх тех же событий.
- События не являются источником правды; состояние — в БД/артефактах.

### 2.7. DTO

```text
PlanView:
  source
  candidates[]
  collection?
  estimated_cost
  steps[]
  warnings[]
  blocked_reason?

RunResult:
  run_id
  status
  items[]
  warnings[]
  errors[]
  output_paths[]

ItemView:
  id
  status
  identity_summary
  decisions_summary
  output_path
  warnings[]

DecisionView:
  field
  value
  alternatives[]
  confidence
  rationale
  mode
  decided_by
  locked

ExplanationView:
  field
  decision
  policy
  chosen_claim
  alternatives[]
  rationale

CorrectionRequest:
  field
  value
  scope          # item | source | artist | profile | global
  lock           # bool
  reason_code?
  reason_text?
```

DTO read-only (frozen). Для изменения — методы/`CorrectionRequest`.

### 2.8. Ошибки

```text
TrackForgeError
├─ ConfigError
├─ UnsupportedSource
├─ CandidateNotFound
├─ AcquisitionFailed
├─ ProviderUnavailable
├─ RateLimited
├─ ResolutionFailed
├─ RenderFailed
├─ PublishFailed
├─ ReviewRequired
└─ PluginError
```

Поля: `message`, `retryable`, `user_action`, `context` (redacted).

### 2.9. Отмена

- `tf.cancel(run_id)` — cooperative.
- `asyncio.CancelledError` — корректно сохраняет состояние.
- CLI: Ctrl+C → pause/cancel с подтверждением.

### 2.10. Стабильность

- Публичные модули: `trackforge.api`, `trackforge.dto`, `trackforge.errors`, `trackforge.plugins`.
- `TrackForge`/`AsyncTrackForge` — стабильные фасады.
- Внутренние `trackforge.domain`/`adapters` — не публичный контракт.
- Semver; deprecation-период; changelog.

## 3. CLI

### 3.1. Команды

```text
trackforge add <url|path>        # plan + run
trackforge plan <source>         # dry-run
trackforge import <path>         # локальный файл/папка
trackforge analyze <item>        # observe + interpret
trackforge resolve <item>        # policy/llm_assisted/llm_autopilot
trackforge render <item>         # output bundle
trackforge publish <item>        # в library
trackforge review <item>         # diff + правки
trackforge explain <item> --field title
trackforge feedback export|stats
trackforge library scan|verify
trackforge config show|validate
trackforge doctor
trackforge plugins list
trackforge models list|download
trackforge resume <run-id>
trackforge cancel <run-id>
trackforge status <run-id>
```

### 3.2. Флаги

- `--profile fast|balanced|full|custom`
- `--mode policy|llm_assisted|llm_autopilot|manual`
- `--auto` / `--review`
- `--dry-run`
- `--json`
- `--offline`
- `--force [step,...]`
- `--refresh`
- `--enable/--disable <capability>`
- `--library-dir`, `--data-dir`
- `--log-level`
- `--no-color`

### 3.3. Exit codes

| Код | Значение |
|---|---|
| 0 | успех |
| 2 | user error (неверный ввод/конфиг) |
| 3 | provider/network |
| 4 | partial/degraded |
| 5 | internal error |
| 130 | прервано пользователем |

### 3.4. Пример

```text
$ trackforge add "https://youtu.be/..." --profile balanced --auto
Plan:
  1. YouTube — "Song (Nightcore)" — 3:12 — m4a 128k / opus 160k
  2. ...
Choose [1]: 1

Acquire  ██████████ 100%
Probe    ██████████ 100%
Observe  ███████░░░  70%  (AcoustID, MusicBrainz, features)
Resolve  ██████████ 100%
Render   ██████████ 100%
Publish  ██████████ 100%

Result: library_dir/Artist - Song (Nightcore).m4a
Warnings: cover low resolution; lyrics not found
```

### 3.5. Review

```text
Field     Current                     Alternatives
title     Song (Nightcore)            Song (Nightcore) [Official Video] (0.60)
artist    Artist                      Artist - Topic (0.40)
album     Album                       Chill Mix (0.55)
cover     CAA front 1200x1200         YouTube thumbnail 1280x720

Actions: [a]ccept [e]dit [l]ock [u]nknown [w]hy [s]kip
```

- `why` — explain.
- `edit` — новое значение + scope + lock.
- `unknown` — оставить пусто.
- `skip` — не менять это поле.

## 4. Будущий GUI / HTTP

### 4.1. Архитектура

- Локальный FastAPI-сервер (опционально), тот же `AsyncTrackForge`.
- Frontend (Svelte/React) — отдельно; SSE/WebSocket для прогресса.
- Никакой бизнес-логики во frontend/HTTP-слое.
- Локальный auth token (генерируется при старте), bind на `127.0.0.1`.
- CORS — только localhost.

### 4.2. Endpoints (эскиз)

```text
POST /api/plan              { source, options } -> PlanView
POST /api/runs              { plan|source, options } -> RunResult
GET  /api/runs/{id}         -> RunView
GET  /api/runs/{id}/events  -> SSE
POST /api/runs/{id}/cancel
GET  /api/items/{id}        -> ItemView
GET  /api/items/{id}/decisions
POST /api/items/{id}/review { corrections }
GET  /api/items/{id}/explain?field=title
GET  /api/library
POST /api/library/scan
GET  /api/doctor
GET  /api/plugins
GET  /api/models
```

### 4.3. UI-экраны

1. **Import** — ссылка/файл/папка, профиль, режим.
2. **Candidates** — выбор качества/формата; для плейлиста — список.
3. **Progress** — шаги, warnings, отмена.
4. **Review** — diff по полям, альтернативы, locks.
5. **Library** — опубликованные элементы, дубликаты, проблемы.
6. **Settings** — профили, провайдеры, cookies, модели.
7. **Feedback** — статистика правок, экспорт.

GUI не обязателен для MVP; API проектируем так, чтобы добавить позже.

## 5. Plugin API

### 5.1. Entry points

```text
trackforge.sources
trackforge.acquirers
trackforge.observers
trackforge.metadata
trackforge.interpreters
trackforge.rankers
trackforge.llm
trackforge.renderers
trackforge.tag_writers
```

### 5.2. Манифест

```python
# псевдокод, не реализация
manifest = PluginManifest(
    id="my_source",
    version="1.0.0",
    contract_version=1,
    kind="source",
    requires=[],
    provides=["source.metadata.v1"],
    cost=...,
    determinism="deterministic",
    cacheable=True,
    config_schema=...,
    license="MIT",
)
```

### 5.3. Контракт

- Плагин реализует протокол порта (например, `SourceResolver`).
- Ядро проверяет `contract_version`; несовместимый плагин не загружается.
- Ошибка плагина не роняет ядро; `doctor`/логи показывают.
- Плагины — trusted Python-код; sandbox в v1 нет.
- Плагин не получает секреты других источников.

### 5.4. Документация

- `docs/plugins/` — позже; примеры source/observer/ranker.
- `trackforge plugins list` показывает id/version/license/status.

## 6. UX-принципы

1. **Dry-run сначала.** Показать план/кандидатов/стоимость до скачивания.
2. **Объяснимость.** Любое поле можно объяснить.
3. **Безопасность.** Оригинал не трогаем; publish не перезаписывает.
4. **Видимая деградация.** Warnings, а не тихие `None`.
5. **Прогресс и отмена.** Долгие шаги показывают статус.
6. **Машинный вывод.** `--json` для скриптов.
7. **Никаких сюрпризов.** Конфиг/пути/профиль видны в `doctor`/`config show`.

## 7. Версионирование

- Публичный API — semver.
- `contract_version` для плагинов.
- Схемы артефактов — `schema_version`.
- Prompt/model versions — в `DecisionRecord`.
- Deprecation: минимум один minor-релиз; предупреждение в логах/доках.

## 8. Открытые вопросы

- Нужен ли HTTP API сразу или только после GUI? Предложение: после MVP.
- Какой frontend (Svelte/React) — позже.
- Нужен ли TUI (Textual) как промежуточный интерфейс? Возможно.
- Формат `--json` (JSON Lines vs один объект) — решить при реализации.
- Как показывать прогресс в неинтерактивном CI — только JSONL.
