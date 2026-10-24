# Plan 11: URL ingest через yt-dlp

- **Статус:** conditional
- **Тип:** MVP
- **Зависит от:** Plan 09, Plan 10 (go)
- **Разблокирует:** Plan 12, 18
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за второй источник. Работаешь в `trackforge` после local MVP и зелёного spike Plan 10. Задача — subprocess-адаптер `yt-dlp`, candidate selection, cookies/JS runtime и ошибки; один URL, без плейлистов.

## Цель

Работает `trackforge add <url>`: URL → `Candidate` list → выбор/`--auto` → audio-only download → CAS + `SourceSnapshot` + `Intake` → существующий pipeline → managed library. `trackforge plan <url>` показывает кандидатов и оценки без скачивания.

## Контекст и инварианты

- `yt-dlp` — subprocess, список аргументов, без shell, timeout; JSON-вывод (`05-sources.md` §4.1).
- Приоритет audio-only: opus/m4a → bestaudio → best; DRM/платный контент не поддерживается.
- Cookies: один Netscape-файл на сайт, `0600`; не логировать; выбирать по домену.
- JS runtime: Deno/yt-dlp-ejs; без него часть YouTube деградирует видимо.
- `SourceSnapshot` (`info.json`) — immutable artifact; `source.metadata.v1` — нормализованные поля.
- `Intake` хранит downloader id/version; `MediaObject` — хэш байтов; оригинал не меняется.
- Ошибки маппятся в типизированные: `LoginRequired`, `CookiesExpired`, `RateLimited`, `GeoBlocked`, `DrmProtected`, `Unavailable`, `ExtractorBroken`, `UnsupportedUrl`.
- SSRF: только `http`/`https`; private/link-local/loopback блокируются по умолчанию; redirects ограничены.
- Плейлисты — Plan 18; здесь `--no-playlist`.

## Прочитать перед началом

- `docs/design/05-sources.md` — §2, §4, §8–9.
- `docs/design/03-pipeline.md` — §2.1–2.4, §10, §12.
- `docs/design/02-architecture.md` — §5, §9.5, §16.
- `docs/design/09-interfaces.md` — §3.1–3.4.
- `docs/design/10-operations.md` — §3, §8.1.
- `docs/plan/artifacts/10-spike-ytdlp.md` (результаты).
- `docs/plan/09-publish-cli-local-mvp.md` (handoff).

## Предусловия

```bash
test -f docs/plan/artifacts/10-spike-ytdlp.md
rg -n "Go|No-go|Blocked" docs/plan/artifacts/10-spike-ytdlp.md
yt-dlp --version
deno --version
uv run ruff check .
uv run mypy src
uv run pytest tests/cli tests/integration/test_local_e2e.py -q
uv run lint-imports
```

Plan 10 должен быть `Go`. Если `No-go`/`Blocked` — стоп, Plan 11 не выполняется; локальный MVP остаётся релизом. `yt-dlp`/Deno — внешние бинарники, не Python-зависимости.
## Scope

- `adapters/sources/ytdlp/client.py`: subprocess wrapper, timeout, JSON parse, progress.
- `adapters/sources/ytdlp/source.py`: `YtDlpSource`.
- `adapters/acquisition/ytdlp.py`: `YtDlpAcquirer`.
- `adapters/http/url_safety.py`: http/https + private IP guard.
- `adapters/sources/ytdlp/errors.py`: mapping.
- `application/use_cases/add_url.py` или расширение `process_item.py`.
- CLI `add`, `plan` для URL; `--auto`, `--json`.
- `doctor` расширить проверкой yt-dlp/Deno/cookies.
- JSON fixtures + fake executable.
## Non-goals

- Плейлисты/`--flat-playlist` (Plan 18).
- Кастомные сайты/парсеры.
- Обход DRM/платного доступа.
- Скачивание видео/выбор качества видео.
- Cookies через GUI/keyring.
- Автоматическое обновление yt-dlp.
## Deliverables

```text
src/trackforge/adapters/sources/ytdlp/__init__.py
src/trackforge/adapters/sources/ytdlp/client.py
src/trackforge/adapters/sources/ytdlp/source.py
src/trackforge/adapters/sources/ytdlp/errors.py
src/trackforge/adapters/acquisition/ytdlp.py
src/trackforge/adapters/http/url_safety.py
src/trackforge/interfaces/cli/commands/add.py
tests/adapters/ytdlp/{test_client,test_source,test_errors}.py
tests/cli/test_add_url.py
tests/fixtures/ytdlp/{single_youtube,single_soundcloud}.json
tests/fakes/yt-dlp
```
## Контракты

### Client

```text
YtDlpClient:
  version() -> str
  dump_json(url: str, opts: DumpOptions) -> dict
  download(url: str, candidate: Candidate, dest: Path, opts: DownloadOptions) -> DownloadedFile
```

- Всегда `subprocess.run([...], capture_output=True, text=True, timeout=...)`; без `shell=True`.
- `--dump-single-json --no-download --no-warnings --no-playlist`.
- `--no-playlist`; `--cookies <path>` только если домен совпадает с cookie-файлом.
- `--newline` + `--progress-template` для прогресса; progress events не логируют URL query с токенами.
- stderr ограничен (например, 8 KB) и redacted; JSON parse error → `ExtractorBroken`.
- Timeout: metadata 60 с, download 30 мин; конфигурируемо.
- Возвращает `DownloadedFile(path, info_json, container, size_bytes, duration)`.

### Source

```text
YtDlpSource:
  id = "ytdlp"
  supports(ref) -> bool
  resolve(ref, opts) -> CandidateList
```

- `supports` true для `Url` с `http`/`https`, прошедшим SSRF guard; иначе false/`UnsupportedSource`.
- `resolve` вызывает metadata-only; не скачивает.
- `Candidate`: `external_id`, `title`, `uploader`, `duration`, `formats[]` (`format_id`, `codec`, `bitrate`, `sample_rate`, `channels`, `language`, `size`, `drm`), `thumbnails[]`, `extra` с extractor.
- Форматы только audio-only (или `best` с audio); видео-форматы не предлагаются.
- `DrmProtected` если `has_drm`.
- `SourceSnapshot` — raw `info.json` artifact `source.snapshot.v1`; `source.metadata.v1` — нормализованные поля (`title`, `uploader`, `description`, `upload_date`, `tags`, `duration`, `thumbnails`, `chapters`).
- Cookies path: `<config_dir>/cookies/<site>.txt`; `0600`; отсутствие — warning, не ошибка для сайтов без login.
- `doctor` проверяет `yt-dlp --version`, `deno --version`, наличие cookies/права/срок (без содержимого).

### Acquirer / errors

```text
YtDlpAcquirer.acquire(candidate, opts) -> IntakeResult
```

- Скачивает в `cache_dir/tmp/<uuid>/`; проверяет размер/хэш; кладёт в CAS; создаёт Intake/MediaObject/Item.
- `SourceSnapshot` сохраняется до очистки temp.
- `downloader_id="ytdlp"`, `downloader_version` из `version()`.
- Error mapping:

| Условие | Ошибка |
|---|---|
| login/age/cookies | `LoginRequired` / `CookiesExpired` |
| 429/too many | `RateLimited` |
| geo | `GeoBlocked` |
| DRM | `DrmProtected` |
| removed/private | `Unavailable` |
| extractor/JSON | `ExtractorBroken` |
| нет resolver | `UnsupportedUrl` |

- Ошибки retryable: `RateLimited`, timeout, 5xx; `LoginRequired`/`DrmProtected` — нет.
- Cookies никогда не пишутся в `SourceSnapshot`, `Intake`, логи, sidecar.

### CLI / SSRF

- `trackforge plan <url>` — `PlanView` с форматами, без скачивания; `--json`.
- `trackforge add <url> [--auto] [--profile] [--mode] [--json] [--dry-run]` — plan + run; `--auto` выбирает best audio; интерактивный выбор — список форматов.
- `--offline` → `OfflineModeError` (exit 2), без вызова yt-dlp.
- SSRF guard: только http/https; resolve DNS и блок private/link-local/loopback по умолчанию; local разрешается только явной настройкой (не в v1); ограничить redirects (например, 5) и размер metadata.
- URL с userinfo/токенами не логируется целиком; redact query.
## Порядок работ

1. Реализуй `url_safety` и тесты.
2. Реализуй `YtDlpClient` с fake executable; JSON fixtures.
3. Реализуй `YtDlpSource`/`Candidate` mapping.
4. Реализуй `YtDlpAcquirer` + `SourceSnapshot`/`source.metadata.v1`.
5. Добавь error mapping и retryable.
6. Подключи `add`/`plan`, doctor checks.
7. Добавь тесты no-network; live — `@pytest.mark.network`.
8. Прогони quality gates; закоммить.
## Тесты

- **unit:** supports/SSRF; format selection; JSON parse; error mapping; cookie selection by domain; timeout; no shell injection (URL `; rm` остаётся аргументом).
- **contract:** `YtDlpClient` fake; `SourceResolver`/`Acquirer` протоколы; `source.metadata.v1` schema.
- **integration (fake binary):** `plan`/`add` на fixture JSON → CAS/Intake/Item; повторный `add` cache hit; cookies не в артефактах/логах.
- **live (`network`):** один URL вручную; не в CI.
- **fixtures:** `single_youtube.json`, `single_soundcloud.json`, fake `yt-dlp` executable, cookies temp file.
- **negative:** 403/login; 429; DRM; private IP; `--offline`; invalid JSON; missing binary; cookie file 0644; URL with playlist (reject until Plan 18).
## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/ytdlp tests/cli/test_add_url.py -q
uv run lint-imports
PATH="$PWD/tests/fakes:$PATH" uv run trackforge plan "https://example.test/watch?v=demo" --json
uv run trackforge doctor --json
```

Ожидается: no-network тесты зелёные; `plan`/`add` на fake binary работают; `doctor` видит yt-dlp/Deno; live-тест отдельным marker.
## Acceptance criteria

- [ ] `uv run pytest tests/adapters/ytdlp tests/cli/test_add_url.py -q` зелёный.
- [ ] `trackforge add <fixture-url>` с fake yt-dlp создаёт файл в библиотеке, `Intake` и `SourceSnapshot`.
- [ ] `trackforge plan <fixture-url>` не скачивает и показывает форматы.
- [ ] Audio-only выбор: `bestaudio`/opus/m4a; DRM отклоняется.
- [ ] Cookies выбираются по домену, не логируются, не попадают в артефакты; права 0600 проверяются.
- [ ] Ошибки login/rate-limit/geo/DRM/JSON понятны и типизированы.
- [ ] `--offline` не вызывает yt-dlp.
- [ ] `doctor` проверяет yt-dlp/Deno/cookies без значений.
- [ ] Live-тест помечен `network` и не входит в CI.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- `YtDlpClient`/`YtDlpSource`/`YtDlpAcquirer`.
- `source.snapshot.v1`/`source.metadata.v1`.
- Candidate/format selection, error mapping, cookies/Deno.
- CLI `add`/`plan` для URL; SSRF guard.
- Fake yt-dlp и JSON fixtures.

## Stop conditions / decision gates

- Plan 10 `No-go`/`Blocked` — стоп; Plan 11 `deferred`, локальный MVP остаётся.
- D7 не выбран — диапазон + lock + doctor warning; не пинь exact без решения.
- Нужны плейлисты — Plan 18.
- Нужен обход DRM/бот-чека — стоп; это non-goal.
- Cookie-файл с правами не 0600 — не используй; попроси исправить.
- Private IP/loopback URL — блокируй; не добавляй allowlist без явной настройки.
