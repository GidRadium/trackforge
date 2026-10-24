# Plan 19: HTTP API (GUI deferred)

- **Статус:** ready
- **Тип:** Optional
- **Зависит от:** Plan 09, Plan 15
- **Разблокирует:** —
- **Оценка:** M
- **Риск:** средний

## Роль

Ты — senior Python engineer, отвечающий за локальный HTTP-интерфейс. Работаешь в `trackforge` после стабильного Python API. Задача — FastAPI/SSE поверх `AsyncTrackForge`; без бизнес-логики и без GUI.

## Цель

Работает `trackforge serve`: локальный FastAPI на `127.0.0.1`, endpoints для plan/run/status/events/cancel/items/decisions/review/explain/library/doctor, SSE-прогресс и локальный auth token. API не дублирует логику; ошибки типизированы; секреты не отдаются. GUI осознанно **Deferred** до v1.1.

## Контекст и инварианты

- HTTP — тонкий клиент над `AsyncTrackForge`/application services (`09-interfaces.md` §4).
- Bind только `127.0.0.1`; локальный auth token генерируется при старте; CORS — только localhost.
- Никакой бизнес-логики, доступа к БД/адаптерам из routes.
- DTO — те же, что Python API; read-only; мутации через use cases.
- Секреты/куки/токены не логируются и не отдаются; ошибки redacted.
- SSE — те же события EventBus; события не источник правды.
- GUI (frontend) — отдельный слой, не в этом плане.
- HTTP API optional: релиз v1.0 не зависит от него.

## Прочитать перед началом

- `docs/design/09-interfaces.md` — §2, §4, §7.
- `docs/design/02-architecture.md` — §2.1, §5, §16.
- `docs/design/10-operations.md` — §8.
- `src/trackforge/api.py` (Plan 09), `src/trackforge/application/dto/`.
- `docs/plan/09-publish-cli-local-mvp.md`, `docs/plan/15-review-feedback.md`.

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/cli tests/integration/test_local_e2e.py -q
uv run lint-imports
uv run python -c "from trackforge.api import AsyncTrackForge; print('api ok')"
```

`AsyncTrackForge`/`TrackForge` должны существовать (Plan 09). Если публичного фасада нет — стоп: Plan 19 не должен создавать второй API.

## Scope

- `[http]` extra: `fastapi`, `uvicorn[standard]`, `sse-starlette`; лицензии MIT/BSD.
- `interfaces/http/app.py`: create_app, lifespan, auth middleware.
- Routes: plan, runs, run status/events/cancel, items, decisions, review, explain, library, doctor, models.
- `trackforge serve --host 127.0.0.1 --port <port>`; token в stdout? Не логировать; файл `state/http-token` 0600.
- SSE `/api/runs/{id}/events`.
- Error mapping DTO → HTTP status; redaction.
- Tests TestClient/httpx ASGI.

## Non-goals

- Frontend/GUI, Svelte/React/PySide6.
- Remote access, auth users/roles, multi-tenant.
- WebSocket (SSE достаточно).
- Загрузка файлов через HTTP (позже).
- Плагины/plugin registry — Deferred до v1.1.
- Library scan/GC endpoints — Deferred; Plan 20 реализует CLI, HTTP не дублирует.
- Изменение домена/БД напрямую.

## Deliverables

```text
src/trackforge/interfaces/http/__init__.py
src/trackforge/interfaces/http/app.py
src/trackforge/interfaces/http/auth.py
src/trackforge/interfaces/http/routes/plan.py
src/trackforge/interfaces/http/routes/runs.py
src/trackforge/interfaces/http/routes/items.py
src/trackforge/interfaces/http/routes/library.py
src/trackforge/interfaces/http/routes/doctor.py
src/trackforge/interfaces/http/routes/meta.py
src/trackforge/interfaces/cli/commands/serve.py
tests/http/test_auth.py
tests/http/test_plan_run.py
tests/http/test_events.py
tests/http/test_review_explain.py
tests/http/test_errors.py
```

## Контракты

### Server

- `create_app(config: TrackForgeConfig) -> FastAPI`.
- `trackforge serve [--host 127.0.0.1] [--port 8765] [--json]`.
- Host default `127.0.0.1`; bind на `0.0.0.0` запрещён без явного `--allow-remote` (не в v1).
- Token: 32+ random bytes, файл `state_dir/http-token` `0600`; заголовок `Authorization: Bearer <token>`; timing-safe compare.
- CORS: только `http://127.0.0.1:<port>`/`http://localhost:<port>`; credentials false.
- Request size limit; timeouts; no directory traversal.

### Endpoints

```text
POST /api/plan              {source, options} -> PlanView
POST /api/runs              {plan|source, options} -> RunResult
GET  /api/runs/{id}         -> RunView
GET  /api/runs/{id}/events  -> SSE
POST /api/runs/{id}/cancel  -> 202
GET  /api/items/{id}        -> ItemView
GET  /api/items/{id}/decisions -> DecisionView[]
POST /api/items/{id}/review {corrections} -> ReviewView
GET  /api/items/{id}/explain?field=title -> ExplanationView
GET  /api/library           -> LibraryEntryView[]
GET  /api/doctor            -> DoctorReport
GET  /api/models            -> ModelView[]
```

- Routes только вызывают `AsyncTrackForge`/use cases; не импортируют адаптеры.
- `POST /api/runs` принимает `SourceRef`/`PlanView`; не выполняет sync-блокирующий вызов в event loop.
- SSE: `text/event-stream`, events из EventBus; heartbeat; cancel при disconnect; no replay beyond run.
- Errors: 400 user error, 401 auth, 404 not found, 409 conflict, 422 validation, 429 rate limit, 500 internal; body `{code, message, retryable, user_action, context?}` без секретов.
- `--json` для CLI `serve` — только startup info (host/port/token path), не token value.

### Security

- Локальный token обязателен для всех `/api/*`, кроме `/healthz`.
- Не отдавать пути с секретами/куки; `doctor` redacted.
- Path параметры — ID/field, не произвольные пути; library scan — только внутри root.
- SSRF/redirect не применимы к HTTP API (нет URL fetch), но source URL проходит существующий guard.
- Rate limit на HTTP (простой) — опционально; не в v1.

## Порядок работ

1. Добавь `[http]` extra и `create_app`.
2. Реализуй auth/token/healthz.
3. Реализуй plan/runs/items/decisions routes.
4. Реализуй SSE/cancel.
5. Реализуй review/explain/library/doctor/models.
6. Добавь `serve` CLI.
7. Добавь TestClient-тесты; проверь no network.
8. Прогони quality gates; закоммить.

## Тесты

- **unit:** token compare; CORS; error mapping; DTO serialization; route validation.
- **contract:** routes не импортируют adapters; `AsyncTrackForge` fake; SSE event schema.
- **integration:** TestClient: plan → run → events → item → decisions → review → explain; cancel; library/doctor.
- **fixtures:** fake facade, temp token, local item.
- **negative:** no token/wrong token; remote host; invalid ID; review scope wider item; path traversal in library; secret redaction; SSE disconnect.

## Проверка

```bash
uv sync --extra tagging --extra http
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/http -q
uv run lint-imports
uv run trackforge serve --host 127.0.0.1 --port 0 --json
```

Ожидается: тесты зелёные; `serve` печатает host/port/token path, не token; `/healthz` без auth; `/api/*` без token → 401.

## Acceptance criteria

- [ ] `uv run pytest tests/http -q` зелёный.
- [ ] `trackforge serve` bind только 127.0.0.1; token обязателен.
- [ ] plan/run/status/events/cancel работают через TestClient.
- [ ] review/explain/library/doctor отдают те же DTO, что Python API.
- [ ] SSE отдаёт события и корректно закрывается.
- [ ] Ошибки типизированы/redacted; секреты не отдаются.
- [ ] Routes не импортируют adapters/domain напрямую; бизнес-логики нет.
- [ ] GUI отсутствует и помечен Deferred.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.

## Handoff

- HTTP API готов для будущего GUI/внешних клиентов.
- `AsyncTrackForge` — единственная точка входа; DTO стабильны.
- GUI — отдельный план v1.1, не блокирует v1.0.

## Stop conditions / decision gates

- Нет `AsyncTrackForge` — стоп; не создавай второй фасад.
- D6 не выбран — JSON-объекты; SSE — отдельный поток.
- Нужен GUI/frontend — Deferred; не добавляй Svelte/React.
- Нужен remote access/auth users — стоп; это non-goal v1.
- Нужны адаптеры в routes — стоп: нарушение слоя.
