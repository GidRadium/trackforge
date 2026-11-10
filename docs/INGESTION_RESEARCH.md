# Загрузка медиа: исследование yt-dlp, источников и ограничений

**Статус:** исследование для проекта TrackForge  
**Дата:** 2026-11-09  
**Проверяемая версия yt-dlp:** `2026.10.21` (исследование); в `pyproject.toml` после lock — `2026.10.26`  
**Связанные документы:** [IDEA.md](./IDEA.md), [METADATA_RESEARCH.md](./METADATA_RESEARCH.md), [INGESTION_ARCHITECTURE.md](./INGESTION_ARCHITECTURE.md)

---

## 0. Краткие выводы

1. **yt-dlp нужно использовать как библиотеку, а не как системную утилиту.** Это даёт структурированный `info_dict`, нормальные `progress_hooks`, `postprocessor_hooks`, отмену, отсутствие парсинга stdout и отсутствие shell-инъекций. CLI-режим оставить только как диагностический fallback.
2. **`info_dict` — это не стабильная схема, а расширяемый словарь.** Обязательных полей мало (`id`, `title`, `formats`/`url`); всё остальное зависит от экстрактора и версии сайта. Поэтому нужен нормализующий слой, а не работа с `info_dict` напрямую в бизнес-логике.
3. **Форматы yt-dlp — это потоки, а не варианты для пользователя.** Видео и аудио часто лежат отдельными форматами; пользователю нужны сгруппированные варианты («1080p MP4», «audio original», «MP3 320»), а не десятки `format_id`.
4. **`supports_url` и классификация URL должны работать без сети, но не обязаны быть точными.** Отдельные `is_playlist_url`/`is_post` в виде `bool` не нужны: для неоднозначных URL (например, YouTube `watch?v=...&list=...`) достаточно `classify_url() -> single | post | collection | unknown`; окончательный тип определяет `get_info`.
5. **Куки и прокси — это свойства источника, а не глобальные настройки.** Нужен `SourceProfile` на каждый loader: `cookies`, `cookies_from_browser`, `proxy`, `impersonate`, `extractor_args`, таймауты, лимиты.
6. **VK Music не должен идти через yt-dlp.** Для аудио нужен отдельный `VkApiLoader` (API-клиент + токен), а для VK-видео и стен — yt-dlp. Это разные пути, разные ограничения и разные ошибки.
7. **Картинки, гифки и документы — не задача yt-dlp.** yt-dlp проектировался вокруг аудио/видео; image-only Pinterest pin может вообще не иметь `formats`. Нужен отдельный `DirectAssetLoader`/`AssetLoader` и расширение модели.
8. **Скачивание и тегирование нужно разделить.** yt-dlp не должен писать финальные теги и обложки: это уже делает существующий metadata-слой TrackForge. Иначе появляются два конкурирующих писателя и непредсказуемый результат.
9. **Плейлисты нельзя резолвить полностью.** Для коллекции сначала нужен «плоский» список (`extract_flat`), затем полный `get_info` только для выбранных элементов. Иначе плейлист на 500 треков превращается в сотни сетевых запросов до первого вопроса пользователю.
10. **База данных не нужна.** Достаточно TOML-конфига, временного staging-каталога, in-memory кэша и файлов `.part` для докачки. Историю, индекс и «библиотеку» не храним.
11. **Поиск, thumbnail и recommended-варианты — ответственность конкретного `MediaLoader`.** YouTube/Yandex/VK сами знают, как искать, какие ссылки проверять и какая картинка лучше; общий `SearchProvider`/thumbnail-код быстро превращается в набор site-specific `if`.
12. **`yt-dlp[default]` тянет `yt-dlp-ejs`, но EJS не работает без JS runtime.** Для полноценного YouTube нужен runtime; в проекте используем Python-пакет `deno` (без системного Deno). Иначе `web`/`tv_samsung`-клиенты могут быть исключены и часть форматов пропадёт. `ffmpeg`/`ffprobe` — отдельно; `yt-dlp-yandex-music` — отдельный плагин.

---

## 1. Метод и границы исследования

Исследование отвечает на вопросы:

- какие поля реально можно получить из yt-dlp для одного медиа, плейлиста и поста;
- какие из них пригодны для выбора качества/формата и для последующего тегирования;
- как устроены поиск, куки, прокси и прогресс;
- где заканчивается универсальный yt-dlp и начинаются кастомные источники (VK, изображения, файлы);
- какую архитектуру имеет смысл заложить, чтобы не переусложнить проект.

Источники:

- исходники установленного yt-dlp `2026.10.21` (`yt_dlp/extractor/common.py`, `yt_dlp/YoutubeDL.py`, `yt_dlp/postprocessor/ffmpeg.py`, экстракторы YouTube, Yandex Music, Pinterest, VK);
- официальный README yt-dlp и раздел «Embedding yt-dlp»;
- документация VK API (внешние обёртки, поскольку официальные страницы методов часто недоступны без JS);
- спецификация LRC/Enhanced LRC;
- документация AcoustID, MusicBrainz и LRCLIB.

Живые запросы к YouTube из окружения проверки упирались в bot-check, поэтому поля ниже приведены по документированной схеме `InfoExtractor` и по коду экстракторов, а не по одному «эталонному» ответу. Это нормально: как раз показывает, почему нельзя рассчитывать на фиксированный набор полей.

---

## 2. Что отдаёт yt-dlp

### 2.1. Верхний уровень `info_dict`

`InfoExtractor` документирует словарь как расширяемый. Для одиночного видео обязательны только `id`, `title` и один из `formats`/`url`. Остальное — «если экстрактор смог».

| Группа | Поля | Комментарий |
|---|---|---|
| Идентификация | `id`, `display_id`, `title`, `alt_title`, `webpage_url`, `original_url`, `extractor`, `extractor_key` | `id` уникален в рамках экстрактора, не глобально |
| Тип результата | `_type` | `video` (по умолчанию), `playlist`, `multi_video`, `url`, `url_transparent` |
| Авторы/канал | `uploader`, `uploader_id`, `uploader_url`, `channel`, `channel_id`, `channel_url`, `channel_follower_count`, `channel_is_verified`, `creators`, `creator` (устар.) | «Автор» и «публикатор» — не одно и то же; отдельного `publisher` нет |
| Даты | `timestamp`, `upload_date`, `release_timestamp`, `release_date`, `release_year`, `modified_timestamp`, `modified_date` | `upload_date`/`release_date` — `YYYYMMDD`; `release_year` — int |
| Описание | `description`, `tags`, `categories`, `cast`, `location`, `license` | `description` может быть огромным |
| Длительность/статистика | `duration`, `view_count`, `like_count`, `dislike_count`, `comment_count`, `repost_count`, `save_count`, `average_rating` | не гарантированы |
| Доступность | `availability`, `age_limit`, `live_status`, `is_live`, `was_live`, `playable_in_embed`, `media_type` | `availability`: `public`, `unlisted`, `private`, `premium_only`, `subscriber_only`, `needs_auth`; `live_status`: `is_live`, `is_upcoming`, `was_live`, `post_live`, `not_live` |
| Изображения | `thumbnails`, `thumbnail` | `thumbnails` — список с `url`, `width`, `height`, `ext`, `preference`, `http_headers`; `thumbnail` — устаревшее одно значение |
| Субтитры | `subtitles`, `automatic_captions` | словарь `{lang: [{ext, url / data, name?, http_headers?}]}` |
| Главы | `chapters` | `start_time`, `end_time`, `title`; это не треклист |
| Форматы | `formats`, `url` | см. ниже |
| Плейлист | `entries`, `playlist_count`, `playlist_index` | `entries` может быть ленивым `PagedList` |
| Музыка | `track`, `track_number`, `track_id`, `artists`, `composers`, `genres`, `album`, `album_type`, `album_artists`, `disc_number` | списки предпочтительнее устаревших `artist`, `genre`, `composer`, `album_artist` |
| Сериалы | `series`, `series_id`, `season`, `season_number`, `season_id`, `episode`, `episode_number`, `episode_id` | для видео-постов и эпизодов |
| Служебное | `__post_extractor`, `_old_archive_ids`, `_format_sort_fields` | внутреннее, не для бизнес-логики |

Ключевой вывод: **`uploader`/`channel` — это не «публикатор» в смысле лейбла/издателя.** Для музыки `uploader` часто ровно тот, кто загрузил файл, а не исполнитель и не правообладатель. Эти поля — подсказки (`hints`), а не финальные метаданные.

### 2.2. Поля формата

Каждый элемент `formats` — поток. Документированы следующие поля (наличие не гарантировано):

| Поле | Смысл |
|---|---|
| `url` | URL потока; для HLS/DASH — манифест или сегмент |
| `format_id` | Короткий id формата, уникальный в рамках видео |
| `format`, `format_note` | Человекочитаемое описание |
| `ext`, `container` | Расширение/контейнер |
| `vcodec`, `acodec` | Кодеки; `none` означает отсутствие потока |
| `width`, `height`, `resolution`, `aspect_ratio`, `fps`, `dynamic_range` | Видео-параметры; `dynamic_range`: `SDR`, `HDR10`, `HDR10+`, `HLG`, `DV` |
| `vbr`, `abr`, `tbr` | Битрейты в kbps: видео, аудио, общий |
| `asr`, `audio_channels` | Частота дискретизации и каналы |
| `filesize`, `filesize_approx` | Точный/оценочный размер |
| `protocol` | `http`, `https`, `m3u8_native`, `http_dash_segments`, `websocket_frag` и т.п. |
| `fragments`, `fragment_base_url` | Для фрагментированных потоков |
| `language`, `language_preference` | Язык аудио/субтитров |
| `has_drm` | `True`, `False` или `maybe`; DRM-поток скачать нельзя |
| `preference`, `quality`, `source_preference` | Служебные приоритеты сортировки |
| `http_headers` | Заголовки, нужные для запроса |
| `request_data`, `manifest_url`, `hls_aes`, `impersonate` | Специфика протокола/защиты |

Типичная картина для YouTube-видео: отдельный video-only поток (например, `137`, 1080p, `acodec=none`) и отдельный audio-only (`140`, m4a, `vcodec=none`). Если показать пользователю сырой список, он увидит 20–30 строк и не поймёт, что выбрать. Нужна группировка в **выходные варианты**.

### 2.3. Плейлисты, посты и `multi_video`

`_type`:

- `video` — одиночное медиа;
- `playlist` — коллекция (плейлист, альбом, канал, доска);
- `multi_video` — несколько видео, образующих один пост/выпуск/шоу;
- `url` — редирект на другой URL;
- `url_transparent` — обёртка, которая прозрачно передаёт результат другому экстрактору.

Для `playlist`/`multi_video` обязателен `entries`; элементы — такие же `info_dict`. `playlist_count` может отсутствовать, а `entries` может быть ленивым.

Практические следствия:

1. **Нельзя определить «пост» только по `_type`.** Социальный пост с несколькими медиа может быть `playlist` или `multi_video`; пост с текстом и одним видео может быть `video`. Тип нужно выводить из URL-источника + `_type` + наличия текста/нескольких entries.
2. **`entries` может быть ленивым.** Если сделать `list(info["entries"])`, можно получить все страницы и все запросы. Для UI нужен `extract_flat` и лимит.
3. **`url_transparent` надо разворачивать.** Иначе `get_info` вернёт обёртку без форматов.

### 2.4. Музыкальные поля

Для музыкальных экстракторов yt-dlp отдаёт:

- `track` — название трека (иногда без артиста);
- `artists` — список исполнителей;
- `album`, `album_artists`, `album_type`;
- `track_number`, `disc_number`;
- `release_date`, `release_year`;
- `genres`;
- `composers`.

Но экстракторы ведут себя по-разному. Например, Yandex Music в `YandexMusicTrackIE` формирует `title` как `"{artist} - {track_title}"`, отдельно заполняя `track` и `artist`. Это надо нормализовать, иначе в теги попадёт «Artist - Title» в поле title и дубль в artist.

### 2.5. Чего yt-dlp не даёт

- **Единого `publisher`.** Есть `uploader`, `channel`, `album_artists`; лейбл/издатель — из MusicBrainz/Discogs или сайта.
- **Гарантированных музыкальных метаданных.** Для обычного YouTube-видео `track`/`artists`/`album` может не быть вообще.
- **Текстов песен.** Кроме отдельных экстракторов (NetEase, QQ Music и т.п.) и субтитров; `.elrc` не генерируется.
- **Картинок как first-class медиа.** Pinterest-экстрактор описывает только видео-форматы; image-only pin может вернуть пустой `formats`.
- **Стабильной схемы между версиями.** Поля добавляются/переименовываются; `artist`/`genre`/`composer` уже помечены deprecated в пользу списков.
- **Провенанса и уверенности.** Непонятно, откуда поле и можно ли ему верить.
- **Долгоживущих URL.** Подписанные ссылки на потоки истекают; кэшировать их между запусками нельзя.

### 2.6. Сокращённый пример структуры (не реальный ответ)

```json
{
  "_type": "video",
  "id": "abc123",
  "title": "Artist - Track (Official Video)",
  "track": "Track",
  "artists": ["Artist"],
  "album": "Album",
  "release_date": "20240112",
  "duration": 252.4,
  "uploader": "Artist",
  "channel": "Artist",
  "webpage_url": "https://example.com/watch?v=abc123",
  "thumbnails": [
    {"url": "https://.../maxres.jpg", "width": 1280, "height": 720, "preference": 0}
  ],
  "formats": [
    {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none",
     "height": 1080, "fps": 30, "filesize": 48000000},
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
     "abr": 128, "filesize": 4100000}
  ],
  "subtitles": {
    "en": [{"ext": "vtt", "url": "https://.../en.vtt"}]
  }
}
```

---

## 3. Форматы и выбор пользователя

### 3.1. Потоки ≠ варианты

Нужно различать три уровня:

1. **`StreamInfo`** — один поток yt-dlp (`format_id`, кодек, разрешение, битрейт, размер).
2. **`DownloadVariant`** — то, что видит пользователь: «1080p MP4 (H.264 + AAC)», «720p WebM», «Audio original (Opus 160k)», «MP3 320 (transcode)». Внутри — один или несколько `StreamInfo`.
3. **`DownloadPlan`** — выбранные варианты для одного или нескольких `MediaItem`.

Группировка (предлагаемый алгоритм):

1. Разделить форматы на `video_only`, `audio_only`, `progressive` (video+audio в одном), `image`.
2. Для каждого video-only подобрать лучший совместимый audio-only:
   - тот же контейнер/совместимая пара (mp4+m4a, webm+webm/opus);
   - приоритет языку оригинала;
   - приоритет большему `abr`, но без экзотики.
3. Схлопнуть дубликаты по `(height, container, vcodec, dynamic_range)`.
4. Добавить progressive-варианты, если они лучше/удобнее (например, 720p MP4 с уже встроенным аудио).
5. Добавить audio-only варианты: «original» (без перекодирования) и, опционально, «MP3 320»/«M4A AAC» (transcode через ffmpeg).
6. Посчитать `filesize = sum(stream.filesize)`; если неизвестно — `filesize_approx` с пометкой «~».
7. Пометить `requires_merge`/`requires_ffmpeg`, если потоков больше одного.
8. Сохранить `selector` — выражение для yt-dlp. Предпочтительно точные `format_id` (`137+140`) с запасным selector-выражением (`bv*[height=1080]+ba/b[height=1080]`).

Пользователю показывать 3–7 вариантов, а не 30. Полный список — по `--all-formats`.

### 3.2. Селекторы yt-dlp

Синтаксис yt-dlp:

- `bv*+ba/b` — лучшее видео + лучшее аудио, иначе лучшее комбинированное;
- `bv*[height<=1080]+ba/b[height<=1080]` — ограничение по высоте;
- `ba[acodec=opus]/ba/b` — аудио с приоритетом Opus;
- `137+140` — точные форматы;
- `bestvideo+bestaudio` требует ffmpeg для merge.

Селекторы зависят от экстрактора и версии. Поэтому в модели храним и точные `format_id`, и fallback-выражение. Если при скачивании точный формат исчез, можно один раз попробовать fallback и предупредить пользователя.

### 3.3. Merge, remux, transcode

- **Merge** (video-only + audio-only) требует ffmpeg. Без ffmpeg yt-dlp предложит только progressive.
- **Remux** (смена контейнера без перекодирования) быстрый, но не всегда возможен.
- **Transcode** (например, в MP3) — единственный способ получить несовместимый кодек/контейнер; это потеря качества и время.
- Для музыки по умолчанию лучше **не перекодировать**: брать исходный audio-only поток. MP3 — отдельная опция «для совместимости».

### 3.4. Рекомендованный вариант

«Лучший» вариант зависит от источника и сценария, поэтому его должен помечать сам loader:

- YouTube: видео — MP4 (H.264 + AAC) максимального разрешения; аудио — M4A/AAC максимального битрейта. Fallback — лучший доступный MP4/audio.
- Yandex Music: аудио — лучшее доступное (FLAC, иначе MP3 320/192); нужен плагин `yt-dlp-yandex-music`.
- VK: аудио — M4A/MP3 с максимальным битрейтом; видео — MP4.
- Generic yt-dlp: видео — MP4/H.264; аудио — M4A/AAC; если нет — лучший original без transcode.

Модель хранит `recommendations.audio` и `recommendations.video` как `variant_id`; CLI/бот используют их для `--audio`/`--video`/`--yes`, а не выбирают «первый в списке». Это важно для Telegram-бота: он получает готовые ID и не угадывает.

---

## 4. Поведение источников

### 4.1. YouTube

- URL: `youtube.com/watch`, `youtu.be`, `/shorts`, `/playlist`, `/channel`, `music.youtube.com`.
- yt-dlp поддерживает видео, плейлисты, каналы, Shorts, музыку; поиск — через `ytsearchN:`.
- Основные боли: bot-check, age restriction, PO token, нестабильность клиентов. Нужны cookies, иногда `impersonate` и JS runtime.
- Для полноценной поддержки YouTube нужны `yt-dlp-ejs` и JS runtime (Deno/Node/Bun/QuickJS); без них часть форматов/клиентов может быть недоступна.
- Cookies YouTube лучше хранить отдельно от других источников: они чаще всего протухают и чаще всего ломают параллельные запросы.
- Не использовать один `YoutubeDL` на несколько потоков; cookies-файл тоже не должен писаться concurrently.

### 4.2. Yandex Music

- URL: `music.yandex.ru|kz|ua|by|com/album/<id>/track/<id>`, `/album/<id>`, `/users/<user>/playlists/<id>`, `/artist/<id>/...`, а с плагином — ещё `/playlists/<uuid>` (shared/charts/liked).
- **Встроенный Yandex-экстрактор yt-dlp считается сломанным:** он ходит в устаревшие `handlers/*.jsx`. Нужен сторонний плагин `yt-dlp-yandex-music` (PyPI, MIT, на момент проверки `2026.9.24`), который подменяет built-in extractor и добавляет shared/liked/charts/artist/album.
- Плагин ставится **в то же окружение**, что и `yt-dlp`; `yt-dlp --list-extractors` плагины не показывает. Проверять надо через `check_install.py` или `importlib.metadata.version("yt-dlp-yandex-music")` + `find_spec("yt_dlp_plugins.extractor.yandex_music_v2")`; `gen_extractor_classes()` может показать только built-in-имена.
- Качества: плагин просит лучшее доступное — FLAC, иначе MP3 320, иначе MP3 192. FLAC доступен не всем аккаунтам/трекам, поэтому «MP3 320 по умолчанию» — неверное упрощение.
- Нужен залогиненный (обычно платный) аккаунт и cookies с `.yandex.ru` `Session_id`. При просроченной сессии API может вернуть `smart_preview` — 13–30-секундные файлы **без явной ошибки**. Плагин это детектит и предупреждает; нам всё равно нужна проверка длительности в download-слое.
- Метаданные: title/artist/album/album artist/track/disc/year, обложка 1000×1000; genre — только машинные коды, feat. — внутри title (структурного feat-поля нет).
- Плагин сторонний и в README самого автора помечен как «vibecoded / not audited»; пинить версию, не обновлять автоматически, покрывать fixture-тестами и smoke.

### 4.3. Pinterest

- URL: `/pin/<id>`, `/<user>/<board>/`.
- `PinterestIE` извлекает видео-форматы из `videos.video_list`; для pin без видео `formats` может быть пустым.
- `PinterestCollectionIE` обходит доску страницами (`page_size=250`) и возвращает только то, что смог извлечь как видео; image-only pins в такой коллекции практически теряются.
- Значит, для Pinterest нужен либо кастомный `PinterestLoader` с обращением к ресурсам Pinterest (`PinResource`, `BoardFeed`) и извлечением `images`, либо отдельный `AssetLoader` для изображений. Наследование от `YtDlpLoader` здесь полезно только для видео-части.
- Pinterest — хороший пример того, почему модель должна поддерживать `MediaKind.IMAGE`/`ANIMATED_IMAGE`, а не только audio/video.

### 4.4. VK

- yt-dlp покрывает VK-видео (`VKIE`) и стены (`VKWallPostIE`), включая аудио-вложения в постах.
- Музыкальные URL VK (`/audio...`, `/music/playlist/...`, `/music/album/...`) — отдельный мир. Для них нужен `VkApiLoader` через VK API, а не yt-dlp.
- Историческая схема: `audio.get` (список аудио пользователя/сообщества, `owner_id`, `album_id`, `audio_ids`, `offset`, `count` до 6000), `audio.getById`, `audio.search`. Требуется access token с доступом к аудио; доступность методов/скоупов зависит от приложения и аккаунта.
- Важная деталь из старой документации: ссылки на mp3 привязаны к IP-адресу. Значит, URL нельзя кэшировать между сессиями/сетями и нельзя отдавать другому клиенту; скачивать надо сразу после `get_info`.
- В объекте аудио есть `lyrics` (обычно plain text), `duration`, `artist`, `title`, `url`, `album_id`, обложка. Это хороший источник подсказок, но не финальных тегов.
- VK API имеет rate limit (порядка 3 запросов/с на токен); прокси и токен — per-source.
- Если токена нет или API недоступен, разумный fallback — yt-dlp для публичных VK-видео/стен, но не для музыки.

### 4.5. SoundCloud, Bandcamp, Vimeo и остальные

- yt-dlp поддерживает десятки источников; для них не нужно писать отдельный класс, достаточно `YtDlpLoader` + профиль.
- SoundCloud: `scsearchN:` для поиска, `sets` для плейлистов, audio-only.
- Bandcamp: альбомы/треки, часто с готовыми метаданными.
- Vimeo: видео, плейлисты.
- Специализированный класс имеет смысл, только если нужны:
  - собственные cookies/прокси/экстрактор-аргументы по умолчанию;
  - собственный поиск;
  - дополнительная нормализация (например, Yandex `artist - title`);
  - обогащение (обложки, lyrics) через API;
  - обход ограничений yt-dlp (Pinterest images).

### 4.6. Прямые файлы, изображения, гифки, документы

yt-dlp — не универсальный загрузчик файлов. Для `https://.../cover.jpg`, `.../anim.gif`, `.../file.pdf` нужен `DirectAssetLoader`:

- `supports_url` — по расширению (без сети);
- `get_info` — `HEAD`/`GET` с проверкой `Content-Type`, `Content-Length`, `Content-Disposition`;
- `DownloadVariant` — original +, при необходимости, thumbnail/resized;
- `download` — потоковое копирование с прогрессом, `Range` для докачки;
- для изображений — `MediaKind.IMAGE`/`ANIMATED_IMAGE`, без попытки писать аудио-теги;
- для документов — `MediaKind.DOCUMENT`, сохранять оригинальное имя/расширение.

Это же API пригодится для VK API (прямые URL) и Pinterest images.

---

## 5. Поиск по названию

### 5.1. Что есть в yt-dlp

- `ytsearchN:query` — поиск YouTube (видео).
- `scsearchN:query` — поиск SoundCloud.
- `https://music.youtube.com/search?q=...` — YouTube Music search URL; можно извлекать `extract_flat`.
- Поиск возвращает playlist-подобный результат: `entries` с `id`, `title`, `url`, `uploader`, `duration`, `thumbnails`.
- Полные форматы у результатов поиска обычно не нужны; их резолвят после выбора.

### 5.2. Ограничения

- Поиск — это сетевой запрос, его нельзя делать в `supports_url`.
- Разные источники дают разные результаты; единого «поиска по всему» нет.
- Ранжирование yt-dlp — это выдача сайта, не обязательно лучшее совпадение.
- Название «Artist - Title» может содержать `feat.`, `remix`, `nightcore`, `official video`; не надо агрессивно чистить, но нужно показывать источник и длительность.
- Автовыбор первого результата опасен: легко скачать ремикс/кавер/не тот клип.

### 5.3. Предлагаемая модель

- `MediaLoader.search(query, limit) -> list[SearchCandidate]` — метод конкретного загрузчика. `YtDlpLoader` даёт generic-реализацию (`ytsearch`/`scsearch`), сайтовые наследники переопределяют под свой API/фильтры.
- `SearchService` — только оркестратор: опрашивает loader'ы с `capabilities.search`, передаёт каждому его `SourceProfile`, собирает, дедуплицирует и ранжирует.
- `SearchCandidate` — лёгкий объект: `source`, `id`, `url`, `title`, `creator`, `duration`, `thumbnails`, `thumbnail`, `published_at`, `score`.
- YouTube должен сам отфильтровать недоступное/Shorts/эфиры и вернуть канонический `webpage_url`; Yandex/VK — своими API.
- `MusicBrainz` — не поиск: он не даёт URL для скачивания, это `MetadataProvider` ingest-слоя.
- Дедупликация по `(source, id)`/каноническому URL; скоринг — эвристика, финальное решение за пользователем.
- По умолчанию 10 результатов, `--limit`.

### 5.4. Thumbnail

- `info_dict["thumbnail"]` — не всегда лучший вариант; в `thumbnails` может не быть `maxres`, а у Яндекса/Pinterest/VK свои CDN-варианты.
- Выбор — метод loader'а: `choose_thumbnail(item) -> Thumbnail | None`; generic-реализация сортирует `thumbnails` по площади/`preference`/формату.
- Специфичные loader'ы добавляют fallback-URL (YouTube `maxresdefault.jpg`/`sddefault.jpg`, Yandex `orig`, Pinterest/VK `coverUrl`) и при необходимости проверяют доступность 1–3 кандидатов.
- `fetch_thumbnail(thumbnail, max_bytes)` возвращает `ThumbnailAsset` (bytes + mime) — это нужно Telegram-боту; не надо тащить Pillow в core.
- В `SourceInfo`/`MediaItem`/`SearchCandidate` попадает уже выбранный `thumbnail`, а `thumbnails` остаётся для UI/отладки.

---

## 6. Куки, прокси и аутентификация

### 6.1. Параметры yt-dlp

| Задача | Параметр `YoutubeDL` | CLI-аналог |
|---|---|---|
| Netscape cookies | `cookiefile` (путь или text stream) | `--cookies` |
| Cookies из браузера | `cookiesfrombrowser` (tuple: browser, profile, keyring, container) | `--cookies-from-browser` |
| Прокси | `proxy` | `--proxy` |
| Отдельный прокси для geo-проверки | `geo_verification_proxy` | `--geo-verification-proxy` |
| Подмена TLS/HTTP-отпечатка | `impersonate` (`ImpersonateTarget`) | `--impersonate` |
| Аргументы экстракторов | `extractor_args` | `--extractor-args` |
| Таймаут сокета | `socket_timeout` | `--socket-timeout` |
| Повторы | `retries`, `fragment_retries`, `extractor_retries` | `--retries`, `--fragment-retries`, `--extractor-retries` |
| Задержки между запросами | `sleep_interval_requests` | `--sleep-requests` |
| Ограничение скорости | `ratelimit` | `--limit-rate` |
| Параллельные фрагменты | `concurrent_fragment_downloads` | `--concurrent-fragments` |
| Geo bypass | `geo_bypass`, `geo_bypass_country` | `--geo-bypass`, `--geo-bypass-country` |

Для HTTPS-прокси и persistent connections yt-dlp рекомендует `requests`; для impersonation — `curl_cffi`. Это опциональные зависимости; `yt-dlp[default]` тянет `requests` и другие полезные пакеты. SOCKS-прокси yt-dlp умеет и без отдельного `PySocks` (использует свою реализацию).

### 6.2. Куки: практические проблемы

- Netscape cookies — обычный текстовый файл; его нужно проверять на формат и права (не world-readable).
- yt-dlp может **записывать** обновлённые cookies обратно в `cookiefile`. Если два процесса используют один файл, он может быть повреждён. Решение: на job делать приватную копию (0600) или использовать read-only поток, если запись не нужна.
- `cookiesfrombrowser` может требовать keyring (Linux: `secretstorage`), и не работает в headless-окружениях.
- Cookies протухают; ошибка должна говорить «обновите cookies», а не «сайт сломался».
- Не логировать содержимое cookies, `Cookie`-заголовки и токены.

### 6.3. Прокси: практические проблемы

- Прокси нужен не всем источникам; делать его глобальным — значит случайно гонять весь трафик через один сервер.
- `proxy` в `YoutubeDL` задаётся на инстанс; это удобно для per-source профиля.
- Прокси с логином/паролем нельзя печатать в лог; в конфиге лучше ссылаться на env (`${YOUTUBE_PROXY}`).
- `impersonate` и cookies часто нужны вместе (TLS fingerprint + авторизация).
- Для `geo_verification_proxy` есть отдельный параметр; не путать с основным прокси.

---

## 7. Прогресс и отмена

### 7.1. `progress_hooks`

yt-dlp вызывает hook со словарём:

- `status`: `downloading`, `finished`, `error`;
- `info_dict`: извлечённые данные;
- `filename`, `tmpfilename`;
- `downloaded_bytes`;
- `total_bytes` (может быть `None`);
- `total_bytes_estimate`;
- `elapsed`, `eta`, `speed`;
- `fragment_index`, `fragment_count`.

Гарантия: при успешном скачивании hook вызывается хотя бы один раз со `status=finished`.

### 7.2. `postprocessor_hooks`

- `status`: `started`, `processing`, `finished`;
- `postprocessor`: имя (например, `FFmpegExtractAudio`, `FFmpegVideoRemuxer`, `FFmpegMetadata`);
- `info_dict`.

Это позволяет показывать «merge», «extract audio», «remux», а не только «download».

### 7.3. Отмена

- В hook можно проверить `threading.Event` и бросить `yt_dlp.utils.DownloadCancelled`.
- `DownloadCancelled` — наследник `YoutubeDLError`; его нужно отобразить в `CancelledError`, а не в «ошибка сайта».
- Для субпроцессного варианта отмена — сигнал процессу; для библиотеки — исключение из hook.
- После отмены `.part`-файл можно сохранить для докачки или удалить по политике.

### 7.4. Агрегация

- Для одного файла: `downloaded_bytes / (total_bytes or total_bytes_estimate)`.
- Для плейлиста: `(completed_items + current_fraction) / total_items`.
- Для merge/extract: отдельная стадия, без ложных процентов; показывать spinner/этап.
- Троттлить события (например, 10/с), иначе rich/CLI захлёбывается.

---

## 8. Библиотека или системная утилита

### 8.1. Сравнение

| Критерий | Библиотека `yt_dlp` | CLI `yt-dlp` через subprocess |
|---|---|---|
| Структурированные данные | `extract_info` → dict-like; `sanitize_info` | `-J`/`--dump-single-json`, парсинг JSON |
| Прогресс | `progress_hooks`, `postprocessor_hooks` | `--progress-template`, парсинг строк |
| Ошибки | классы `DownloadError`/`ExtractorError`, traceback | код возврата + stderr, парсинг текста |
| Формат-селекторы | `format` param | `-f` |
| Cookies/прокси | per-instance params | CLI-флаги |
| Отмена | исключение из hook | сигнал процессу |
| Изоляция сбоев | падение/утечка в процессе Python | отдельный процесс |
| Версионирование | pinned dependency, ломается при обновлении | можно обновлять отдельно |
| Скорость старта | импорт ~секунда (лениво) | запуск процесса |
| Безопасность | нет shell | нужна аккуратность с аргументами |
| Тестируемость | легко подменить `YoutubeDL` | сложнее, нужен fake binary |

### 8.2. Рекомендация

**Основной путь — библиотека.** Причины:

- yt-dlp README прямо просит не парсить обычный stdout и предлагает `-J`, `--print`, `--progress-template` — то есть для subprocess всё равно нужен структурированный протокол, который библиотека даёт бесплатно;
- `progress_hooks` и `postprocessor_hooks` — это ровно то, что нужно для UX;
- `extract_info(download=False)` возвращает данные без скачивания;
- можно задать per-source `cookiefile`, `proxy`, `extractor_args` на отдельный инстанс;
- нет риска shell-инъекции и проблем с quoting;
- проще тестировать: подменить `YoutubeDL`/`extract_info` фейком.

Ограничения библиотеки и меры:

- **Импорт дорогой** → ленивый импорт только в `YtDlpLoader` (включая `search`).
- **Версия ломается** → пин в lockfile, `tfg doctor` показывает версию, регулярное обновление.
- **Глобальные плагины** → yt-dlp при инициализации подгружает плагины; это код, которому доверяем. Для воспроизводимости можно ограничить plugin dirs/не устанавливать сторонние.
- **Падение процесса** → для CLI допустимо; для серверного режима позже можно вынести download в worker-процесс. Прогресс через IPC — отдельная задача, не MVP.
- **JS runtime/EJS** → `yt-dlp-ejs` и Python-пакет `deno` включаем в зависимости; проверять `find_deno_bin()`/версию в `doctor`, не полагаться на системный PATH.

**Subprocess-адаптер** имеет смысл как:

- диагностический режим `--engine=cli` (сравнить поведение);
- fallback, если библиотека несовместима с окружением;
- изоляция для будущего серверного режима.

Но не как основной путь: это удвоение логики (селекторы, прогресс, ошибки) и хрупкий парсинг.

### 8.3. Зависимости и лицензия

- `yt-dlp` лицензирован под **Unlicense** (public domain), Python `>=3.10`; ограничений copyleft нет.
- `yt-dlp[default]` включает `mutagen`, `requests`, `certifi`, `brotli`, `websockets`, `pycryptodomex` и **`yt-dlp-ejs`** (на 2026.10.21 — `0.8.0`).
- `ffmpeg`/`ffprobe` — внешние бинарники, не Python-зависимости.
- **`yt-dlp-ejs` — не «просто пакет»:** для него нужен JavaScript runtime. Официально поддерживаются Deno (рекомендуется), Node.js, Bun и QuickJS. Без runtime часть YouTube-клиентов/форматов может быть недоступна; `yt-dlp` прямо говорит, что `yt-dlp-ejs` + JS runtime нужны для полноценной поддержки YouTube.
- **Deno можно не ставить системно:** PyPI-пакет `deno` (официальная перепаковка бинарников Deno, MIT, `2.9.8`) даёт `deno.find_deno_bin()` и `deno`-команду в venv. Это уменьшает зависимость от окружения; цена — wheel ~40 MB.
- Поддерживаемые платформы `deno`: Linux x86_64/arm64 (glibc 2.27+), macOS x86_64/arm64, Windows x64. На Alpine/musl нужен fallback (Node/Bun/системный Deno).
- `yt-dlp` умеет `js_runtimes`/`--js-runtimes`; путь лучше передавать явно: `js_runtimes={"deno": {"path": deno.find_deno_bin()}}`. Если runtime нет, `web`/`tv_samsung`-клиенты могут быть исключены.
- `yt-dlp-yandex-music` — отдельный плагин, ставится в то же окружение; см. §4.2.
- `curl_cffi` — опционально для impersonation (TLS fingerprint).
- `yt-dlp[deno]` extra делает то же самое (`deno>=2.9.8`); явная зависимость `deno` в `pyproject.toml` проще и не зависит от extras. `doctor` проверяет `find_deno_bin()` и версию.

| Компонент | Зачем | Обязателен |
|---|---|---|
| `yt-dlp-ejs` | JS-код для YouTube (challenge/player) | для полноценного YouTube |
| `deno` (PyPI) | выполнить EJS | да, если нужен EJS |
| `yt-dlp-yandex-music` | Yandex Music API | для Yandex Music |
| `ffmpeg`/`ffprobe` | merge/remux/audio/probe | для видео и postprocessing |
| `curl_cffi` | impersonate | опционально |

---

## 9. Связь с metadata-слоем TrackForge

### 9.1. Что мапить

| `SourceInfo`/`MediaItem` | `metadata`-модель | Примечание |
|---|---|---|
| `title` | `CommonMetadata.title` | подсказка, не финал |
| `artists` | `CommonMetadata.artists` | список; `artist` — deprecated |
| `creator`/`uploader`/`channel` | `artist`/`comment` | не путать с исполнителем |
| `album`, `album_artists` | `AudioMetadata.album`, `album_artist` | `album_artist` в модели один, список схлопнуть |
| `track_number`, `disc_number` | `AudioMetadata.track_number`, `disc_number` | |
| `release_date`/`upload_date` | `PartialDate` | `YYYYMMDD` → `PartialDate` |
| `genres` | `CommonMetadata.genre` | список |
| `description` | `comment`/`VideoMetadata.synopsis` | для видео — synopsis |
| `thumbnails` | `Artwork` | скачать и выбрать front |
| `subtitles` | `.lrc`/`.srt` sidecar | не все субтитры — lyrics |
| `lyrics` (VK/NetEase) | `Lyrics` | plain или synced |
| `duration` | техническое поле/probe | не тег |
| `webpage_url` | `comment`/`extra` | provenance |
| `chapters` | отдельная модель | не путать с треками |

### 9.2. Подсказки vs финальные метаданные

`SourceInfo` — это **hints**. Финальные метаданные должны собираться из:

1. явного выбора/правки пользователя;
2. fingerprint → AcoustID → MusicBrainz;
3. провайдеров (MusicBrainz, Discogs, Cover Art Archive);
4. source hints как fallback.

Каждому значению нужен провенанс и уверенность. Иначе при расхождении «название с YouTube» и «MusicBrainz» невозможно понять, что писать.

### 9.3. Кто пишет теги

Только существующий `trackforge.metadata` (mutagen/ffmpeg). yt-dlp запускается с:

- `embed_metadata = False`;
- `embed_thumbnail = False`;
- `writeinfojson`/`writethumbnail` — только как sidecar для ingest (опционально).

Иначе yt-dlp и TrackForge будут по очереди перезаписывать одни и те же поля, а `--embed-metadata` ещё и добавлять ID3v1.

---

## 10. Тексты песен, LRC и ELRC

- **LRC** — текстовый sidecar с временными метками `[mm:ss.xx]`; ID-теги `[ti:]`, `[ar:]`, `[al:]`, `[offset:]`.
- **Enhanced LRC / A2** — тот же формат, но с word-level метками `<mm:ss.xx>` внутри строки.
- yt-dlp умеет писать субтитры, а `FFmpegSubtitlesConvertorPP` поддерживает `lrc` (ffmpeg-муксер `lrc`). Но это субтитры, а не обязательно текст песни; авто-субтитры как lyrics использовать нельзя без проверки.
- `.elrc` yt-dlp/ffmpeg не генерируют; для word-level нужен источник с пословными таймингами (например, LRCLIB `hasWordSync`/`lyricsfile`).
- Если есть только plain text, не надо выдумывать тайминги: можно записать текст в тег (`USLT`/`LYRICS`) и/или `.txt`, но не выдавать его за синхронизированный `.lrc`.
- Имя sidecar должно совпадать с финальным stem медиа: `Author - Title.mp3` → `Author - Title.lrc`/`Author - Title.elrc`; обложка — `Author - Title.cover.jpg` (в плоской папке общий `cover.jpg` может конфликтовать между артистами).
- При коллизии имён sidecar переезжает вместе с медиа в `1/`, `2/`, ... — имя не портится суффиксом `(1)`.

---

## 11. Идентификация после скачивания

Это не часть загрузчика, но граница важна.

1. `ffprobe`/`MediaFile.open` — технические параметры и существующие теги.
2. `fpcalc` (Chromaprint) → AcoustID lookup → MusicBrainz recording ID.
   - AcoustID нужен client API key; сервис просит не более 3 запросов/с и не для коммерческого использования без отдельной договорённости.
3. MusicBrainz lookup по MBID/поиску; обязателен осмысленный `User-Agent`, лимит ~1 запрос/с.
4. Провайдеры обложек/треков (Cover Art Archive, Discogs) и lyrics (LRCLIB; требует `User-Agent`, лимиты, `Retry-After`).
5. Ранжирование кандидатов: fingerprint score, совпадение title/artist, duration, album, source hints.
6. Запись через `MediaFile.save()`, переименование, sidecars.

Если fingerprint недоступен (нет `fpcalc`, инструментальная/живая запись, слишком короткий фрагмент) — текстовый поиск по source hints. Если и там нет уверенности — оставить source metadata и предупредить, не перезаписывать пользовательские значения.

---

## 12. Риски и ограничения

| Риск | Следствие | Мера |
|---|---|---|
| Экстракторы ломаются при изменении сайта | внезапные ошибки | pinned yt-dlp, `doctor`, быстрый апдейт, typed errors |
| Cookies протухают | 401/403/bot-check | per-source cookies, понятная ошибка, копия файла на job |
| Прокси недоступен | таймауты | per-source proxy, health-check, fallback «direct» только явно |
| DRM/premium | формат есть, скачать нельзя | `has_drm`, `availability`, `VariantUnavailable` |
| Огромные плейлисты | долгий resolve/скачивание | flat extraction, лимиты, пагинация, `--items` |
| Подписанные URL истекают | 403 при отложенном скачивании | не кэшировать URL; скачивать сразу после resolve |
| Разные форматы/кодеки | непредсказуемый файл | группировка, явные labels, ffmpeg-проверка |
| Images не поддерживаются yt-dlp | пустой `formats` | `DirectAssetLoader`, кастомный Pinterest loader |
| VK API ограничен | нет токена/метода | fallback на yt-dlp для видео, не для музыки |
| Секреты в логах | утечка | redaction, не логировать cookies/tokens/proxy |
| Плагины yt-dlp = код | RCE при недоверенных плагинах | не ставить сторонние, документировать |

---

## 13. Источники

### yt-dlp

- [README: Embedding yt-dlp](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)
- [README: Format selection](https://github.com/yt-dlp/yt-dlp#format-selection)
- [README: Dependencies](https://github.com/yt-dlp/yt-dlp#dependencies)
- [README: Post-processing options](https://github.com/yt-dlp/yt-dlp#post-processing-options)
- [YoutubeDL parameters and hooks](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py)
- [InfoExtractor info_dict schema](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/common.py)
- [YouTube extractor/search](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_search.py)
- [Yandex Music extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/yandexmusic.py)
- [yt-dlp-yandex-music plugin](https://pypi.org/project/yt-dlp-yandex-music/)
- [yt-dlp-yandex-music source](https://github.com/fgsfds1/yt-dlp-yandex-music-plugin)
- [Pinterest extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/pinterest.py)
- [VK extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/vk.py)
- [FFmpeg postprocessors](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/postprocessor/ffmpeg.py)
- [yt-dlp EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [yt-dlp-ejs on PyPI](https://pypi.org/project/yt-dlp-ejs/)
- [Deno](https://deno.land)
- [deno on PyPI](https://pypi.org/project/deno/)
- [yt-dlp cookies FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)

### Источники и API

- [VK audio.get (сторонняя документация/обёртка)](https://vknet.github.io/vk/audio/get/)
- [VK API: права доступа](https://dev.vk.com/ru/reference/access-rights)
- [LRC file format](https://en.wikipedia.org/wiki/LRC_(file_format))
- [AcoustID web service](https://acoustid.org/webservice)
- [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API)
- [LRCLIB API](https://lrclib.net/docs)
- [Chromaprint](https://acoustid.org/chromaprint)
- [FFmpeg formats: lrc](https://ffmpeg.org/ffmpeg-formats.html#lrc)
- [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/)

---

*Дата обращения к источникам: 2026-11-09. Версия yt-dlp, на которую опирается документ: 2026.10.21.*
