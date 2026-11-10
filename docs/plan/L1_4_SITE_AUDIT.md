# L1.4 — расширение сайтов и аудит полей

**Предусловие:** [L1_3_YANDEX_SOUNDCLOUD.md](./L1_3_YANDEX_SOUNDCLOUD.md)  
**Следующий чекпоинт:** [L1_5_INSPECT_FREEZE.md](./L1_5_INSPECT_FREEZE.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Уже есть YouTube, Yandex Music, SoundCloud, `YtDlpLoader`, плейлисты. Теперь цель — **не «поддержать всё», а понять, что отдают разные сайты**, до фиксации модели и до скачивания.

Добавляем:

- Vimeo — видео/плейлисты;
- Bandcamp — трек/альбом;
- Pinterest и VK — **probes**, чтобы увидеть posts/images, без полноценных загрузчиков.

**Не делать:** скачивание, поиск, полноценные VK/Pinterest loaders, `SourceInfo` v1.

---

## Цель

Заполнить `docs/INGESTION_SITE_AUDIT.md` матрицей «сайт × поле × формат × ограничение» и собрать sanitized fixtures. Понять, каких полей/типов не хватает в `SourceInfo`.

---

## Задачи

### 1. Vimeo

- URL: видео, плейлисты/альбомы;
- через `YtDlpLoader` (отдельный класс нужен только если есть site-specific quirks);
- проверить:
  - `_type=video`/`playlist`;
  - `formats` (HLS/DASH/progressive);
  - `thumbnails`;
  - `duration`, `uploader`, `release_date`;
  - плейлист flat/full;
  - cookies/прокси (если нужно).

Добавить `VimeoLoader` только если без него нельзя:

- URL-паттерн/приоритет;
- cookies/proxy defaults;
- нормализация.

Иначе достаточно generic + fixture.

### 2. Bandcamp

- URL: track/album;
- проверить:
  - `track`/`album`/`artists`;
  - `track_number`, `release_date`;
  - `formats` (MP3/FLAC, если отдаёт);
  - `thumbnails`/cover;
  - `description`;
  - `_type=playlist` для альбома.
- cookies обычно не нужны.

### 3. Pinterest probe

Не делать полноценный loader. Нужно понять:

- pin → `_type`/`formats`/`images`;
- image-only pin: какие поля есть, чего не хватает;
- board → collection;
- thumbnail sizes;
- posts с текстом.

Записать в audit: «нужен ли `MediaKind.IMAGE`/`POST` и какие поля».

### 4. VK probe

Не делать `VkApiLoader` (L5). Проверить:

- wall post → `_type=playlist`/`multi_video`;
- audio/video attachments;
- какие поля приходят;
- что не покрывается `SourceInfo`.

Записать ограничения.

### 5. `docs/INGESTION_SITE_AUDIT.md`

Создать документ с таблицами:

```text
| Сайт | URL-тип | SourceInfo.kind | items | variants | thumbnail | auth | ограничения |
```

И отдельно:

- поля, которые есть у большинства;
- поля, которые есть только у одного сайта;
- поля, которых не хватает в модели;
- что уходит в `extras`;
- какие `MediaKind` реально нужны;
- какие fixtures собраны.

### 6. Fixtures

Sanitized JSON без cookies/токенов:

- `tests/acquire/fixtures/vimeo_video.info.json`;
- `vimeo_playlist.info.json`;
- `bandcamp_track.info.json`;
- `bandcamp_album.info.json`;
- `pinterest_pin.info.json` (probe);
- `vk_wall.info.json` (probe).

Если сайт отдаёт слишком большой JSON — сохранить только используемые поля + `_type`/`formats`/`thumbnails`.

### 7. Тесты

- `test_site_fixtures.py` — все fixtures проходят `YtDlpMapper` без исключений;
- `test_vimeo.py` — single/collection;
- `test_bandcamp.py` — track/album;
- `test_probe_kinds.py` — Pinterest/VK дают ожидаемый `kind`/`resolution`;
- `test_unknown_fields.py` — неизвестные поля не роняют mapper.

---

## Ручная проверка на реальных данных

Для каждого сайта — один публичный URL:

```python
source = service.inspect(url)
print(source.source, source.kind, source.title)
print(source.thumbnail)
print(source.items[0].variants if source.items else None)
```

Проверить:

- Vimeo video/playlist;
- Bandcamp track/album;
- Pinterest image-only pin;
- VK wall post с аудио/видео.

Записать в audit:

- что пришло;
- что не пришло;
- какие ошибки;
- нужен ли отдельный loader.

**Не скачивать медиа.** Если yt-dlp делает запросы к манифестам — это нормально; байты файла — нет.

---

## Критерии готовности

- [ ] `docs/INGESTION_SITE_AUDIT.md` заполнен.
- [ ] Vimeo/Bandcamp дают `SourceInfo` (через generic или лёгкий loader).
- [ ] Pinterest/VK probes описаны; понятно, чего не хватает модели.
- [ ] Fixtures добавлены и проходят mapper.
- [ ] Неизвестные поля не роняют `SourceInfo`.
- [ ] `uv run pytest -q` зелёный.
- [ ] Ручной smoke на 4 сайтах выполнен.

---

## Что не делать

- Не делать полный `VkApiLoader`/`PinterestLoader`.
- Не скачивать.
- Не добавлять поля «на всякий случай»; сначала audit.
- Не фиксировать `SourceInfo` v1.

---

## Передача дальше

Следующий чат получает:

- audit-матрицу;
- fixtures Vimeo/Bandcamp/Pinterest/VK;
- список недостающих полей/типов.

Дальше — [L1_5_INSPECT_FREEZE.md](./L1_5_INSPECT_FREEZE.md): `inspect` и фиксация модели.
