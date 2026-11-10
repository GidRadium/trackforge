# L1.1 — generic/YouTube single `inspect`

**Предусловие:** [L0_FOUNDATION.md](./L0_FOUNDATION.md), [L1_0_AUTH.md](./L1_0_AUTH.md)  
**Следующий чекпоинт:** [L1_2_PLAYLISTS.md](./L1_2_PLAYLISTS.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

L0 дал модели/registry/staging, L1.0 — cookies/proxy/doctor. Теперь нужен первый реальный источник: **YouTube single video** через `YtDlpLoader`, плюс `YouTubeLoader` с сайтовой политикой.

Ключевые документы:

- `docs/INGESTION_ARCHITECTURE.md` §4.3 (`YtDlpLoader`), §4.4 (YouTube), §5.1 (thumbnail), §6 (search — пока не делать), §7.1 (варианты), §7.1.1 (recommendations);
- `docs/INGESTION_RESEARCH.md` §2 (`info_dict`), §3 (форматы), §4.1 (YouTube).

**Не делать:** скачивание, плейлисты, поиск, Yandex/SoundCloud, CLI `inspect` (это L1.5), пользовательский выбор.

---

## Цель

По URL одного YouTube-видео получить `SourceInfo` с:

- `kind=SINGLE`;
- `items[0]` с title/creator/duration/thumbnails;
- `variants` — сгруппированные audio/video/progressive;
- `thumbnail` — лучший вариант;
- `recommendations.audio/video`;
- без записи файлов и без скачивания медиа.

---

## Задачи

### 1. `YtDlpLoader`

В `src/trackforge/acquire/loaders/ytdlp.py`:

```python
class YtDlpLoader(MediaLoader):
    name = "yt-dlp"
    config_key = "yt_dlp"
    priority = 1000
```

- `supports_url(url)` — `http(s)` fallback (generic);
- `get_info(ref, ctx, flat=False, limit=None)`:
  - `_extract(url, ctx, flat=flat, limit=limit)`;
  - `YtDlpMapper(ctx.profile).map(...)`;
  - `_finalize` — thumbnail/recommendations по items.
- `_extract`:
  - `YoutubeDL` opts: `skip_download=True`, `quiet=True`, `no_warnings=True`, `noprogress=True`, `ignoreerrors=False`, `cachedir=False`;
  - `js_runtimes={"deno": {"path": deno.find_deno_bin()}}`;
  - `remote_components=[]`;
  - cookies/proxy из `_base_options(ctx.profile)`;
  - `extract_flat` пока `False`;
  - `ydl.extract_info(url, download=False)`;
  - `ydl.sanitize_info(info)`.
- `download` — заглушка `NotImplementedError` (L2).

### 2. `YtDlpMapper`

В `loaders/ytdlp.py` или `loaders/ytdlp_mapper.py`:

- `_type=video` → `SourceInfo(kind=SINGLE)`;
- `_type=url`/`url_transparent` — пока `UnresolvedRedirect`/ошибка, плейлисты позже;
- поля:
  - `id`, `title`, `creator` (`uploader`/`channel`), `duration`, `description`;
  - `artists` (`artists` или `creator` fallback), `album`, `album_artists`, `track_number`, `disc_number`;
  - `release_date`/`upload_date` → `PartialDate`;
  - `thumbnails`/`thumbnail`;
  - `suggested_stem` = `"{creator} - {title}"` или `title`;
  - `webpage_url`/`url` → `page_url`;
  - `variants` — `build_variants`;
  - `recommendations` — `self.recommend`/`default_recommendations`;
  - `resolution=FULL`.
- Не падать на отсутствующих полях; неизвестное — в `extras`.

### 3. `build_variants`

- Разделить `formats` на video-only, audio-only, progressive;
- если `formats` пуст, но есть `url` — один progressive;
- для video-only подобрать audio:
  - совместимый контейнер (mp4+m4a, webm+opus);
  - язык оригинала;
  - максимальный `abr`;
- `selector` — точные `format_id` + fallback;
- `filesize`/`filesize_approx` суммировать;
- `requires_merge`/`requires_ffmpeg`;
- `downloadable=False` для `has_drm=True`/`"maybe"`, private/premium без auth, без URL;
- `label` — `"1080p MP4"`, `"Audio M4A 128k"` и т.п.;
- dedupe/sort.

### 4. `YouTubeLoader`

В `loaders/youtube.py`:

- URL regex: `youtube.com/watch`, `youtu.be`, `/shorts`, `music.youtube.com` (playlist — L1.2);
- `config_key="youtube"`, `priority=10`;
- `classify_url`: watch без `list` → `SINGLE`, `list=`/`/playlist` → `COLLECTION`, остальное `UNKNOWN`;
- `get_info` — через `YtDlpLoader`, но с `choose_thumbnail`/`recommend`;
- `choose_thumbnail`:
  - начинать с `item.thumbnails`;
  - для primary item проверить `maxresdefault.jpg`/`sddefault.jpg` (HEAD, 1–2 запроса);
  - выбрать `select_best_thumbnail`;
- `recommend`:
  - video: MP4/H.264/AAC, максимальное разрешение; fallback — лучший MP4, затем лучший;
  - audio: M4A/AAC, максимальный битрейт; fallback — лучший audio-only;
- `search` пока `SearchNotSupported` (L3).

### 5. Ошибки

Маппить `DownloadError`/`ExtractorError`:

- `UnsupportedUrl`;
- `AuthRequired` — sign in/login/private/members-only;
- `GeoBlocked`;
- `DrmProtected`;
- `RateLimited`;
- `Unavailable`;
- `NoFormats`;
- `DownloadFailed` — остальное.

Не парсить stdout/stderr; строки — только для сообщения.

### 6. Тесты

Fixtures (sanitized, без cookies/токенов):

- `tests/acquire/fixtures/youtube_video.info.json`;
- `tests/acquire/fixtures/youtube_drm.info.json` (если есть).

Тесты:

- `test_ytdlp_mapper.py` — поля/даты/thumbnail/recommendations;
- `test_variants.py` — video-only+audio-only, progressive, audio-only, HDR, DRM, missing sizes;
- `test_youtube_urls.py` — URL-таблица/classify;
- `test_ytdlp_options.py` — `js_runtimes` path, cookies/proxy, `download=False`;
- `test_youtube_thumbnail.py` — fallback/выбор;
- `test_errors.py` — маппинг.

---

## Ручная проверка на реальных данных

Использовать **своё тестовое YouTube-видео**, cookies/proxy из L1.0.

1. Проверить, что `get_info` не создаёт файлов:

```bash
uv run python - <<'PY'
from trackforge.acquire.service import AcquisitionService
# service = ...
source = service.inspect("https://www.youtube.com/watch?v=...")
print(source.kind, source.title, source.creator)
print(source.thumbnail)
print(source.items[0].recommendations)
for v in source.items[0].variants:
    print(v.id, v.label, v.downloadable)
PY
```

Ожидаемо:

- `kind=single`;
- есть thumbnail URL/размеры;
- есть audio/video варианты;
- recommended не `None`;
- в рабочем каталоге не появились `.mp4`/`.m4a`/`.part`.

2. Проверить, что `get_info` не делает `download=True` (monkeypatch/лог).

3. Проверить fallback: видео без maxres не должно падать.

4. Проверить DRM/private URL (если есть) — typed error, не traceback.

---

## Критерии готовности

- [ ] `service.inspect(youtube_url)` возвращает `SourceInfo(kind=single)`.
- [ ] `items[0].variants` содержит video/audio/progressive и корректные селекторы.
- [ ] `thumbnail` и `recommendations.audio/video` заполнены.
- [ ] `get_info` не создаёт медиафайлов.
- [ ] `js_runtimes` содержит путь `deno.find_deno_bin()`.
- [ ] cookies/proxy из L1.0 используются.
- [ ] Ошибки типизированы.
- [ ] `uv run pytest -q` зелёный.
- [ ] Ручной smoke на YouTube-видео успешен.

---

## Что не делать

- Не скачивать.
- Не реализовывать плейлисты (`entries`) — L1.2.
- Не делать поиск/CLI.
- Не добавлять Yandex/SoundCloud.
- Не писать теги.

---

## Передача дальше

Следующий чат получает:

- `YtDlpLoader`, `YtDlpMapper`, `build_variants`, `YouTubeLoader`;
- fixtures одного видео;
- подтверждённый `inspect` single.

Дальше — [L1_2_PLAYLISTS.md](./L1_2_PLAYLISTS.md): flat/full плейлисты.
