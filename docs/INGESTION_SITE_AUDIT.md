# Аудит сайтов L1.4 — Vimeo, Bandcamp, Pinterest, VK

**Статус:** завершён; `SourceInfo v1` frozen (2026-11-10)

**Дата:** 2026-11-10  
**Проверено на:** `yt-dlp 2026.10.26`, Python `3.14.9`  
**Общий контекст:** [INGESTION_ARCHITECTURE.md](./INGESTION_ARCHITECTURE.md), [INGESTION_RESEARCH.md](./INGESTION_RESEARCH.md)  
**Следующий чекпоинт:** [plan/L2_DOWNLOAD.md](./plan/L2_DOWNLOAD.md)

---

## 1. Что проверялось

Цель L1.4 — не «поддержать всё», а понять, что реально отдают Vimeo,
Bandcamp, Pinterest и VK, и чего не хватает модели `SourceInfo`.

Проверено:

- Vimeo: одиночное видео, альбом/showcase, flat/full;
- Bandcamp: трек, альбом, форматы, поля альбома;
- Pinterest: image-only pin, video pin, доска (по данным экстрактора);
- VK: wall post, direct video, аудио-вложения (по форме `VKWallPostIE`).

**Не проверялось и не делалось в L1.4:** скачивание медиа, полноценные
`PinterestLoader`/`VkApiLoader`, поиск. `SourceInfo v1` зафиксирован
отдельным чекпоинтом L1.5 (см. §10).

---

## 2. Решения по L1.4

1. **Vimeo и Bandcamp остаются на generic `YtDlpLoader`.** Отдельные
   классы не нужны: URL-паттерны, приоритеты и нормализация не требуются,
   а `formats`/`thumbnails`/`playlist` уже нормализуются mapper'ом.
2. **Для Vimeo нужны cookies для обычных `vimeo.com/<id>`.** На проверенной
   версии yt-dlp web-клиент требует логин. Публичный
   `player.vimeo.com/video/<id>` работает без cookies; альбом можно
   перечислить flat без cookies.
3. **Pinterest и VK — только probes.** Отдельных загрузчиков нет.
   Image-only pin и wall post выявили пробелы модели, которые закрываются
   в L5 (images/POST), а не в L1.4.
4. **`SourceInfo` не фиксируется.** Новые поля «на всякий случай» не
   добавлялись.
5. Mapper стал терпимее к трём реальным особенностям сайтов:
   - audio-only HLS/DASH без `acodec` распознаётся по `audio` в
     `format_id`/`format_note` (Vimeo, VK);
   - flat-элемент с `_type=url` больше не превращается в «direct video»
     из-за одного `url`;
   - элемент коллекции с готовыми `formats`, но без собственного page URL,
     получает URL родительского поста (VK audio attachment).
6. Транспортные поля (`cookies`, `http_headers`, `manifest_url`,
   `requested_formats`) не попадают в `extras`.

---

## 3. Матрица «сайт × URL-тип × результат»

| Сайт | URL-тип | `SourceInfo.kind` | `items` | `variants` | `thumbnail` | auth | Ограничения |
|---|---|---|---|---|---|---|---|
| YouTube | `/watch?v=`, `youtu.be` | `single` | 1 | video/audio, progressive + video-only + audio-only | да | cookies нужны для YouTube-профиля | уже покрыто L1.1–L1.2 |
| YouTube | `/playlist?list=` | `collection` | flat/full | flat — нет; full — у выбранного | да | cookies | уже покрыто L1.2 |
| Yandex Music | `/album/.../track/...` | `single` | 1 | audio (FLAC/MP3) | да, `orig` | cookies + плагин | уже покрыто L1.3 |
| Yandex Music | `/album/...`, `/artist/...` | `collection` | flat | flat — нет | да | cookies + плагин | `smart_preview` проверяется отдельно |
| SoundCloud | `/artist/track` | `single` | 1 | audio (progressive/HLS) | да | обычно нет | уже покрыто L1.3 |
| SoundCloud | `/artist/sets/...` | `collection` | flat/full | flat — нет | да | обычно нет | уже покрыто L1.3 |
| **Vimeo** | `player.vimeo.com/video/<id>` | `single` | 1 | HLS video-only + HLS audio-only, нужен merge | да, CDN без размеров | **нет** | нет progressive; `release_date` в player config отсутствует |
| **Vimeo** | `vimeo.com/<id>` | — | — | — | — | **cookies обязательны** | `AuthRequired`: «web client only works when logged-in» |
| **Vimeo** | `/album/<id>`, `/showcase/<id>` | `collection` | flat | flat — нет | обычно нет на альбоме | альбом — часто без cookies; showcase может дать 403 | full-resolve элементов требует cookies (или player URL) |
| **Bandcamp** | `<artist>.bandcamp.com/track/<slug>` | `single` | 1 | mp3/aac/flac/wav/aiff/ogg, progressive | да, `f4.bcbits.com` | нет | `title` содержит `Artist - Track`, `track` — чистое название |
| **Bandcamp** | `<artist>.bandcamp.com/album/<slug>` | `collection` | flat/full | audio у элементов | на альбоме часто нет; у треков есть | нет | top-level thumbnail отсутствует; `track_number` уходит в `extras` |
| **Pinterest** | `/pin/<id>` (image-only) | `single` (не `post`) | 1 | **нет** | да, `images` → `thumbnails` | публичный pin — без cookies | `NoFormats` в generic; картинка не становится `MediaKind.IMAGE` |
| **Pinterest** | `/pin/<id>` (video) | `single` (не `post`) | 1 | HLS + progressive | да | публичный pin — без cookies | video pin работает, но `kind=POST` не выставляется |
| **Pinterest** | `/<user>/<board>/` | `collection` | flat | flat — нет | да | доска может требовать cookies | image-only pins теряются; видео-элементы неполные |
| **VK** | `vk.com/wall-..._...` | `collection` (не `post`) | 0 в live-проверке | — | — | публичная стена/пост может требовать логин | `VKWallPostIE` вернул `_type=playlist`, но 0 entries; аудио/видео attachments не извлеклись |
| **VK** | `vk.com/video-..._...` | `single` | 1 | DASH/HLS/progressive, video-only + audio-only | да, `userapi.com` | публичное видео — без cookies | много дублей HLS/DASH; нужен merge |
| **VK** | `/audio...`, `/music/playlist/...` | — | — | — | — | токен (L5) | yt-dlp не покрывает; нужен `VkApiLoader` в L5 |

---

## 4. Матрица полей

Обозначения: `✓` — поле стабильно есть; `~` — есть не всегда/зависит от
типа; `—` — нет или не используется.

| Поле yt-dlp | YouTube | Yandex | SoundCloud | Vimeo | Bandcamp | Pinterest | VK | Куда попадает |
|---|---|---|---|---|---|---|---|---|
| `_type` (`video`/`playlist`/`multi_video`) | ✓ | ✓ | ✓ | ✓ | ✓ | ~ (часто `video` даже у image pin) | ✓ | `SourceInfo.kind` |
| `id` / `display_id` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | `SourceInfo.id`, `MediaItem.id` |
| `title` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | `SourceInfo.title`, `MediaItem.title` |
| `track` | — | ~ | — | — | ✓ | — | ~ (audio attachment) | `MediaItem.title` (приоритетнее `title`) |
| `description` / post text | ✓ | ✓ | ✓ | ~ | ✓ (album) | ✓ | ~ (wall text) | `SourceInfo.description` |
| `uploader` / `channel` / `creator` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ~ (пустой у части видео) | `SourceInfo.creator`, `MediaItem.creator` |
| `artists` / `artist` | ~ | ~ | — | — | ✓ | — | ~ (audio) | `MediaItem.artists` |
| `album` / `album_artists` | — | ✓ | — | — | ✓ | — | — | `MediaItem.album`, `album_artists` |
| `track_number` / `disc_number` | — | ~ | — | — | ✓ | — | — | **`MediaItem.extras`** |
| `duration` | ✓ | ✓ | ✓ | ✓ | ✓ | ~ (video pin) | ✓ | `MediaItem.duration` |
| `release_date` / `timestamp` | ✓ | ~ (`date`) | — | — (player; web не проверен) | ✓ | ✓ | ✓ (`upload_date`) | `MediaItem.release_date` |
| `thumbnails` / `thumbnail` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | `Thumbnail`, `MediaItem.thumbnail` |
| `formats` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (video) / — (image) | ✓ | `DownloadVariant` |
| `subtitles` / `automatic_captions` | ✓ | ~ | — | ~ | — | — | ~ | `SubtitleTrack` |
| `tags` / `categories` | ✓ | ~ | — | — | ✓ | ✓ | — | **`extras`** |
| `view_count` / `like_count` / comments / reposts | ✓ | ~ | ~ | ~ | — | ✓ | ✓ | **`extras`** |
| `coverUrl` / `cover_url` (VK audio) | — | — | — | — | — | — | ~ | `thumbnails` (probe) |
| `images` (Pinterest PinResource) | — | — | — | — | — | — (yt-dlp кладёт их в `thumbnails`) | — | **`extras`/L5** |
| `is_live` / `live_status` | ✓ | — | — | ~ | — | — | ~ | `MediaItem.live_status` |
| `quality` (Yandex `smart_preview`) | — | ✓ | — | — | — | — | — | `extras`, проверка L1.3 |

### 4.1. Поля, которые есть почти у всех

- `_type`, `id`, `title`, `webpage_url`, `thumbnails`, `formats`;
- `duration` — у всего, кроме image-only Pinterest pin;
- `uploader`/`creator` — кроме случаев, где сайт отдаёт только
  `uploader_id` (Bandcamp album, часть VK);
- `description` — у видео/альбомов/постов, но не у всех треков;
- `timestamp`/`upload_date`/`release_date` — в разных комбинациях.

### 4.2. Поля, характерные для одного сайта

- **Vimeo:** web/API может отдавать `player_embed_url`, `release_timestamp`;
  в player config их не было. Отдельные HLS audio-only renditions без
  `acodec`; в player config нет `upload_date`.
- **Bandcamp:** `track_id`, `track_number`, `album_artist`,
  `album_artists`, `tags`, `freeDownloadPage`; `title` = `Artist - Track`.
- **Pinterest:** `categories`, `repost_count`, `comment_count`,
  `pin_join`, `hashtags`; `images` доступны только через PinResource,
  yt-dlp кладёт их в `thumbnails`.
- **VK:** `is_live`, `view_count`, `like_count`, `coverUrl`,
  `owner_id`/`audio.id`; ссылки на аудио привязаны к IP и времени.

### 4.3. Чего не хватает в модели

Это **не** повод добавлять поля сейчас; список для L1.5/L5.

1. **`SourceKind.POST` не производится mapper'ом.** VK wall и Pinterest pin
   приходят как `collection`/`single`. Нужен post-aware loader (L5),
   который выставит `kind=post`, `description` — текст поста, у элементов
   будет общий `group_id`.
2. **Image-only pin не имеет media-item.** `MediaKind.IMAGE` и
   `StreamRole.IMAGE` уже есть, но у pin нет `DownloadVariant`; картинка
   живёт только в `thumbnails`. Нужен image variant (L5).
3. **`track_number`, `disc_number`, `track_total`, `genre`, `lyrics`,
   `tags`, `categories`** — полезные метаданные, но для выбора/скачивания
   не нужны. Пока остаются в `extras`.
4. **`publisher`/label** есть только на уровне `SourceInfo` и не выводится
   из Bandcamp/VK однозначно. `uploader != publisher`; не подменять.
5. **IP-bound URL VK audio.** `StreamInfo.url` хранит URL, но в модели нет
   признака «не кэшировать/не передавать другому клиенту». Это правило
   download-слоя L5, не поле модели.
6. **Размеры thumbnail у Vimeo/Bandcamp/VK часто отсутствуют.** Модель
   допускает `width=None`, но выбрать лучший вариант по площади нельзя.
   В L1 это допустимо; HEAD/проверка размеров — отдельная задача.
7. **Pinterest `images` и VK `coverUrl`** не являются отдельными медиа;
   для них нужен site-specific loader, а не расширение generic mapper.

---

## 5. Форматы и `MediaKind`

| Сайт | Что в `formats` | `protocol` | Итоговый `MediaKind` | Merge/ffmpeg | Замечание |
|---|---|---|---|---|---|
| Vimeo video | HLS video-only + HLS audio-only | `m3u8_native` | `audio_video` (после pairing), `audio` | да | у audio-only может не быть `acodec`; исправлено эвристикой `audio` в id/note |
| Vimeo album | нет у flat-элементов | — | `unknown` (flat) | нет | full-resolve требует cookies/player |
| Bandcamp track | progressive mp3/m4a/ogg/flac/wav/aiff | `https` | `audio` | нет | несколько codec-вариантов, dedupe по codec/ext |
| Bandcamp album | progressive audio у элементов | `https` | `audio` | нет | flat — без `formats` |
| Pinterest image pin | `formats=[]` | — | `unknown` | — | изображение только в `thumbnails` |
| Pinterest video pin | HLS `V_*` + progressive | `m3u8_native`/`https` | `audio_video` | ~ | некоторые HLS-варианты без явных codec |
| VK wall (fixture) | audio m4a + video DASH/HLS | `https`/`m3u8_native` | `audio`, `audio_video` | да для видео | live-экстрактор сейчас отдаёт 0 entries |
| VK direct video | DASH/HLS/progressive, video-only + audio-only | `https`/`m3u8_native` | `audio_video` | да | много дублей HLS/DASH |
| Pinterest/VK image | — | — | `image` (L5) | — | `MediaKind.IMAGE` пока не производится |

**Какие `MediaKind` реально нужны сейчас:** `AUDIO`, `VIDEO`,
`AUDIO_VIDEO`, `UNKNOWN`. `IMAGE`/`ANIMATED_IMAGE` нужны для Pinterest и
direct assets в L5. `DOCUMENT` пока не нужен.

---

## 6. Что уходит в `extras`

Mapper складывает туда всё, что не является публичным полем модели:

- `tags`, `categories`, `hashtags`;
- `track_number`, `track_total`, `disc_number`, `track_id`, `genre`;
- `view_count`, `like_count`, `comment_count`, `repost_count`;
- `uploader_id`, `uploader_url`, `webpage_url_domain`, `extractor`,
  `extractor_key`, `quality`, `date` (Yandex);
- site-specific объекты (`images`, `coverUrl`, `pin_join`), если их добавит
  будущий loader.

Не попадают в `extras`:

- `cookies`, `http_headers`, `manifest_url`, `requested_formats`,
  `fragments`, `requested_subtitles`;
- подписанные/временные media URL (они остаются только в
  `StreamInfo.url`/`DownloadVariant` и не сериализуются в inspect JSON);
- `_`-поля yt-dlp.

---

## 7. Fixtures

Все fixtures лежат в `tests/acquire/fixtures/`, sanitized: без cookies,
токенов, `http_headers` и подписанных CDN-URL. Media-URL заменены на
`media.example.test`; публичные thumbnail-URL оставлены как есть.

| Fixture | Источник | Что сохранено | Mapper |
|---|---|---|---|
| `vimeo_video.info.json` | `player.vimeo.com/video/76979871` | id/title/uploader/duration, 5 HLS formats (2 audio-only + 3 video-only), thumbnail | `single`, `audio_video`, audio+video variants |
| `vimeo_playlist.info.json` | `vimeo.com/album/2632481` (flat) | album title, 5 flat entries (`_type=url`, id, url) | `collection`, flat items |
| `bandcamp_track.info.json` | `benprunty.bandcamp.com/track/lanius-battle` | track/album/artists, `track_number`, dates, tags, 4 audio formats | `single`, `audio` |
| `bandcamp_album.info.json` | `c418.bandcamp.com/album/minecraft-volume-alpha` | album description, 3 resolved track entries с форматами и thumbnails | `collection`, `audio` items |
| `pinterest_pin.info.json` | image-only pin `48976714684296474` | title/description/uploader, 9 thumbnails, `formats=[]` | `single`, `unknown`, без variants |
| `vk_wall.info.json` | probe на основе реального VK video + формы `VKWallPostIE` | `_type=playlist`, 1 video attachment, 1 audio attachment (audio без своего page URL) | `collection`, `audio_video` + `audio` |

`vk_wall.info.json` — **нормализованный probe**, а не дословный текущий
ответ yt-dlp: live-экстрактор на проверенной версии вернул `_type=playlist`
с нулём entries, поэтому форма аудио-вложения восстановлена по
`VKWallPostIE`, а видео-вложение взято из реального direct video.
Это отмечено, чтобы fixture не выглядел «живым» ответом, которого нет.

---

## 8. Ручная проверка на реальных данных

Выполнялось через `AcquisitionService.inspect` (или прямой `yt_dlp` там,
где generic падает). Медиа не скачивалось; запросы только к страницам,
API и манифестам.

| Сайт | URL | Режим | Что пришло |
|---|---|---|---|
| Vimeo | `https://vimeo.com/76979871` | `inspect(flat=True)` | `AuthRequired`: web-клиент требует cookies |
| Vimeo | `https://player.vimeo.com/video/76979871` | `inspect()` | `single`, title, thumbnail; variants `720p-mp4`, `540p-mp4`, `360p-mp4`, `270p-mp4`, `audio-mp4` |
| Vimeo | `https://vimeo.com/album/2632481` | `inspect(flat=True, limit=3)` | `collection`, 3 flat items, source thumbnail `None` |
| Vimeo | `https://vimeo.com/showcase/11803104` | `inspect(flat=True)` | `AuthRequired` (403 на album API) |
| Bandcamp | `https://benprunty.bandcamp.com/track/lanius-battle` | `inspect()` | `single`, `audio`; mp3/aiff/flac/m4a/ogg/wav variants; thumbnail |
| Bandcamp | `https://c418.bandcamp.com/album/minecraft-volume-alpha` | `inspect(flat=True, limit=3)` | `collection`, 3 flat items, source thumbnail `None` |
| Pinterest | image pin `48976714684296474` | `inspect()` | `NoFormats` — generic не умеет image-only pin |
| Pinterest | image pin `48976714684296474` | `yt_dlp` + `ignore_no_formats_error=True` → mapper | `_type=video`, `formats=[]`, 9 thumbnails; mapper: `single`, item `unknown`, variants `0`, best thumbnail 800×1600 |
| Pinterest | video pin `664281013778109217` | `inspect()` | `single`, `audio_video`, video variants; **не** `post` |
| VK | `https://vk.com/wall-8881618_469114` | `inspect()` | `Unavailable`: `_type=playlist`, 0 entries |
| VK | `https://vk.com/video-2000843141_119843141` | `inspect()` | `single`, `audio_video`, DASH/HLS variants; ошибок нет |

Ошибки, которые стоит помнить:

- `NoFormats` (Pinterest image-only) — не «сайт сломан», а отсутствие
  image-variant в generic-модели;
- `AuthRequired` (Vimeo web/showcase) — следствие web-клиента yt-dlp,
  а не приватности конкретного видео;
- `Unavailable` (VK wall) — текущий `VKWallPostIE` не разбирает
  современную разметку; direct video при этом работает.

---

## 9. Тесты

Добавлены:

- `test_site_fixtures.py` — все шесть fixtures проходят `YtDlpMapper`
  без исключений; проверяется отсутствие cookies/headers в данных;
- `test_vimeo.py` — single/collection, pairing HLS audio+video,
  flat-элементы не становятся direct variants, generic registry;
- `test_bandcamp.py` — track/album, поля альбома, `track_number` в
  `extras`, flat-режим, generic registry;
- `test_probe_kinds.py` — Pinterest image/video pin и VK wall/multi_video:
  ожидаемые `kind`/`resolution` и зафиксированные пробелы (`POST`,
  `IMAGE`);
- `test_unknown_fields.py` — неизвестные/битые поля, thumbnail'ы, числа,
  `_type`, транспортные поля не роняют mapper.

Для L1.5 добавлены:

- `test_inspect_service.py` — single/collection/flat/limit/source,
  типизированные ошибки и отсутствие download;
- `test_inspect_json.py` — схема v1, `schema_version`, flat-состояние и
  отсутствие секретов/сырых полей;
- `test_inspect_cli.py` — `CliRunner`, `--json`/`--flat`/`--limit`/
  `--source`, коды выхода 0/1/2;
- `test_model_freeze.py` — имена/наборы полей модели и JSON v1, все
  fixtures в flat/full, неизвестные поля только в `extras`.

Полный прогон:

```text
uv run pytest -q
714 passed
```

---

## 10. `SourceInfo v1` frozen

**Дата фиксации:** 2026-11-10.  **Версия:** `schema_version: 1`.

Решение: `SourceInfo` и публичный JSON-контракт `inspect` считаются
зафиксированными. L2 может использовать модель и `variant.id` без ожидания
дополнительных полей.

Что вошло в v1:

- модель: `SourceInfo`, `MediaItem`, `DownloadVariant`, `Thumbnail`,
  `Recommendations`, `StreamInfo` — наборы полей закреплены
  `test_model_freeze.py`;
- сериализатор: `trackforge.acquire.serialization`
  (`source_info_to_dict`/`source_info_to_json`), плюс
  `SourceInfo.to_dict()`/`to_json()` и `schema_version`;
- JSON: `schema_version`, `source`, `kind`, `title`, `creator`, `thumbnail`,
  `items`, `warnings`; у item — `id`, `title`, `page_url`, `resolution`,
  `thumbnail`, `recommendations`, `variants`; у variant — `id`, `label`,
  `kind`, `container`, `height`, `filesize`, `requires_merge`;
- flat: `resolution="flat"`, `variants=[]`;
- в JSON нет `cookies`, `http_headers`, токенов, proxy, `extras`,
  `subtitles`, `StreamInfo.url`, селекторов и сырого `info_dict`;
- API: `AcquisitionService.inspect(url, flat=..., limit=..., source=...)`
  возвращает `SourceInfo` и не скачивает медиа;
- CLI: `tfg inspect URL [--json] [--flat] [--limit N] [--source NAME]`,
  коды выхода 0/1/2, без интерактивных вопросов.

Правило расширения: новые source-specific поля — только в
`MediaItem.extras`; публичное поле в JSON — только с новой
`schema_version`.  `POST` и `IMAGE`-варианты (Pinterest/VK) остаются
задачами L5 и не меняют v1.

Для L5:

- Pinterest: `kind=post`, image variant (`MediaKind.IMAGE`), board —
  collection; image-only pin сейчас не скачивается;
- VK Music: `VkApiLoader`, IP-bound audio URL, lyrics/cover;
- VK wall: либо починить/заменить `VKWallPostIE`, либо классифицировать
  wall как `POST` в post-aware loader'е; текущий результат — пустая
  коллекция;
- при добавлении `POST` проверить `group_id` и `description` на нескольких
  медиа в одном посте.

`SourceInfo v1` **зафиксирован**; следующие изменения публичной схемы —
только через новую версию.
