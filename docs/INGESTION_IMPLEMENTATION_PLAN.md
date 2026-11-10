# План реализации: получение данных и скачивание

**Статус:** план разбит на чекпоинты  
**Дата:** 2026-11-09  
**Связанные документы:** [IDEA.md](./IDEA.md), [INGESTION_RESEARCH.md](./INGESTION_RESEARCH.md), [INGESTION_ARCHITECTURE.md](./INGESTION_ARCHITECTURE.md), [plan/README.md](./plan/README.md)

---

## Главное

План разбит на самодостаточные файлы в [plan/](./plan/). Один файл = один новый контекст LLM = один ручной smoke-тест = один gate. Следующий чекпоинт не начинается, пока текущий не проверен на реальных данных.

Порядок:

```text
L0 каркас
→ L1.0 cookies/proxy/doctor
→ L1.1 YouTube single inspect
→ L1.2 плейлисты
→ L1.3 Yandex Music + SoundCloud
→ L1.4 Vimeo/Bandcamp + Pinterest/VK probes
→ L1.5 inspect + SourceInfo v1 freeze
→ L2 download
→ L3 выбор/CLI
→ L4 ingest handoff
→ L5 VK API / Pinterest / direct assets
→ L6 hardening
```

**L2 не начинается, пока L1.5 не зафиксировал `SourceInfo` v1.**

---

## Чекпоинты

| № | Файл | Gate |
|---|---|---|
| 0 | [plan/L0_FOUNDATION.md](./plan/L0_FOUNDATION.md) | модели/registry/staging, без сети |
| 1 | [plan/L1_0_AUTH.md](./plan/L1_0_AUTH.md) | cookies/proxy/doctor до первого YouTube-запроса |
| 2 | [plan/L1_1_YTDLP_SINGLE.md](./plan/L1_1_YTDLP_SINGLE.md) | YouTube single `inspect` |
| 3 | [plan/L1_2_PLAYLISTS.md](./plan/L1_2_PLAYLISTS.md) | YouTube playlist flat/full |
| 4 | [plan/L1_3_YANDEX_SOUNDCLOUD.md](./plan/L1_3_YANDEX_SOUNDCLOUD.md) | Yandex plugin + SoundCloud |
| 5 | [plan/L1_4_SITE_AUDIT.md](./plan/L1_4_SITE_AUDIT.md) | Vimeo/Bandcamp + probes + audit |
| 6 | [plan/L1_5_INSPECT_FREEZE.md](./plan/L1_5_INSPECT_FREEZE.md) | `inspect` + `SourceInfo v1` |
| 7 | [plan/L2_DOWNLOAD.md](./plan/L2_DOWNLOAD.md) | download/staging/progress/cancel |
| 8 | [plan/L3_SELECTION_CLI.md](./plan/L3_SELECTION_CLI.md) | выбор/CLI/поиск |
| 9 | [plan/L4_INGEST_HANDOFF.md](./plan/L4_INGEST_HANDOFF.md) | финальные имена/sidecars/теги |
| 10 | [plan/L5_EXTRA_SOURCES.md](./plan/L5_EXTRA_SOURCES.md) | VK/Pinterest/direct |
| 11 | [plan/L6_HARDENING.md](./plan/L6_HARDENING.md) | doctor/security/release |

---

## Жёсткие правила

- yt-dlp — библиотека, не subprocess.
- Никакой БД/истории/download archive.
- L1 не скачивает медиа.
- L2 не пишет теги и не делает финальное имя.
- L3 — первое место пользовательского выбора.
- L4 — единственный писатель тегов/финальных имён/sidecars.
- Cookies/proxy — per-source, не глобально.
- До первого запроса к YouTube обязательны cookies/proxy и `doctor`.
- Yandex Music — только с `yt-dlp-yandex-music`.
- `yt-dlp-ejs` + Python-пакет `deno` — в зависимостях; системный Deno не обязателен.
- Staging/final names — плоские по умолчанию; коллизии `1/`, `2/`, не `(1)`.
- Поиск/thumbnail/recommendations — методы `MediaLoader`.
- Registry сам перебирает loaders и подставляет `SourceProfile` по `config_key`.

---

## Первый шаг

Начать с [plan/L0_FOUNDATION.md](./plan/L0_FOUNDATION.md). Не переходить к [plan/L1_0_AUTH.md](./plan/L1_0_AUTH.md), пока L0 не пройден полностью.
