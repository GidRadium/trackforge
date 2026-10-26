# Plan 20: Hardening + release v1.0

- **Статус:** ready
- **Тип:** MVP (release)
- **Зависит от:** Plan 01–15; Plan 16–18.1 — conditional; Plan 19 — optional
- **Разблокирует:** релиз
- **Оценка:** L
- **Риск:** средний

## Роль

Ты — release engineer / senior Python engineer. Работаешь в `trackforge` после основных вертикальных срезов. Задача — безопасность, лицензии, backup/GC, упаковка, smoke и релиз; без новых future-фич.

## Цель

Собран и проверен `trackforge 1.0.0` (или `0.9.0` по D10): wheel ставится в чистое окружение, `doctor` проходит, локальный E2E и (если доступен) URL E2E работают, миграции применяются, backup/restore и `gc --dry-run` проверены, license/security audit закрыт, CHANGELOG и root README обновлены. Релизный артефакт `docs/plan/artifacts/20-release-report.md` заполнен.

## Контекст и инварианты

- GPL-3.0-or-later; ядро не импортирует GPL/AGPL; optional extras явные (`ADR-0005`).
- Linux-only v1; Python 3.12+; CPU-only; 1k–10k треков; playlists до 500 (`Q32`).
- Оригиналы не изменяются; publish не перезаписывает; секреты не логируются.
- LRC/ELRC — только sidecar; managed library + sidecar.
- Все миграции — Alembic; backup/restore не теряет decisions/feedback.
- Release не добавляет новые фичи; только hardening и операции, перечисленные в docs.
- `docs/design/` не изменяется; root `README.md` можно обновить (install/usage/status), `LICENSE` — нет.

## Прочитать перед началом

- `docs/design/11-quality.md` — §5–9.
- `docs/design/10-operations.md` — §4, §7–8.
- `docs/design/04-storage.md` — §11–12.
- `docs/design/02-architecture.md` — §9.5, §16.
- `docs/design/12-roadmap.md` — §9–11.
- `docs/design/01-domain-model.md` — §11.
- `docs/plan/artifacts/10-spike-ytdlp.md`, `docs/plan/artifacts/13-spike-cpu-models.md`, `docs/plan/artifacts/14-llm-preflight.md`.
- `docs/plan/19-http-gui.md` (optional).

## Предусловия

```bash
uv sync --extra tagging
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run lint-imports
```

Зелёные. Plans 16–18.1 могут быть `deferred`; зафиксируй это в release report. Plan 19 optional и не блокирует. `uv build` должен работать; для smoke нужны `ffmpeg`/`ffprobe`, локальный файл и (опционально) URL.
## Scope

- Security audit: SSRF/path traversal/symlink/secrets/cookies/SQL/no-DRM; plugin surface отсутствует в v1.0 (Deferred).
- License audit: direct/transitive deps, models, external binaries; extras; license table.
- `trackforge backup`, `trackforge library scan|verify`, `trackforge gc --dry-run` (и `--yes`).
- `doctor` schema/migration/free-space checks; repair — Deferred.
- Packaging: `uv build`; wheel data (prompts); install in clean venv; `uv tool install`.
- CI: Python 3.12 (+3.13 optional), all mandatory gates, no network.
- CHANGELOG, version `1.0.0`/`0.9.0`, root README install/usage/status.
- Release smoke: local E2E + optional URL E2E; backup/restore; migration empty/upgrade.
- Release report artifact.
## Non-goals

- Новые анализаторы/источники/GUI/плагины.
- Cross-platform packaging, Windows/macOS.
- In-place tagging, hardlink, move mode.
- Автоматическое обучение/PreferenceRule.
- Essentia/AGPL, local 0.5B/7B (Deferred).
- Публикация в PyPI (если не запрошено отдельно).
## Deliverables

```text
src/trackforge/application/use_cases/{backup,library_scan,gc}.py
src/trackforge/interfaces/cli/commands/{backup,library,gc}.py
CHANGELOG.md
docs/plan/artifacts/20-release-report.md
pyproject.toml                     # version, classifiers, package data
README.md                          # install/usage/status
.github/workflows/ci.yml           # финальная матрица/гейты
dist/trackforge-*.whl              # артефакт сборки, не коммитить
```
## Контракты

### Backup / restore

```text
trackforge backup --output <dir> [--include-cas] [--json]
```

- Копирует `catalog.sqlite` (consistent snapshot: WAL checkpoint/backup API), `config.toml`, `feedback/`, `items/` snapshots; CAS — optional.
- Manifest `backup.json`: версия, дата, список файлов/хэши, schema version, CAS included.
- Не копирует `.env`/cookies; предупреждает.
- Restore: документированная процедура (остановить процессы, распаковать, `doctor`), автоматический `restore` — Deferred.
- Backup не меняет источник; read-only.

### Library scan / verify

```text
trackforge library scan [--json]
trackforge library verify [--json]
```

- `scan`: найти файлы в library без `library_entries`, entries без файлов, path mismatch; statuses `missing|modified|untracked`.
- `verify`: сверить bundle/content hash, sidecar schema, LRC presence; не перезаписывать.
- Не удалять автоматически; recommendations only.

### GC

```text
trackforge gc [--dry-run] [--yes] [--json]
```

- Найти unreferenced CAS/artifacts; учитывать `library_entries`, `renders`, `output_bundles`, `feedback` refs.
- Default `--dry-run`; удаление только с `--yes`; не удалять feedback/decisions.
- Symlink/path safety; report bytes freed.
- Не удалять объекты, на которые есть ссылки в БД/артефактах.

### Security / license

- `rg`/`ruff S`/manual checklist: no secrets, cookies 0600, no path traversal, SSRF, parameterized SQL, no DRM.
- License table: project GPL-3.0-or-later; mutagen GPL-2.0-or-later (extra); demucs MIT; faster-whisper MIT; torch BSD-3; cross-encoder Apache/MIT; ffmpeg LGPL/GPL external; Chromaprint LGPL external; yt-dlp Unlicense; FastAPI/uvicorn MIT/BSD.
- `pip-licenses`/`uv tree` (dev-only) — не добавлять в core.
- Нет GPL/AGPL imports в `domain/application/pipeline`; `lint-imports`.
- Model licenses/checksums в `model_registry`/release report.

### Packaging / version

- `version = "1.0.0"` при D10(b); `0.9.0` при URL no-go.
- Wheel включает `src/trackforge/prompts/`; `package-data`.
- `uv build`, `uv pip install --python <temp-venv> dist/*.whl`, `trackforge --version`, `trackforge doctor`.
- `uv tool install dist/*.whl` smoke — опционально.
- `CHANGELOG.md`: Added/Changed/Fixed/Deferred/Known issues; без TBD.
- Root README: install, quickstart, extras, license boundary, known limitations.

### Release report

`docs/plan/artifacts/20-release-report.md`:

```text
1. Version/commit/date; scope (which plans green/deferred).
2. Environment (Linux, Python, ffmpeg, models).
3. Commands + results: lint/type/tests/build/doctor/smoke.
4. Security/license checklist results.
5. Migrations empty/upgrade; backup/restore; gc dry-run.
6. Optional URL live smoke (redacted).
7. Known limitations/deferred.
8. Go/no-go for release.
```
## Порядок работ

1. Заморозить scope по D10; записать green/deferred планы.
2. Реализовать backup/library scan/verify/gc + CLI + тесты.
3. Провести security checklist и license audit; исправить найденное.
4. Обновить CI/package data/version/CHANGELOG/README.
5. Собрать wheel, поставить в чистый venv, прогнать `doctor`/smoke.
6. Проверить миграции с пустой/старой БД; backup/restore; gc dry-run.
7. Заполнить release report; прогнать все gates; закоммить/тег.
8. Остановись: не добавляй фичи после release.
## Тесты

- **unit:** backup manifest; scan statuses; gc reference counting/dry-run; version; package data.
- **contract:** CLI commands; backup/restore docs; no GPL imports; wheel contents.
- **integration:** backup → restore в temp → doctor; gc dry-run не удаляет; library scan missing/modified/untracked; migration empty/upgrade.
- **smoke:** local file E2E; optional URL `network`; read-back.
- **negative:** backup без прав; restore с несовместимой schema; gc с referenced object; scan path traversal; secret in logs; missing extras.
## Проверка

```bash
uv sync --extra tagging
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run lint-imports
uv build
python3 -m venv /tmp/tf-release-venv
uv pip install --python /tmp/tf-release-venv/bin/python dist/*.whl
uv pip install --python /tmp/tf-release-venv/bin/python "mutagen>=1.47"  # tagging extra
/tmp/tf-release-venv/bin/trackforge --version
/tmp/tf-release-venv/bin/trackforge doctor --json
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=2" -q:a 4 /tmp/tf-rel-sine.mp3
uv run trackforge import /tmp/tf-rel-sine.mp3 --data-dir /tmp/tf-rel-data --library-dir /tmp/tf-rel-lib --auto --json
uv run trackforge backup --output /tmp/tf-backup --json
uv run trackforge library scan --json
uv run trackforge gc --dry-run --json
```

Ожидается: все gates зелёные; wheel ставится; `doctor` не critical; локальный smoke создаёт файл; backup/scan/gc работают; отчёт заполнен.
## Acceptance criteria

- [ ] `ruff`, `mypy`, `pytest`, `lint-imports` зелёные; CI обновлён.
- [ ] `uv build` создаёт wheel; установка в чистый venv работает; `trackforge --version` соответствует версии.
- [ ] `doctor` проходит на чистой среде (warnings допустимы, critical — нет).
- [ ] Local E2E smoke: файл + теги + sidecar + LRC; оригинал не изменён.
- [ ] Optional URL smoke выполнен или `deferred` с причиной.
- [ ] Миграции пустой/старой БД; backup/restore; `gc --dry-run` проверены.
- [ ] Security checklist закрыт; секреты/куки не в git/логах/артефактах.
- [ ] License audit: таблица, extras, model licenses; GPL boundary зелёный.
- [ ] CHANGELOG/README/version обновлены; `docs/design/` не изменён.
- [ ] `docs/plan/artifacts/20-release-report.md` заполнен; go/no-go явный.
- [ ] Релизный тег/коммит создан; после релиза фичи не добавляются.
## Handoff

Релиз завершён. Дальше — только поддержка и новые планы:
- v1.1: GUI, HTTP API (если Plan 19 не вошёл), Essentia, local 0.5B.
- Known limitations из release report — вход для следующих планов.
- Планы 01–20 остаются историей; изменения контрактов — через новые ADR/планы.

## Stop conditions / decision gates

- D10 не выбран — выполни `01–15` + зелёные 16–18.1, версия `1.0.0`; если URL no-go — `0.9.0`.
- Любой mandatory gate красный — стоп, не выпускай.
- License boundary нарушен — стоп; не «принимай GPL как есть» без решения пользователя.
- Нужна новая фича для релиза — стоп; это новый план, не release scope.
- Нет чистого окружения/моделей для smoke — зафиксируй `blocked`, не выдумывай успех.
- Backup/restore теряет decisions/feedback — стоп; релиз не готов.
