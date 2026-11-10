# План по чекпоинтам

**Статус:** рабочая структура  
**Дата:** 2026-11-09  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Как этим пользоваться

Один файл = один новый контекст LLM = один проверяемый чекпоинт.

Правила:

1. Дать LLM **только один** файл (`L0_...`, `L1_0_...` и т.д.) + попросить прочитать общий контекст по ссылкам.
2. Реализовать **только этот** слой. Не начинать следующий, даже если «очевидно».
3. Прогнать unit/integration-тесты.
4. Прогнать **ручную проверку на реальных данных** из файла.
5. Заполнить чек-лист готовности.
6. Только после полного прохождения — переходить к следующему файлу.

Если ручная проверка выявила проблему в модели данных — исправлять в текущем чекпоинте, а не «потом».

---

## Последовательность

| № | Файл | Что закрывает | Следующий |
|---|---|---|---|
| 0 | [L0_FOUNDATION.md](./L0_FOUNDATION.md) | модели, конфиг, registry, staging, fake loader | L1.0 |
| 1 | [L1_0_AUTH.md](./L1_0_AUTH.md) | cookies, proxy, doctor — gate до YouTube | L1.1 |
| 2 | [L1_1_YTDLP_SINGLE.md](./L1_1_YTDLP_SINGLE.md) | generic + YouTube single `inspect` | L1.2 |
| 3 | [L1_2_PLAYLISTS.md](./L1_2_PLAYLISTS.md) | плейлисты flat/full | L1.3 |
| 4 | [L1_3_YANDEX_SOUNDCLOUD.md](./L1_3_YANDEX_SOUNDCLOUD.md) | Yandex Music + плагин, SoundCloud | L1.4 |
| 5 | [L1_4_SITE_AUDIT.md](./L1_4_SITE_AUDIT.md) | Vimeo/Bandcamp + probes, аудит полей | L1.5 |
| 6 | [L1_5_INSPECT_FREEZE.md](./L1_5_INSPECT_FREEZE.md) | `inspect` CLI/API и `SourceInfo` v1 | L2 |
| 7 | [L2_DOWNLOAD.md](./L2_DOWNLOAD.md) | скачивание, staging, progress/cancel | L3 |
| 8 | [L3_SELECTION_CLI.md](./L3_SELECTION_CLI.md) | пользовательский выбор, CLI, поиск | L4 |
| 9 | [L4_INGEST_HANDOFF.md](./L4_INGEST_HANDOFF.md) | финальные имена, sidecars, metadata handoff | L5 |
| 10 | [L5_EXTRA_SOURCES.md](./L5_EXTRA_SOURCES.md) | VK API, Pinterest/images, direct assets | L6 |
| 11 | [L6_HARDENING.md](./L6_HARDENING.md) | doctor, concurrency, security, release | — |

---

## Инварианты, общие для всех чекпоинтов

- **yt-dlp — библиотека**, не subprocess.
- **Никакой БД**, истории, download archive, библиотеки.
- **L1 не скачивает медиа** (страницы/манифесты — можно).
- **L2 не пишет теги**, не делает финальное имя и ingest.
- **L3** — первое место, где появляется пользовательский выбор.
- **L4** — единственный слой, который пишет теги/финальные имена/sidecars.
- **Cookies/proxy — per-source**, не глобально.
- **До первого запроса к YouTube** должны работать cookies/proxy и `doctor`.
- **Yandex Music** — только с `yt-dlp-yandex-music`; без плагина не готов.
- **EJS + Deno** — Python-зависимости; системный Deno не обязателен.
- **Staging/final names** — плоские по умолчанию; коллизии `1/`, `2/`, не `(1)`.
- **Loader сам** делает `search`, `choose_thumbnail`, `recommend`.
- **Registry сам** перебирает loaders и подставляет `SourceProfile` по `config_key`.

---

## Что считается «проверено»

- `uv run pytest -q` — зелёный.
- Ручной smoke из файла — выполнен на реальных данных.
- Ошибки/ограничения записаны в конце файла или в `docs/INGESTION_SITE_AUDIT.md`.
- Следующий чекпоинт не начат до этого.

---

## Прогресс

| Чекпоинт | Статус | Дата | Заметки |
|---|---|---|---|
| L0 | не начат | | |
| L1.0 | не начат | | |
| L1.1 | не начат | | |
| L1.2 | не начат | | |
| L1.3 | не начат | | |
| L1.4 | не начат | | |
| L1.5 | не начат | | |
| L2 | не начат | | |
| L3 | не начат | | |
| L4 | не начат | | |
| L5 | не начат | | |
| L6 | не начат | | |
