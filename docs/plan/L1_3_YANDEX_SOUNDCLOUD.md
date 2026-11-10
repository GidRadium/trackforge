# L1.3 — Yandex Music и SoundCloud

**Предусловие:** [L1_2_PLAYLISTS.md](./L1_2_PLAYLISTS.md)  
**Следующий чекпоинт:** [L1_4_SITE_AUDIT.md](./L1_4_SITE_AUDIT.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Уже работают `YtDlpLoader`/`YouTubeLoader` для single и playlists. Теперь добавляем два источника с разными ограничениями:

- **Yandex Music** — только с плагином `yt-dlp-yandex-music`; встроенный экстрактор считается сломанным;
- **SoundCloud** — обычный yt-dlp, cookies опционально.

**Не делать:** скачивание, поиск, Vimeo/Bandcamp, `SourceInfo` v1.

---

## Цель

Получить `SourceInfo` для:

- Yandex: track, album, user playlist, shared/charts/liked playlist, artist (в пределах текущей модели);
- SoundCloud: track, set;
- с корректными вариантами, thumbnail, recommendations и typed errors.

---

## Задачи

### 1. Yandex plugin

Убедиться, что в окружении:

- `yt-dlp-yandex-music>=2026.9.24`;
- модуль `yt_dlp_plugins.extractor.yandex_music_v2` импортируется;
- `deno`/`yt_dlp_ejs` на месте (для YouTube, но doctor общий).

`yt-dlp --list-extractors` плагины не показывает. Нельзя проверять плагин только по `gen_extractor_classes()`: там могут быть built-in-имена.

### 2. `YandexMusicLoader`

`loaders/yandex_music.py`:

```python
class YandexMusicLoader(YtDlpLoader):
    name = "yandex_music"
    config_key = "yandex_music"
    priority = 20
    required_plugins = ("yt-dlp-yandex-music",)
```

- URL regex: `music.yandex.(ru|kz|ua|by|com)`;
- `classify_url`:
  - `/track/` → SINGLE;
  - `/album/`, `/playlists/`, `/artist/` → COLLECTION;
  - остальное UNKNOWN;
- `is_ready` — только наличие/импорт плагина;
- `get_info`:
  - без cookies → `AuthRequired` (не идти в сломанный built-in);
  - `super().get_info`;
  - нормализация `artist - title` (не дублировать артиста);
  - `warn_on_smart_preview`: проверить `quality`/duration; если похоже на 13–30-секундное превью — `PreviewOnly` warning;
- `recommend`: лучшее доступное аудио — FLAC, если сервер отдал, иначе MP3 320/192. Не утверждать, что FLAC есть всегда;
- `search` — не в этом чекпоинте (L3).

### 3. Yandex metadata

- title/artist/album/album artist/track/disc/year;
- cover 1000×1000;
- genre — машинные коды, не маппить в человекочитаемый жанр без отдельной таблицы;
- feat. — внутри title, структурного поля нет;
- `suggested_stem` — `artist - title`.

### 4. SoundCloud

`loaders/soundcloud.py`:

```python
class SoundCloudLoader(YtDlpLoader):
    name = "soundcloud"
    config_key = "soundcloud"
    priority = 20
```

- URL: track, `/sets/`;
- `classify_url`: `/sets/` → COLLECTION, иначе SINGLE/UNKNOWN;
- `recommend`: audio original без transcode;
- cookies опционально;
- `search` — L3.

### 5. Ошибки

- Yandex без плагина → `MissingPlugin`/`ConfigError` с инструкцией;
- Yandex без cookies → `AuthRequired`;
- `smart_preview` → warning/`PreviewOnly` (не молчаливый успех);
- SoundCloud timeout/403 → `AuthRequired`/`RateLimited`/`DownloadFailed` по ситуации;
- не логировать cookies.

### 6. Тесты

Fixtures:

- `yandex_track.info.json`;
- `yandex_album.info.json` (flat);
- `yandex_playlist.info.json` (shared/liked);
- `yandex_smart_preview.info.json`;
- `soundcloud_track.info.json`;
- `soundcloud_set.info.json`.

Тесты:

- `test_yandex_plugin.py` — `is_ready` без плагина/с плагином; импорт модуля;
- `test_yandex_mapper.py` — title/artist/album/cover/quality;
- `test_yandex_preview.py` — `PreviewOnly`;
- `test_yandex_urls.py`;
- `test_soundcloud.py` — track/set/URLs/recommend.

---

## Ручная проверка на реальных данных

### Yandex

Нужны cookies с `.yandex.ru` `Session_id` и залогиненный аккаунт.

1. Track:

```python
source = service.inspect("https://music.yandex.ru/album/.../track/...")
print(source.kind, source.title, source.items[0].recommendations)
print(source.items[0].variants)
```

Ожидаемо:

- `single`;
- audio variants: FLAC/MP3 320/192 в зависимости от аккаунта;
- thumbnail 1000×1000;
- нет `smart_preview` (проверить duration).

2. Album/playlist flat:

```python
collection = service.inspect("https://music.yandex.ru/album/...", flat=True, limit=20)
```

Ожидаемо: коллекция, не более 20, `resolution=flat`.

3. Проверить `ffprobe` duration после скачивания — это L2; в L1 достаточно, что metadata duration не 13–30 секунд.

### SoundCloud

```python
track = service.inspect("https://soundcloud.com/.../...")
sett = service.inspect("https://soundcloud.com/.../sets/...", flat=True, limit=10)
```

Ожидаемо: track — single/audio; set — collection; без падений.

---

## Критерии готовности

- [ ] Yandex-плагин импортируется; `is_ready` без него даёт понятную ошибку.
- [ ] Yandex track/album/playlist дают `SourceInfo`.
- [ ] Yandex recommendations — FLAC/MP3 по факту наличия, не жёстко MP3.
- [ ] `smart_preview` детектится.
- [ ] SoundCloud track/set работают.
- [ ] Cookies/proxy используются.
- [ ] `uv run pytest -q` зелёный.
- [ ] Ручной smoke Yandex + SoundCloud успешен.

---

## Что не делать

- Не скачивать.
- Не реализовывать поиск.
- Не добавлять Vimeo/Bandcamp.
- Не фиксировать `SourceInfo` v1.
- Не обещать FLAC всем аккаунтам.

---

## Передача дальше

Следующий чат получает:

- `YandexMusicLoader` с плагином и preview guard;
- `SoundCloudLoader`;
- fixtures;
- подтверждённые данные с двух сайтов.

Дальше — [L1_4_SITE_AUDIT.md](./L1_4_SITE_AUDIT.md).
