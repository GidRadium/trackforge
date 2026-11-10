# L1.5 — `inspect` и фиксация `SourceInfo` v1

**Предусловие:** [L1_4_SITE_AUDIT.md](./L1_4_SITE_AUDIT.md)  
**Следующий чекпоинт:** [L2_DOWNLOAD.md](./L2_DOWNLOAD.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Это последний чекпоинт L1. Уже работают YouTube, Yandex Music, SoundCloud, Vimeo/Bandcamp, плейлисты, cookies/proxy. Теперь нужно:

1. Дать единый `inspect` CLI/API.
2. Зафиксировать `SourceInfo` v1 по результатам аудита.
3. **Не начинать L2**, пока модель не заморожена.

**Не делать:** скачивание, пользовательский выбор, поиск, теги.

---

## Цель

`tfg inspect URL --json` и `AcquisitionService.inspect` показывают все данные, которые нужны для будущего выбора и скачивания, без скачивания медиа.

---

## Задачи

### 1. `AcquisitionService.inspect`

```python
def inspect(
    self,
    url: str,
    *,
    flat: bool = False,
    limit: int | None = None,
    source: str | None = None,
) -> SourceInfo:
    resolved = self._registry.resolve(url, source=source)
    ctx = self._context_for(resolved)
    return resolved.loader.get_info(SourceRef(...), ctx=ctx, flat=flat, limit=limit)
```

Требования:

- сам resolve/loader/profile;
- ошибки типизированы;
- никаких вопросов пользователю;
- никакого download;
- возвращает `SourceInfo`.

### 2. CLI `tfg inspect`

```bash
tfg inspect "https://www.youtube.com/watch?v=..." --json
tfg inspect "https://www.youtube.com/playlist?list=..." --flat --limit 20 --json
tfg inspect "https://music.yandex.ru/album/.../track/..." --json
tfg inspect "https://soundcloud.com/.../sets/..." --flat --limit 20 --json
```

- `--json` — машинный вывод;
- без `--json` — человекочитаемо: source/kind/title/creator/thumbnail/варианты;
- `--flat`, `--limit`, `--source`;
- коды выхода: 0 успех, 1 ошибка, 2 неверные аргументы;
- никаких интерактивных вопросов.

### 3. JSON-схема

`schema_version: 1`:

```json
{
  "schema_version": 1,
  "source": "youtube",
  "kind": "single",
  "title": "...",
  "creator": "...",
  "thumbnail": {"url": "...", "width": 1280, "height": 720},
  "items": [
    {
      "id": "...",
      "title": "...",
      "page_url": "...",
      "resolution": "full",
      "thumbnail": {"url": "...", "width": 1280, "height": 720},
      "recommendations": {"audio": "...", "video": "..."},
      "variants": [
        {"id": "...", "label": "...", "kind": "video", "container": "mp4",
         "height": 1080, "filesize": 48000000, "requires_merge": true}
      ]
    }
  ],
  "warnings": []
}
```

Требования:

- без `http_headers`, cookies, tokens, raw `info_dict`;
- `thumbnail` — URL/размеры, не байты;
- `variants` — без подписанных URL;
- для flat — `variants=[]`, `resolution="flat"`;
- стабильные имена полей.

### 4. Фиксация модели

- Прогнать все fixtures через mapper.
- Проверить audit: все обязательные поля покрыты.
- Новые поля — только `extras` или новая версия.
- Добавить `schema_version` в JSON/модель (или в сериализатор).
- Записать в `docs/INGESTION_ARCHITECTURE.md`/`docs/INGESTION_SITE_AUDIT.md` решение: `SourceInfo v1 frozen`.
- С этого момента L2 может использовать модель.

### 5. Тесты

- `test_inspect_service.py` — single/collection/flat/limit/source;
- `test_inspect_json.py` — схема, отсутствие секретов, `schema_version`;
- `test_inspect_cli.py` — `CliRunner`, коды выхода;
- `test_model_freeze.py` — имена/набор полей v1, неизвестные поля в extras.

---

## Ручная проверка на реальных данных

1. YouTube single:

```bash
uv run tfg inspect "https://www.youtube.com/watch?v=..." --json | jq .
```

Проверить:

- `kind=single`;
- thumbnail;
- recommendations;
- variants;
- нет файлов в рабочем каталоге.

2. YouTube playlist:

```bash
uv run tfg inspect "https://www.youtube.com/playlist?list=..." --flat --limit 10 --json | jq '.items | length'
```

Ожидаемо `<= 10`.

3. Yandex track/album, SoundCloud track/set — по одному URL.

4. Проверить, что `--json` не содержит cookies/tokens/headers:

```bash
uv run tfg inspect ... --json | grep -Ei "cookie|token|authorization|proxy" || true
```

5. Проверить, что после inspect нет `.part`/`.mp4`/`.m4a`.

---

## Критерии готовности

- [ ] `AcquisitionService.inspect` работает.
- [ ] `tfg inspect` работает с `--json`/`--flat`/`--limit`.
- [ ] JSON соответствует схеме v1.
- [ ] Нет секретов/подписанных URL.
- [ ] Все fixtures проходят mapper.
- [ ] Audit закрыт; `SourceInfo v1` зафиксирован.
- [ ] `uv run pytest -q` зелёный.
- [ ] Ручной smoke на YouTube/Yandex/SoundCloud успешен.

**L2 не начинается, пока модель не зафиксирована.**

---

## Что не делать

- Не скачивать.
- Не делать интерактивный выбор.
- Не добавлять поиск.
- Не менять поля после freeze без версии.
- Не писать теги.

---

## Передача дальше

Следующий чат получает:

- `inspect` API/CLI;
- `SourceInfo v1` + schema;
- fixtures/audit;
- подтверждение, что данные с сайтов стабильны.

Дальше — [L2_DOWNLOAD.md](./L2_DOWNLOAD.md).
