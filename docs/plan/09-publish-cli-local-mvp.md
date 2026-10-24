# Plan 09: Publish + CLI local MVP

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 06, 07, 08
- **Разблокирует:** Plan 11, 12, 15, 18, 19
- **Оценка:** L
- **Риск:** высокий
## Роль

Ты — senior Python engineer, отвечающий за первый законченный пользовательский сценарий. Работаешь в `trackforge` после render. Задача — managed library, CLI, review/explain/doctor и end-to-end локальный импорт.
## Цель

Работает `trackforge import <local-file>`: файл проходит plan → acquire → observe → interpret → resolve → render → publish, в managed library появляется файл с корректными тегами, sidecar и LRC (если есть), оригинал не изменён. CLI умеет `plan`, `resolve`, `render`, `publish`, `review`, `explain`, `doctor`, `status`, `config`; повторный запуск использует кэш/resume.

## Контекст и инварианты

- Managed library: оригинал не трогаем; output — производный файл + sidecar (`Q3`).
- Публикация атомарна: temp в том же root → rename; коллизия — суффикс, не перезапись (`03-pipeline.md` §2.8).
- Только внутри `library_root`; sanitize имён, запрет `..`/symlink escape (`04-storage.md` §8).
- Naming: `{artist} - {title} ({qualifier})`; fallback `Unknown Artist - Unknown Title [<short-id>]` (Q28).
- LRC/ELRC — рядом с audio, тот же basename; sidecar `trackforge.json` рядом.
- Один item — один worker; плейлисты позже.
- CLI — тонкий клиент; бизнес-логика в application services (`09-interfaces.md` §1).
- `--json` — один объект на команду (D6); ошибки в stderr; exit codes 0/2/3/4/5.
- Секреты/куки не логируются; `doctor` не печатает значения env.

## Прочитать перед началом

- `docs/design/09-interfaces.md` — §2–3, §6–7.
- `docs/design/04-storage.md` — §7–8.
- `docs/design/03-pipeline.md` — §2.8, §6, §9, §12.
- `docs/design/10-operations.md` — §2–4, §6.
- `docs/design/08-metadata.md` — §6.
- `docs/design/07-resolution.md` — §10–11.
- `src/trackforge/application/use_cases/`, `interfaces/cli/`, `adapters/persistence/filesystem/`.
- `docs/plan/08-render-tag-writer.md` (handoff).

## Предусловия

```bash
uv sync --extra tagging
ffmpeg -version
ffprobe -version
uv run ruff check .
uv run mypy src
uv run pytest tests/integration/test_render_formats.py -q
uv run lint-imports
```

Зелёные. `RenderItem` и `OutputBundle` доступны; mutagen extra установлен.
## Scope

- `application/use_cases/publish_item.py`, `process_item.py`, `import_path.py`, `review_item.py`, `doctor.py`.
- `application/dto/results.py`, `views.py`.
- `adapters/publishing/filesystem.py`: naming, sanitize, atomic publish, collisions.
- CLI: `import`, `plan`, `resolve`, `render`, `publish`, `review`, `explain`, `doctor`, `status`, `resume`, `config show/validate`.
- `ProcessItem` orchestration и run/step resume.
- Публичный Python API: `AsyncTrackForge`, `TrackForge`, `RunOptions`; CLI использует его.
- Basic review: accept/edit/lock/unlock/unknown/why/skip; feedback event + user claim.
- `doctor` checks.
- E2E tests и CLI tests.
## Non-goals

- URL/yt-dlp (`add`/`plan` для URL) — Plan 11.
- Metadata providers, LLM, ASR/Demucs.
- `feedback export/stats` — Plan 15.
- HTTP/GUI/TUI, in-place/hardlink/move, backup/GC, плейлисты/папки.
- Транслитерация и Windows reserved names (sanitize — уже сейчас).
## Deliverables

```text
src/trackforge/application/use_cases/{publish_item,process_item,import_path,review_item,doctor}.py
src/trackforge/application/dto/{results,views}.py
src/trackforge/application/ports/publishing.py
src/trackforge/api.py
src/trackforge/__init__.py                 # экспорт публичного API
src/trackforge/adapters/publishing/{__init__,filesystem}.py
src/trackforge/interfaces/cli/commands/{import_path,plan,resolve,render,publish,review,explain,doctor,status,resume,config}.py
src/trackforge/interfaces/cli/presenters.py
tests/cli/{test_import,test_review,test_doctor,test_json_exit_codes}.py
tests/api/test_facade.py
tests/integration/{test_local_e2e,test_publish}.py
```
## Контракты
### Naming / publish

```text
NamingTemplate = "{artist} - {title} ({qualifier})"
fallback = "Unknown Artist - Unknown Title [<short-id>]"

Publisher.publish(bundle: OutputBundle, library_root: Path, naming: str,
                  options: PublishOptions(copy, sidecar, lrc, collision=suffix)) -> LibraryEntry
```

- `{artist}` — первый main artist или `Artist feat. X`; `{title}` — финальный title без qualifier; `{qualifier}` — Nightcore/Remix/...; скобки не добавляются, если qualifier пуст; если title уже содержит qualifier — не дублировать.
- Sanitize: `/ \ : * ? " < > |`, control chars, `\0`; trim, collapse whitespace, trailing dots/spaces; max 180 bytes на компонент; `.`/`..` запрещены; reserved names Windows sanitize уже сейчас.
- `library_root` default `~/Music/TrackForge`; output только внутри; symlink за пределы root запрещён.
- Flat для одиночного; подпапка коллекции — Plan 18.
- Коллизия: ` (1)`, ` (2)`, ...; никогда не перезаписывать существующий файл.
- Идемпотентность: если `library_entries` уже есть и файл/bundle hash совпадают — вернуть entry, не копировать.
- Атомарно: temp в `library_root/.tmp/`, fsync, rename; sidecar/LRC копируются рядом.
- `LibraryEntry(status=published)`; путь и bundle id в SQLite.
- Ошибка прав/диска → `PublishFailed`; частичный файл удаляется.

### ProcessItem

```text
ProcessItem.run(source: SourceRef, options: RunOptions) -> RunResult
```

- Создаёт `Run(status=planned)`, `run_item`, steps; выполняет use cases по стадиям.
- `RunOptions`: `profile`, `mode`, `auto`, `review`, `force_steps`, `offline`, `dry_run`, `target_format`.
- `--dry-run`: без side effects/скачивания/копирования, возвращает `PlanView`.
- `--auto` — без вопросов; `--review` — показать решения до render/publish.
- Ошибка стадии → run `failed`, item status; resume по `run_id`.
- Повторный import: acquire/observe cache hit; resolve новый record; render/publish idempotent.
- `--force` игнорирует кэш выбранных шагов; старые артефакты не удаляет.

### Public API

```text
AsyncTrackForge(config): async context manager.
  plan, run, import_path, analyze, resolve, render, publish, review, explain, doctor
  events(run_id) -> AsyncIterator[Event]; cancel(run_id); close()
TrackForge(config): sync-обёртка; те же методы; внутри запускает event loop.
```

- `TrackForge` внутри уже работающего loop → `RuntimeError` с указанием `AsyncTrackForge`.
- `trackforge.__init__` экспортирует `TrackForge`, `AsyncTrackForge`, `TrackForgeConfig`, `RunOptions`.
- CLI использует `AsyncTrackForge`; не собирает use cases вручную.
- DTO frozen; мутации — только через методы/correction request.
- Тест: `from trackforge import AsyncTrackForge, TrackForge`; sync wrapper не deadlock.

### CLI

```text
trackforge import <path> [--profile] [--mode] [--auto|--review] [--dry-run]
                         [--json] [--force step,...] [--target-format] [--offline]
                         [--data-dir] [--library-dir]
trackforge {plan,resolve,render,publish} <path|item-id> [--json] [--target-format]
trackforge review <item-id> [--auto] [--json] [--field] [--set] [--lock] [--unlock] [--reason]
trackforge explain <item-id> --field <field> [--json]
trackforge {status,resume} <run-id> [--json]
trackforge {doctor,config show,config validate} [--json]
```

- `import` — основной E2E; `plan` — dry-run для local.
- `resolve`/`render`/`publish` используют item из БД; без скрытого полного pipeline.
- `review` actions: `accept`, `edit`, `lock`, `unlock`, `unknown`, `why`, `skip`; `--json` — `ReviewView`.
- `review --set field=value --lock` создаёт user claim (`source=user`, `method=manual_override`, `confidence=1.0`), `Correction`/feedback event, lock; затем re-resolve; output не меняется до `render/publish`.
- `explain` — `ExplanationView`.
- `doctor` — human/`--json`; exit 2 при critical.
- `--json` — один JSON-объект; ошибки — JSON в stderr при `--json`? Допустимо: stdout пуст, stderr JSON; зафиксировать в тесте.
- Exit codes: 0 успех, 2 user error, 3 provider/network, 4 partial/degraded, 5 internal; 130 Ctrl+C.
- `add` (URL) — Deferred до Plan 11; `analyze` — API-метод, CLI-команда не входит в local MVP.

### Doctor

Проверяет: Python 3.12, `ffmpeg`, `ffprobe`, `mutagen`, права `config/data/state/library`, схему БД/Alembic, свободное место, `fpcalc` (warning), `yt-dlp`/`deno` (warning), конфликтующие настройки. `DoctorReport(checks[])`, `status: ok|warning|critical`.
## Порядок работ

1. Реализуй naming/sanitize/collision и publish atomic.
2. Реализуй `ProcessItem`/`ImportPath` поверх use cases и executor.
3. Реализуй CLI-команды и presenters; `--json`/exit codes.
4. Реализуй review (accept/edit/lock/unknown/why) и feedback event.
5. Реализуй `doctor`.
6. Добавь CLI/integration тесты и E2E.
7. Прогони quality gates; закоммить.
## Тесты

- **unit:** naming template/qualifier/fallback; sanitize Unicode/length/reserved; collision; path traversal; publish idempotency; exit code mapping; JSON shape.
- **contract:** `Publisher` fake; CLI commands вызывают services, не адаптеры; `--json` schema.
- **integration:** local MP3 + FLAC E2E: import → library file + sidecar + LRC; original unchanged; second import cache/resume; review lock → re-render → new output; explain; doctor.
- **fixtures:** generated media, dirty tags, Unicode title, long title, collision file, temp library.
- **negative:** library path traversal; read-only library; existing file collision; missing ffmpeg (doctor); invalid item id; review invalid field; `--json` error shape.
## Проверка

```bash
uv sync --extra tagging
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/cli tests/integration/test_local_e2e.py tests/integration/test_publish.py -q
uv run lint-imports
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=2" -q:a 4 /tmp/tf-sine.mp3
uv run trackforge import /tmp/tf-sine.mp3 --data-dir /tmp/tf-data --library-dir /tmp/tf-lib --auto --json
uv run trackforge doctor --json
```

Ожидается: exit 0; файл в `/tmp/tf-lib`; sidecar; теги читаются; повторный import сообщает cache hit; review lock сохраняется; doctor `ok`/`warning`, но не `critical`.
## Acceptance criteria

- [ ] `uv run pytest tests/cli tests/integration/test_local_e2e.py tests/integration/test_publish.py -q` зелёный.
- [ ] `trackforge import <fixture>` создаёт файл в `library_dir`, sidecar и LRC (если lyrics есть).
- [ ] Оригинальный файл не изменён; CAS-объект не изменён.
- [ ] Повторный import не качает/не копирует заново; cache/resume наблюдаемы.
- [ ] `review --set title=... --lock` сохраняет claim/lock/feedback; `explain` показывает новое решение; `render/publish` создаёт новый bundle.
- [ ] Collision не перезаписывает существующий файл; path traversal отклоняется.
- [ ] `from trackforge import AsyncTrackForge, TrackForge` работает; sync-фасад не deadlock и явно падает внутри loop.
- [ ] `--json` даёт валидный JSON; exit codes соответствуют ошибкам.
- [ ] `doctor` ловит отсутствие ffmpeg/прав и возвращает 2 при critical.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- `ProcessItem`/`ImportPath`/`PublishItem`.
- Managed library, naming/sanitize/collision, atomic publish.
- CLI local MVP и `--json`/exit codes.
- Basic review/locks/feedback event; `explain`.
- `doctor` и E2E-тесты.
## Stop conditions / decision gates

- D6 не выбран: используй один JSON-объект; если нужен JSONL — стоп.
- Нужен URL/`add` — Plan 11.
- Нужны feedback export/stats — Plan 15.
- Нужна запись за пределы `library_root` — стоп; это нарушает инвариант.
- `review` требует широкую scope (`source/artist/...`) — Plan 15; в v1 только item/field.
