# L1.2 — плейлисты

**Предусловие:** [L1_1_YTDLP_SINGLE.md](./L1_1_YTDLP_SINGLE.md)  
**Следующий чекпоинт:** [L1_3_YANDEX_SOUNDCLOUD.md](./L1_3_YANDEX_SOUNDCLOUD.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Уже есть `YtDlpLoader`/`YtDlpMapper`/`build_variants`/`YouTubeLoader` для одного видео. Теперь нужны коллекции: YouTube playlist, а также базовая flat-механика для будущих Yandex/SoundCloud.

Ключевые документы:

- `docs/INGESTION_ARCHITECTURE.md` §2 (двухстадийность), §4.3 (`_type`), §7.2;
- `docs/INGESTION_RESEARCH.md` §2.3 (`playlist`/`multi_video`), §4.1.

**Не делать:** скачивание, пользовательский выбор, Yandex/SoundCloud, поиск.

---

## Цель

Научиться получать коллекцию **без разворачивания всех элементов**:

- `get_info(url, flat=True, limit=50)` → `SourceInfo(kind=COLLECTION)`, items с `resolution=FLAT`;
- полный `get_info(item.page_url)` → `SINGLE` с вариантами;
- лимиты и пагинация;
- не делать сотни запросов до первого вопроса пользователю.

---

## Задачи

### 1. Определение коллекции

В `YtDlpMapper`:

- `_type=playlist` → `kind=COLLECTION`;
- `_type=multi_video` → пока `COLLECTION`/`POST`? В этом чекпоинте считать `COLLECTION`; отдельный POST — L5;
- `_type=video` → `SINGLE`;
- `_type=url`/`url_transparent` — развернуть или `UnresolvedRedirect`;
- entries могут быть `None`/ленивыми; не делать `list()` без лимита.

### 2. Flat-режим

`YtDlpLoader.get_info(..., flat=True, limit=...)`:

- `extract_flat="in_playlist"`;
- `playlist_items=f"1:{limit}"` при `limit`;
- `_entries` → items;
- каждый item:
  - `id`, `title`, `page_url` (`webpage_url`/`url`), `position` (1-based), `creator`/`uploader`;
  - `resolution=FLAT`;
  - `variants=()`;
  - `thumbnail` из доступных thumbnails;
- `SourceInfo.kind=COLLECTION`, `title`/`creator`/`description` коллекции.

### 3. Full resolve элемента

`YouTubeLoader.get_info`/`YtDlpLoader.get_info` без flat:

- `noplaylist=True` (не тянуть весь список);
- `page_url` элемента → `SINGLE` с вариантами;
- если `page_url` нет — `VariantUnavailable`/`UnresolvedEntry`.

### 4. `YouTubeLoader.classify_url`

- `/playlist?list=` → `COLLECTION`;
- `watch?v=...&list=...` → `COLLECTION` (но `--no-playlist` оставить на L3);
- `youtu.be/...` → `SINGLE`;
- остальное `UNKNOWN`.

### 5. Лимиты

- default `limit=50` для flat;
- `limit=0`/`None` — не тянуть всё; максимум конфигурируемый;
- `playlist_items` — строка `1:N`;
- для больших коллекций не делать full resolve всех items.

### 6. Тесты

Fixtures:

- `tests/acquire/fixtures/youtube_playlist_flat.info.json`;
- `tests/acquire/fixtures/youtube_playlist_full.info.json`.

Тесты:

- `test_playlist_flat.py`:
  - `kind=COLLECTION`;
  - `len(items) <= limit`;
  - `resolution=FLAT`;
  - `variants` пустые;
  - `page_url` заполнен;
- `test_playlist_item.py`:
  - `inspect(item.page_url)` → `SINGLE` + variants;
- `test_playlist_limits.py` — `playlist_items`/limit;
- `test_playlist_errors.py` — пустой/приватный плейлист.

---

## Ручная проверка на реальных данных

1. YouTube playlist:

```bash
uv run python - <<'PY'
from trackforge.acquire.service import AcquisitionService
# service = ...
collection = service.inspect("https://www.youtube.com/playlist?list=...", flat=True, limit=10)
print(collection.kind, collection.title, len(collection.items))
for item in collection.items[:3]:
    print(item.position, item.page_url, item.resolution, item.thumbnail)
PY
```

Ожидаемо:

- `kind=collection`;
- не больше 10 items;
- `resolution=flat`, variants пустые;
- нет скачанных файлов;
- запрос быстрый (без full resolve).

2. Выбранный item:

```python
full = service.inspect(collection.items[0].page_url)
print(full.kind, full.items[0].variants)
```

Ожидаемо: `single`, варианты есть.

3. Приватный/пустой плейлист — typed error, не traceback.

---

## Критерии готовности

- [ ] `flat=True` возвращает коллекцию с лимитом.
- [ ] Flat-элементы имеют `page_url`, `position`, `resolution=FLAT`.
- [ ] Flat не содержит variants и не делает full resolve.
- [ ] `inspect(item.page_url)` даёт `SINGLE` с вариантами.
- [ ] `watch?v=...&list=...` классифицируется как коллекция.
- [ ] `kind`/`resolution` корректны.
- [ ] `uv run pytest -q` зелёный.
- [ ] Ручной smoke на YouTube playlist успешен.

---

## Что не делать

- Не разворачивать весь плейлист.
- Не скачивать.
- Не делать UI выбора.
- Не добавлять Yandex/SoundCloud.
- Не писать `SourceInfo` v1 (это L1.5).

---

## Передача дальше

Следующий чат получает:

- flat/full коллекции;
- fixtures;
- понимание, какие поля даёт YouTube playlist.

Дальше — [L1_3_YANDEX_SOUNDCLOUD.md](./L1_3_YANDEX_SOUNDCLOUD.md).
