# 10. Operations

> Статус: **черновик v0.3**. Конфигурация, секреты, установка, конкурентность, логи, backup и эксплуатация.
> Проект личный и локальный, но «локальный» не значит «без операционных правил».

## 1. Принципы

1. **Явная конфигурация.** Никаких глобальных `settings`; `AppConfig` инжектится.
2. **Секреты — отдельно.** `.env`/keyring/cookies, не в `config.toml`, не в БД, не в логах.
3. **Воспроизводимость.** Версии моделей/промптов/схем фиксируются.
4. **Безопасные дефолты.** Оригиналы не трогаем; publish не перезаписывает; dry-run для опасного.
5. **Диагностируемость.** `doctor`, structured logs, run/step IDs.
6. **Local-first.** Без сервера, Redis, облачной БД; сеть — только для источников/LLM.

## 2. Конфигурация

### 2.1. Файлы

- `config.toml` — не секретные настройки.
- `.env` — секреты (API keys); не коммитить.
- `cookies/<site>.txt` — cookies, `0600`.
- `profiles.toml` — опционально, если профилей много (можно в `config.toml`).

Почему TOML, а не `config.txt`: секции, типы, комментарии, меньше парсинга руками. Если нужен `config.txt` — только как legacy/alias, не default.

### 2.2. Пример

```toml
[core]
data_dir = "~/.local/share/trackforge"
library_dir = "~/Music/TrackForge"
profile = "balanced"
mode = "policy"
log_level = "INFO"
offline = false

[library]
layout = "flat"                 # flat | collection
naming = "{artist} - {title} ({qualifier})"
cover_embed = true
lyrics_sidecar = true
id3_version = 4                 # 3 | 4
preserve_unknown_tags = false

[analysis]
enabled = []                    # дополнительные capability
disabled = []                   # явно выключенные
demucs = false
essentia = false

[models.asr]
backend = "faster-whisper"
name = "small"                  # модель выбирается здесь, не в коде
compute_type = "int8"

[models.ranker]
backend = "onnx"
name = "..."                    # cross-encoder; выбрать после бенчмарка
path = "..."

[models.local_llm]
enabled = false                 # optional fallback; API — primary
backend = "ollama"
base_url = "http://localhost:11434/v1"
name = "qwen2.5-0.5b-instruct"  # 0.5B class, classification/ranking only

[ranker]
strategy = "hybrid"             # lexical | cross_encoder | hybrid | local_llm

[llm]
# protocol/provider; ключ — только в .env через api_key_env
provider = "openai-compatible"
base_url = "https://api.openai.com/v1"
model = "..."
api_key_env = "TRACKFORGE_LLM_API_KEY"
max_cost_usd_per_item = 0.50
temperature = 0.1
audio_opt_in = false

[sources.youtube]
cookies = "~/.config/trackforge/cookies/youtube.txt"
js_runtime = "deno"             # проверить в spike

[sources.soundcloud]
cookies = "~/.config/trackforge/cookies/soundcloud.txt"

[feedback]
enabled = true
export_format = "jsonl"

[privacy]
send_text_to_cloud = true
send_audio_to_cloud = false
redact_logs = true
```

### 2.3. Слои

```text
defaults < config.toml < profiles.toml < env < CLI
```

Важно:

- Все ID моделей (`asr.name`, `ranker.name`, `local_llm.name`, `llm.model`) — **конфиг**, не хардкод в коде. Дефолты в примере — только стартовая точка; их можно менять без релиза.
- `[llm]` не содержит API key. `api_key_env` указывает на переменную в `.env`; сам ключ — только там/в env.
- `provider`/`base_url`/`model` — не секреты; они в `config.toml`, чтобы результат был воспроизводим.
- Любой несекретный ключ можно переопределить через env (`TRACKFORGE_LLM_PROVIDER`, `TRACKFORGE_LLM_BASE_URL`, ...). Это удобно для CI/экспериментов, но итоговый конфиг стоит фиксировать в `config show`.
- «Провайдера LLM по умолчанию» в коде нет: пользователь задаёт endpoint/model; без них LLM-шаги просто `skipped`/`degraded`.

- Env: `TRACKFORGE_*` (например, `TRACKFORGE_CORE_PROFILE`).
- CLI: `--profile`, `--mode`, `--library-dir`, `--offline`.
- Валидация — pydantic-settings; ошибка с ключом/значением.
- `trackforge config show` — итоговый конфиг (секреты redacted).
- `trackforge config validate` — проверка без запуска.

### 2.4. Профили

- `fast`, `balanced`, `full`, `custom`.
- Профиль — набор capability + бюджет + модель.
- Переопределение на уровне item/CLI.
- Профили хранятся как данные, не как код.

## 3. Секреты

### 3.1. Где

- `.env` в config dir (`0600`).
- ENV переменные.
- Cookies — отдельные файлы (`0600`).
- Keyring — опционально (позже).
- Никогда — в `config.toml`, БД, sidecar, логах, feedback.

### 3.2. Переменные (пример)

```text
TRACKFORGE_LLM_API_KEY
TRACKFORGE_ACOUSTID_API_KEY
TRACKFORGE_MUSICBRAINZ_USER_AGENT   # не секрет, но нужен
TRACKFORGE_LASTFM_API_KEY
TRACKFORGE_DEEZER_API_KEY?
TRACKFORGE_TAVILY_API_KEY?
```

### 3.3. Redaction

- Логи/ошибки/`doctor` не печатают значения секретов.
- URL с токенами — маскировать query.
- Cookies — только путь/наличие, не содержимое.
- `--debug` — redacted.

### 3.4. Права

- `config_dir` — `0700`.
- `.env`, cookies — `0600`.
- `catalog.sqlite` — `0600`.
- `data_dir` — `0700`.
- Проверка прав в `doctor`.

## 4. Установка и bootstrap

### 4.1. Требования

- Python 3.12+.
- `uv`.
- `ffmpeg`/`ffprobe`.
- `fpcalc` (Chromaprint) — для AcoustID.
- `deno`/JS runtime — для части YouTube (проверить).
- SQLite — в Python.
- Опционально: `faster-whisper`, `demucs`, `essentia`, `onnxruntime`, `torch`.

### 4.2. Шаги (предварительно)

```text
uv venv
uv sync
uv run trackforge doctor
uv run trackforge config init
uv run trackforge add <url|path>
```

Позже:

```text
uv tool install trackforge
trackforge doctor
```

### 4.3. Extras

- `[tagging]` — mutagen (GPL).
- `[asr]` — faster-whisper.
- `[stems]` — demucs.
- `[essentia]` — essentia (AGPL warning).
- `[ranker]` — onnxruntime + cross-encoder.
- `[llm]` — openai/httpx (если не в core).
- `[all]` — всё, кроме AGPL, если нужен безопасный default.

### 4.4. `doctor`

Проверяет:

- Python/uv/ffmpeg/ffprobe/fpcalc/deno;
- версии;
- права на config/data/library;
- наличие моделей;
- API keys (наличие, не значение);
- cookies (наличие/срок);
- схему БД/миграции;
- свободное место;
- доступность сети (опционально);
- плагины/contract version;
- конфликтующие настройки.

Вывод: human + `--json`; exit code 2 при критичных проблемах.

## 5. Конкурентность и ресурсы

- `asyncio` для I/O; process pool для CPU.
- `max_workers = min(cpu_count - 1, config)`; default — консервативный.
- Тяжёлые модели — не более 1 одновременно.
- Per-host rate limiter; глобальный лимит.
- Memory guard: перед загрузкой модели проверить budget.
- File lock на data dir; второй процесс ждёт/падает с понятной ошибкой.
- SQLite WAL; `busy_timeout`.
- Cancellation — cooperative.

## 6. Логи и observability

- Structured logs (JSONL) в `state/logs/`.
- Поля: `timestamp`, `level`, `event`, `run_id`, `item_id`, `step_id`, `capability`, `provider`, `duration_ms`, `status`, `warning_codes`.
- Ротация по размеру/возрасту; не хранить вечно.
- CLI — human-readable; `--json` — machine.
- `run.jsonl` — события конкретного run.
- Метрики: cache hit rate, provider latency/errors, tokens/cost, CPU time, degraded rate.
- Никакой телеметрии по умолчанию.

## 7. Backup / restore / GC

### 7.1. Backup

- `trackforge backup` — `catalog.sqlite`, `config.toml`, `feedback/`, `items/` (снимки), опционально CAS.
- CAS можно не бэкапить, если есть исходники; но sidecar/decisions — да.
- Библиотека — обычные файлы; sidecar рядом.

### 7.2. Restore

- Восстановить data/config; `doctor --repair` (позже) проверит ссылки.
- `library scan` найдёт расхождения.

### 7.3. GC

- `trackforge gc --dry-run` — найти unreferenced CAS/artifacts.
- Удаление — только после подтверждения.
- Feedback/decisions не удалять автоматически.
- Debug-артефакты — TTL.

## 8. Безопасность

### 8.1. URL/SSRF

- Только `http`/`https`.
- Блокировать private/link-local/loopback по умолчанию; разрешать local только явной настройкой.
- Ограничивать redirects/размер ответа.
- Не выполнять HTML/JS из источника.

### 8.2. Пути

- Output — только внутри `library_root`.
- Sanitize filename; не следовать symlink за пределы root.
- Temp — в controlled dir.

### 8.3. Плагины

- Trusted Python-код; без sandbox в v1.
- Документировать риск.
- `doctor` показывает плагины/лицензии.

### 8.4. Сеть/ToS

- Уважать rate limits/robots/ToS.
- Не обходить DRM/платный доступ.
- Пользователь отвечает за использование.

### 8.5. Feedback

- Локально; экспорт — opt-in.
- Redaction перед отправкой в облако.
- Не хранить аудио.

## 9. Кросс-платформенность и масштаб

- **v1 — только Linux.** Windows/macOS — позже.
- Целевая библиотека — 1k–10k треков; плейлисты — до 500 с паузами/resume.
- На будущее:
  - пути/reserved names;
  - `ffmpeg`/`fpcalc` binaries;
  - file locks;
  - line endings.
- Код не должен зависеть от Linux-only API без abstraction.

## 10. Troubleshooting

| Симптом | Вероятная причина | Что делать |
|---|---|---|
| `ffmpeg not found` | не установлен/не в PATH | `doctor`, установить |
| YouTube требует JS | нет Deno/yt-dlp устарел | обновить, `doctor` |
| `LoginRequired` | нет/просрочены cookies | обновить cookies |
| `RateLimited` | слишком много запросов | пауза/лимиты |
| LLM 401/403 | ключ/провайдер | проверить `.env` |
| `database is locked` | второй процесс | закрыть/подождать |
| Нет места | CAS/модели | `gc`, очистить |
| `Tag write failed` | права/формат | проверить output dir/format |
| Плохой ASR | модель/микс | Demucs/medium/lyrics |

## 11. Открытые вопросы

- `config.toml` vs `config.txt` — принят TOML; legacy не нужен.
- Keyring vs `.env` — начать с `.env`, keyring позже.
- Нужен ли systemd/launchd сервис для GUI — позже.
- Политика ротации логов/артефактов.
- Кросс-платформенная упаковка `ffmpeg`/`fpcalc`.
- Минимальные права/ACL для library.
