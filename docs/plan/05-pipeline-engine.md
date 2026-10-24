# Plan 05: Pipeline engine

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 02, 03, 04
- **Разблокирует:** Plan 06, 07
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за execution engine. Работаешь в `trackforge` после домена, CAS и SQLite. Задача — generic planner/executor DAG; конкретные анализаторы и источники здесь не реализуются.

## Цель

Появляется pipeline, который по набору capability-манифестов строит DAG от желаемых outputs, выполняет шаги с кэшем, resume, retry, бюджетом, отменой и видимой деградацией, сохраняя состояние в SQLite и артефакты в CAS. Работает на фейковых capability в тестах.

## Контекст и инварианты

- Шаг — capability с `requires/provides/cost/version/determinism/cacheable` (`ADR-0004`).
- Cache key = SHA-256 канонического JSON входов + capability/version + params + relevant config + model/prompt (`03-pipeline.md` §5.1).
- Состояния: Run `planned → running → paused → completed | failed | cancelled`; Step `pending → ready → running → succeeded | failed | skipped | cached` (`03-pipeline.md` §6.1).
- Required output только один — успешно созданный файл; title/artist desired; остальное optional (`01-domain-model.md` §9.6).
- Ошибки не кэшируются; negative cache не используется.
- События не источник правды; состояние — SQLite/артефакты.
- CPU-bound шаги — в process pool; executor асинхронный, но не знает конкретных моделей.
- `--force` игнорирует кэш выбранных шагов, но не удаляет артефакты.

## Прочитать перед началом

- `docs/design/03-pipeline.md` — полностью.
- `docs/design/04-storage.md` — §4–6.
- `docs/design/02-architecture.md` — §5, §10–11.
- `docs/design/01-domain-model.md` — §6.5, §9.
- `docs/design/adr/0004-capability-dag-pipeline.md`.
- `src/trackforge/domain/model/capabilities.py`, `outputs.py`, `claims.py`.
- `src/trackforge/adapters/persistence/sqlite/`, `src/trackforge/application/ports/storage.py`.
- `docs/plan/03-cas-artifact-store.md`, `docs/plan/04-sqlite-catalog.md` (handoff).

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/domain tests/storage -q
uv run lint-imports
```

Зелёные. `ArtifactStore` и репозитории run/step/artifact доступны.
## Scope

- `pipeline/dag.py`: `Node`, `Edge`, `Graph`, topo sort, cycle detection.
- `pipeline/planner.py`: desired outputs → provider chains → DAG; budget; blocked.
- `pipeline/cache.py`: cache key, lookup, schema compatibility, force set.
- `pipeline/budget.py`: CPU/network/cost/memory budget; estimate/actual.
- `pipeline/degradation.py`: required/desired/optional mapping, warning codes.
- `pipeline/jobs.py`: run/step state machine, resume.
- `pipeline/executor.py`: ready queue, retries, cancellation, fallback chains, events.
- `pipeline/events.py`: in-process EventBus.
- `application/ports/execution.py`: `Capability`, `StepContext`, `StepResult`.
- `pipeline/__init__.py` — только реэкспорты.
- Тесты на фейковых capability.
## Non-goals

- Конкретные `ffprobe`, `yt-dlp`, `MusicBrainz`, Whisper, Demucs, LLM.
- CLI `status/resume` (Plan 09), HTTP/SSE (Plan 19).
- Модели/ModelManager (Plan 14+).
- Параллельная обработка нескольких item (Plan 18).
- Negative cache, distributed queue, Celery/Redis.
## Deliverables

```text
src/trackforge/application/ports/execution.py
src/trackforge/pipeline/__init__.py
src/trackforge/pipeline/dag.py
src/trackforge/pipeline/planner.py
src/trackforge/pipeline/cache.py
src/trackforge/pipeline/budget.py
src/trackforge/pipeline/degradation.py
src/trackforge/pipeline/jobs.py
src/trackforge/pipeline/events.py
src/trackforge/pipeline/executor.py
tests/pipeline/{conftest,test_dag,test_planner,test_cache,test_budget,test_executor,test_resume,test_degradation,test_events}.py
```
## Контракты

### Capability

```text
Capability:
  manifest: CapabilityManifest
  async run(ctx: StepContext) -> StepResult

StepContext: run_id, item_id, step_id, inputs[ArtifactType, ArtifactRef],
  params, config_subset, budget, force, cancelled: asyncio.Event
StepResult: artifacts[ArtifactDraft], warnings[], actual_cost, status
  (succeeded | degraded | skipped)
```

- Capability не пишет в item/DB/CLI; только возвращает `StepResult`.
- Capability не выбирает финальные теги.
- Capability не вызывает другие capability напрямую.

### Plan / planner

```text
PlanNode(node_id, capability_id/version, requires[], provides[], fallback_group?, optional)
Plan(nodes[], edges[], desired_outputs[], required_outputs[], estimated_cost,
     warnings[], blocked_reason?)
Planner.plan(desired_outputs, profile, available) -> Plan
```

- `desired_outputs` — `ArtifactType`/field; `profile.enabled/disabled` учитываются.
- Provider chain: несколько capability с одинаковым `provides`; planner добавляет fallback edges в порядке chain.
- Цикл → `PipelinePlanError`; missing required provider → `Plan.blocked_reason`; missing optional → warning.
- Бюджет: optional сверх бюджета → `skipped` warning; required сверх бюджета → blocked.
- План не выполняет I/O; сохранение плана — DTO/БД (Plan 04 runs/steps).

### Cache

```text
cache_key_for(inputs, capability, params, relevant_config, model?, prompt?) -> str
Cache.lookup(key, schema_version) -> ArtifactRef | None
Cache.store(key, StepResult) -> list[ArtifactRef]
```

- `relevant_config` — только ключи, объявленные capability; не весь конфиг.
- Schema mismatch → cache miss.
- `force_steps`/`force_all` обходят lookup, но не удаляют старые артефакты.
- TTL: `expires_at` в manifest; `--refresh` игнорирует TTL. В Plan 05 TTL-проверка есть, refresh приходит из Plan 09/12.
- Ошибки не кэшируются.

### Executor

- Топологический порядок + ready queue; `asyncio.Semaphore` для I/O; CPU — `ProcessPoolExecutor` в адаптерах.
- До шага: `running`, `attempts++`, `step.started`; после — artifact/DB/`step.finished|cached|failed`.
- Retry: только `retryable`, max 3, backoff+jitter; rate limit — порт, не executor.
- Fallback: `degraded`/`skipped`/retryable → следующий provider в chain.
- Cancellation: cooperative `asyncio.Event` между шагами; состояние сохраняется.
- Required failure → run `failed`; optional → warning + `skipped/degraded`.

### Resume

- `resume(run_id)`: найти `run.status in {running, paused}`, шаги `running/pending/ready`; проверить артефакты; валидные → `cached`/`succeeded`, отсутствующие → `pending`.
- Повторный запуск завершённого run → `RunAlreadyCompleted` (или новый run по явному запросу).
- `Run`/`Step` state machine не допускает нелегальных переходов.

### Events

```text
run.started, step.started/progress/cached/finished/failed,
run.paused/finished/failed/cancelled
```

- `EventBus.publish(event)`, `async for event in events(run_id)`.
- Поля: `run_id`, `item_id`, `step_id`, `capability`, `provider`, `duration_ms`, `status`, `warning_codes`.
- Секреты/куки не попадают в события.
## Порядок работ

1. Реализуй `dag.py` с cycle detection и topo sort.
2. Реализуй planner с provider chains/бюджетом/warnings.
3. Реализуй cache key и lookup/store.
4. Реализуй state machine `jobs.py` поверх `RunRepository`.
5. Реализуй executor с retry/fallback/cancellation/events.
6. Реализуй resume и тесты падения между шагами.
7. Добавь fake capability и 3–5-шаговый DAG в тестах.
8. Прогони quality gates; закоммить.
## Тесты

- **unit:** DAG cycle/missing provider/topo; planner fallback/budget/disabled; cache key stability/relevant config/schema mismatch/force; budget exceeded; state transitions; warning codes.
- **contract:** fake `Capability` соответствует протоколу; `ArtifactStore` fake; `RunRepository` fake.
- **integration:** 3-step fake DAG с реальным `FileSystemArtifactStore` и SQLite в `tmp_path`; cache hit; crash/resume; cancellation; fallback на второй provider.
- **fixtures:** `FakeCapability` с управляемым success/fail/degraded/delay; `tmp_path` data dir; `make_plan`.
- **negative:** цикл; missing required provider; non-retryable error; invalid artifact manifest; duplicate step id; resume неизвестного run; cancellation во время шага.
## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/pipeline -q
uv run lint-imports
uv run pytest tests/pipeline/test_resume.py -q
```

Ожидается: фейковый DAG выполняется; второй запуск даёт cache hits; resume после искусственного падения не повторяет успешные шаги; optional failure даёт `degraded`, required — `failed`.
## Acceptance criteria

- [ ] `uv run pytest tests/pipeline -q` зелёный.
- [ ] Planner строит DAG из `requires/provides`, detects цикл и missing provider.
- [ ] Provider chain fallback работает: первый падает retryable, второй succeeds.
- [ ] Cache hit не вызывает capability повторно; `--force` вызывает; schema mismatch — miss.
- [ ] Resume после падения между шагами не выполняет успешные шаги повторно.
- [ ] Optional failure → `degraded`/`skipped` + warning; required failure → run `failed`.
- [ ] Cancellation сохраняет состояние и не оставляет step в `running`.
- [ ] Event sequence содержит `run.started`, `step.*`, `run.finished`.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] В `pipeline` нет импортов конкретных адаптеров; `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- `Capability`/`StepContext`/`StepResult`.
- `Planner`/`Plan`/`Graph`.
- `Executor` с retry/fallback/cancellation/events.
- `Cache`/`Budget`/`Degradation`/`Resume`.
- Fake capabilities и pipeline-тесты.

## Stop conditions / decision gates

- Нужен конкретный анализатор/источник — это Plan 06/11/12, не расширяй.
- Требуется Celery/Redis/Prefect — стоп: это противоречит `02-architecture.md` §2.2.
- Требуется negative cache — Deferred, не в v1.
- Если cache key невозможно стабилизировать (недетерминированный JSON) — стоп и опиши, какой вход не сериализуется.
