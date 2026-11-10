# L5 — дополнительные источники

**Предусловие:** [L4_INGEST_HANDOFF.md](./L4_INGEST_HANDOFF.md)  
**Следующий чекпоинт:** [L6_HARDENING.md](./L6_HARDENING.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Основной конвейер (L0–L4) работает. Теперь — источники, которые не покрываются обычным `YtDlpLoader`:

- VK Music через API;
- Pinterest images/posts;
- прямые файлы/гиф/документы.

**Не менять ядро** без необходимости; новые источники должны реализовывать те же `MediaLoader`/`SourceInfo`.

---

## Цель

Добавить 2–3 источника, которые проверяют модель на:

- аудио без yt-dlp (VK API);
- изображения/гиф;
- posts с несколькими медиа;
- прямые URL.

---

## Задачи

### 1. `DirectAssetLoader`

`loaders/direct.py`:

- `supports_url` по расширениям (`.jpg`, `.png`, `.gif`, `.webp`, `.avif`, `.pdf`, `.zip`);
- `get_info` — HEAD/GET: `Content-Type`, `Content-Length`, `Content-Disposition`;
- `DownloadVariant(id="original", kind=IMAGE/DOCUMENT)`;
- `recommend(image=...)`/`document`;
- `download` — потоковое копирование с progress/range;
- no metadata tags; sidecar info optional.

Тесты: local HTTP server, content-type, redirects, size/range, progress.

### 2. `VkApiLoader`

`loaders/vk.py`:

- URL: `/audio...`, `/music/playlist/...`, `/music/album/...`; **не** `/wall` (это yt-dlp);
- `is_ready` = token;
- `VkAudioClient`:
  - `audio.getById`, `audio.get`, `audio.search`;
  - token, proxy, rate limit;
  - IP-bound URLs — скачивать сразу, не кэшировать;
- map audio object → `MediaItem`:
  - id/owner_id, artist, title, duration, cover_url, lyrics (plain);
  - one `DownloadVariant` (m4a/mp3);
- `recommend` — лучшее аудио;
- `download` — `HttpStreamDownloader`, progress/cancel;
- `search` — `audio.search`, если доступен; иначе `SearchNotSupported`.

Тесты: fake client, URL mapping, token missing, IP-bound URL no cache, progress.

### 3. Pinterest (images/posts)

`loaders/pinterest.py`:

- video pins — через `YtDlpLoader`;
- image-only — через Pinterest API/`PinResource`/`BoardFeed`;
- `SourceInfo.kind=POST` для pin; `items` с `MediaKind.IMAGE`;
- board — collection;
- `choose_thumbnail` — largest image;
- `download` — direct image stream;
- no tags; sidecar info optional.

Тесты: image-only pin, video pin, board, thumbnail selection.

### 4. Posts

- `SourceInfo.kind=POST`;
- `description` — текст;
- `items` — медиа с общим `group_id`;
- download all items by default; `--items` selection — L3 уже есть.
- не делать «соцсеть»; только посты, которые нужны.

### 5. Тесты

- `test_direct_asset.py`;
- `test_vk_api.py`;
- `test_pinterest.py`;
- `test_post_model.py`;
- `test_l5_progress.py`.

---

## Ручная проверка на реальных данных

1. Direct image:
   - `tfg https://.../cover.jpg` → файл + thumbnail info.
2. VK:
   - с токеном: audio track/playlist;
   - без токена: `AuthRequired`, не fallback в yt-dlp для музыки.
3. Pinterest:
   - image pin → `kind=post`, image item;
   - video pin → video variants;
   - board → collection.
4. Проверить, что L0–L4 не сломались.

---

## Критерии готовности

- [ ] DirectAssetLoader работает.
- [ ] VkApiLoader работает с токеном; без токена — typed error.
- [ ] Pinterest image/video/board.
- [ ] POST-модель с несколькими items.
- [ ] Прогресс/отмена для direct/VK.
- [ ] Существующие тесты зелёные.
- [ ] ручной smoke VK/Pinterest/direct.

---

## Что не делать

- Не переписывать ядро.
- Не добавлять БД.
- Не делать полноценный Pinterest scraper.
- Не использовать VK API для видео/стен (yt-dlp).
- Не логировать токены.

---

## Передача дальше

Следующий чат получает:

- Direct/VK/Pinterest loaders;
- POST/images модель;
- тесты/ограничения.

Дальше — [L6_HARDENING.md](./L6_HARDENING.md).
