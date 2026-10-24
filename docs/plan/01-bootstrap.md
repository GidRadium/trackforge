# Plan 01: Bootstrap и quality gates

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** —
- **Разблокирует:** Plan 02, 03, 04, 10, 13
- **Оценка:** M
- **Риск:** низкий

## Роль

Ты — senior Python engineer. Работаешь в репозитории `trackforge` с пустым `src/` и готовыми проектными документами. Не выходи за scope этого плана: здесь нет бизнес-логики TrackForge.

## Цель

Появляется воспроизводимый `uv`-проект с src layout, минимальной конфигурацией, CLI-каркасом и зелёными quality gates (`ruff`, `mypy`, `pytest`, `import-linter`), на которые могут опираться все последующие планы.

## Контекст и инварианты

- Python 3.12+, `uv`, `pyproject.toml`, пакет `src/trackforge` — из `02-architecture.md` §1, §4.
- Слои: `interfaces → application → domain`; `adapters` реализуют порты; сборка — в `bootstrap` (`ADR-0001`).
- `domain` не импортирует I/O-библиотеки; ядро не импортирует GPL/AGPL (`ADR-0005`).
- Linux-only v1; пути — XDG через `platformdirs`, env-override `TRACKFORGE_*` (`10-operations.md`).
- Секреты — только `.env`/env; в этом плане секретов нет.
- Root `README.md`, `LICENSE`, `docs/design/` и `docs/plan/00-META-PROMPT.md` не изменяются.
- Пустые «на будущее» модули не создаются: только пакеты и файлы, нужные для каркаса и проверок.

## Прочитать перед началом

- `docs/design/README.md` — карта документов и процесс.
- `docs/design/02-architecture.md` — §1–4, §12, §15.
- `docs/design/09-interfaces.md` — §1–2, §3.1–3.3, §7.
- `docs/design/10-operations.md` — §1–4, §6.
- `docs/design/11-quality.md` — §1–2, §5, §7.
- `docs/design/adr/0001-modular-monolith-hexagonal.md`.
- `docs/design/adr/0005-gpl-license-boundary.md`.
- `README.md`, `LICENSE` (шапка), `.gitignore`.

## Предусловия

Проверь и при неудаче остановись:

```bash
python3 --version        # 3.12.x
uv --version
git status --short
test -d docs/design
```

`uv` обязателен. Если его нет — сообщи, что нужна установка toolchain; не устанавливай системные пакеты самостоятельно. `ffmpeg` на этом плане не нужен.

## Scope

- `pyproject.toml` с hatchling, src layout, core/dev dependencies.
- Пакеты `trackforge`, `trackforge.domain`, `trackforge.application`, `trackforge.interfaces.cli`, `trackforge.bootstrap`, `trackforge.config`; пустые package roots `adapters`/`pipeline` — только для import-linter, модули позже.
- `TrackForgeConfig` (pydantic-settings): пути XDG, `profile`, `mode`, `log_level`, `offline`; TOML + env.
- CLI-каркас `trackforge`: `--version`, `config show`, `config validate`.
- Structured logging bootstrap без секретов.
- `ruff`, `mypy`, `pytest`, `import-linter`, CI workflow.
- Тесты на config/CLI/import boundaries.

## Non-goals

- Домен, storage, pipeline, адаптеры, doctor, review, render.
- HTTP/GUI, плагины, модели.
- Установка `mutagen`, `httpx`, `sqlalchemy`, `typer`-команд кроме каркаса.
- Изменение `docs/design/`, `docs/plan/00-META-PROMPT.md`, `LICENSE`.

## Deliverables

```text
pyproject.toml
uv.lock
.importlinter
.github/workflows/ci.yml
src/trackforge/__init__.py
src/trackforge/__main__.py
src/trackforge/config/__init__.py
src/trackforge/config/settings.py
src/trackforge/config/errors.py
src/trackforge/bootstrap/__init__.py
src/trackforge/bootstrap/container.py
src/trackforge/bootstrap/logging.py
src/trackforge/domain/__init__.py
src/trackforge/application/__init__.py
src/trackforge/adapters/__init__.py
src/trackforge/pipeline/__init__.py
src/trackforge/interfaces/__init__.py
src/trackforge/interfaces/cli/__init__.py
src/trackforge/interfaces/cli/main.py
tests/conftest.py
tests/unit/test_config.py
tests/unit/test_cli.py
tests/contract/test_layering.py
```

`trackforge` — import package. Версия — `0.1.0` (Plan 20 меняет на `1.0.0`). Entry point: `trackforge = "trackforge.interfaces.cli.main:app"`.

## Контракты

### Config

```text
TrackForgeConfig:
  config_dir: Path      # TRACKFORGE_CONFIG_DIR, default platformdirs user_config_dir
  data_dir: Path        # TRACKFORGE_DATA_DIR, default user_data_dir
  cache_dir: Path       # TRACKFORGE_CACHE_DIR, default user_cache_dir
  state_dir: Path       # TRACKFORGE_STATE_DIR, default user_state_dir
  library_dir: Path     # TRACKFORGE_LIBRARY_DIR, default ~/Music/TrackForge
  profile: fast | balanced | full | custom = balanced
  mode: policy | llm_assisted | llm_autopilot | manual = policy
  log_level: DEBUG | INFO | WARNING | ERROR = INFO
  offline: bool = False
```

- `TrackForgeConfig.from_file(path: Path | None = None) -> TrackForgeConfig`.
- Приоритет: defaults < `config.toml` < env < явные аргументы конструктора.
- Ошибка конфига — `ConfigError` с именем ключа; без traceback в CLI.
- Конфиг не создаёт директории и не читает `.env`.

### CLI

```text
trackforge --version
trackforge config show [--json]
trackforge config validate [--json]
```

- `--version` печатает `trackforge 0.1.0`, exit 0.
- `config show --json` — один JSON-объект в stdout (D6).
- `config validate` — exit 0 при валидном конфиге, exit 2 при user error.
- Ошибки — в stderr; exit codes: 0, 2, 5 (`09-interfaces.md`).
- CLI не содержит бизнес-логики: только вызов config/container.

### Layering / license

`.importlinter` фиксирует:

1. `trackforge.interfaces` может импортировать `trackforge.application`/`trackforge.domain`; `application` — только `domain`; `domain` — только stdlib.
2. `trackforge.domain`, `trackforge.application`, `trackforge.pipeline` не импортируют `trackforge.adapters`, `trackforge.interfaces`, `trackforge.bootstrap`.
3. Ядро не импортирует `mutagen`, `essentia`, `torch`, `faster_whisper`, `demucs`, `onnxruntime`.
4. `domain` не импортирует `pydantic`, `sqlalchemy`, `httpx`, `typer`, `rich`.

Допустимое исключение: `pydantic`/`pydantic-settings` в `config/` и application DTO; это не домен.

## Порядок работ

1. Создай `pyproject.toml`: hatchling, `requires-python = ">=3.12"`, зависимости `pydantic`, `pydantic-settings`, `platformdirs`, `typer`, `rich`; dev — `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `import-linter`. Лицензии: MIT/Apache-2.0/BSD; GPL/AGPL нет.
2. Настрой `ruff` (py312, line-length 100, импорты, bugbear, security), `mypy` (strict, `src`), `pytest` (маркеры `unit`, `contract`, `integration`, `network`, `slow`, `spike`; `addopts = -m "not network and not slow"`).
3. Создай пакеты и `__init__.py`; не добавляй пустые модули.
4. Реализуй `TrackForgeConfig` и `ConfigError`; TOML-загрузка через stdlib `tomllib`; env через pydantic-settings; никаких секретов.
5. Реализуй `bootstrap/container.py` с `AppContext(config)` и `build_context(config)`; logging — JSONL-совместимый formatter, redaction hook (пока пустой список секретов).
6. Реализуй CLI на Typer: `--version`, `config show`, `config validate`; `--json` — один объект.
7. Добавь `.importlinter` и тест-контракт, который запускает `lint-imports` (или проверяет конфиг).
8. Добавь CI: Python 3.12, `uv sync --dev`, `ruff check`, `ruff format --check`, `mypy src`, `pytest`, `lint-imports`. Сеть не нужна; `uv` cache.
9. Добавь `tests/conftest.py` с `tmp_path`-конфигом и фикстурой `config`.
10. Прогони все проверки и закоммить.

## Тесты

- **unit:** defaults путей; env override; TOML override; env сильнее TOML; invalid profile/mode → `ConfigError`; `--version`; `config show --json` парсится как JSON; `config validate` exit 0/2.
- **contract:** import-linter зелёный; `trackforge.domain` не импортирует pydantic/sqlalchemy/httpx/typer; `application` не импортирует adapters/interfaces/bootstrap.
- **integration:** нет.
- **fixtures:** `tmp_path`; TOML-файл с `[core] profile = "fast"`; env `TRACKFORGE_CORE_PROFILE` (или согласованный alias).
- **negative:** несуществующий TOML; неизвестный ключ; невалидный log level; `TRACKFORGE_DATA_DIR` с пустым значением.

## Проверка

```bash
uv lock
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run lint-imports
uv run trackforge --version
uv run trackforge config show --json
```

Ожидается: все команды exit 0; `--version` печатает `trackforge 0.1.0`; `config show --json` — валидный JSON; `pytest` не делает сетевых вызовов.

## Acceptance criteria

- [ ] `uv sync --dev` завершается успешно, `uv.lock` закоммичен.
- [ ] `uv run trackforge --version` печатает версию и возвращает 0.
- [ ] `config show --json` и `config validate` работают; invalid config даёт exit 2 и понятную ошибку.
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest`, `uv run lint-imports` — зелёные.
- [ ] В `src/trackforge/domain` и `src/trackforge/application` нет импортов `mutagen`, `essentia`, `torch`, `faster_whisper`, `demucs`, `onnxruntime`, `sqlalchemy`, `httpx`, `typer`.
- [ ] CI workflow содержит все quality gates и не требует сети, кроме установки зависимостей.
- [ ] `docs/design/`, `LICENSE`, root `README.md` не изменены.

## Handoff

Следующие планы могут считать готовыми:

- `trackforge` устанавливается и запускается через `uv run`.
- `TrackForgeConfig` и env/TOML-контракт существуют.
- `AppContext`/`build_context` — точка сборки.
- `ruff`, `mypy`, `pytest`, `lint-imports` и CI работают.
- Пакеты слоёв созданы; `adapters`/`pipeline` появятся в следующих планах.

## Stop conditions / decision gates

- Нет `uv` или Python 3.12 — стоп, сообщи; не устанавливай системные пакеты без разрешения.
- Нужно нарушить слои (`domain` импортирует pydantic/SQLAlchemy) — стоп: это архитектурное решение, не обход.
- Для ID/БД/HTTP требуются решения — они в следующих планах (D2–D4); здесь не выбирай.
- Если CI-провайдер недоступен, создай локальный workflow и зафиксируй, что remote CI не проверен; не выдумывай зелёный статус.
