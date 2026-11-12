# L6 — hardening

**Предусловие:** [L5_EXTRA_SOURCES.md](./L5_EXTRA_SOURCES.md)  
**Следующий чекпоинт:** нет (релиз)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Все основные источники и конвейер работают. Теперь — стабильность, безопасность, диагностика и упаковка. Новых фич не добавляем.

---

## Цель

Сделать инструмент предсказуемым:

- `doctor` полный;
- конкурентность и лимиты;
- retries/429;
- redaction/секреты;
- JSON schema;
- Windows;
- packaging/release.

---

## Задачи

### 1. `doctor`

Проверки:

- `yt-dlp` версия;
- `yt_dlp_ejs` + `deno.find_deno_bin()`;
- `yt-dlp-yandex-music` модуль;
- `ffmpeg`/`ffprobe`;
- cookies: формат/права/срок (если можно);
- proxy: URL/доступность (`--check-network`);
- config: пути/секреты;
- source-specific: YouTube cookies, Yandex cookies, VK token.

Вывод: `OK/WARN/FAIL`, без секретов.

### 2. Retry/rate limit

- network/429/5xx — retryable с backoff+jitter;
- auth/geo/DRM/unsupported — no retry;
- `Retry-After` для 429;
- per-source semaphore;
- `--jobs` (1 по умолчанию), cookies copy per job;
- `concurrent_fragment_downloads`.

### 3. Security

- URL schemes только http/https; `file://` — явный флаг;
- path traversal в `suggested_stem`/title/template;
- cookie 0600;
- redaction logs/JSON/repr;
- proxy credentials;
- plugins: только доверенные, pin;
- no shell.

### 4. JSON schema

- `schema_version: 2`;
- `inspect`/`download`/`doctor` JSON;
- события progress — JSONL;
- стабильные поля;
- отсутствие `raw`/headers/tokens;
- тест на отсутствие секретов.

### 5. Windows

- sanitize reserved names;
- max path;
- `os.replace`/cross-device;
- Unicode NFC;
- тесты с Windows-путями (без реального Windows).

### 6. Packaging

- `pyproject` deps актуальны;
- `uv.lock`;
- entry point `tfg`;
- optional extras (если решим);
- README/docs;
- changelog/version.

### 7. Тесты

- `test_doctor.py`;
- `test_retry.py` (fake transport);
- `test_security_redaction.py`;
- `test_path_traversal.py`;
- `test_json_schema.py`;
- `test_windows_paths.py`;
- `test_concurrency.py`.

---

## Ручная проверка на реальных данных

1. `tfg doctor` — все источники.
2. `tfg doctor --source youtube --check-network`.
3. YouTube + Yandex + SoundCloud + VK/Pinterest — по одному сценарию.
4. Отмена на большом файле.
5. Плейлист с ошибкой одного item.
6. Проверить, что секреты не в `--json`/логах.
7. Повторный запуск не ломает имена.

---

## Критерии готовности

- [ ] doctor полный.
- [ ] retry/429.
- [ ] concurrency/cookies per job.
- [ ] security tests.
- [ ] JSON schema.
- [ ] Windows sanitize.
- [ ] package/entry point.
- [ ] `uv run pytest -q` зелёный.
- [ ] ручной smoke всех источников.

---

## Что не делать

- Не добавлять новые источники.
- Не делать БД.
- Не менять модель без версии.
- Не логировать секреты.

---

## Передача дальше

Релиз. Дальше — AcoustID/MusicBrainz identification как отдельный проект/слой.
