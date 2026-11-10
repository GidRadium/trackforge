# L2 — скачивание

**Предусловие:** [L1_5_INSPECT_FREEZE.md](./L1_5_INSPECT_FREEZE.md)  
**Следующий чекпоинт:** [L3_SELECTION_CLI.md](./L3_SELECTION_CLI.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

`SourceInfo v1` зафиксирован, `inspect` работает. Теперь можно скачивать **по явному `variant_id`**, без пользовательского выбора. L2 не пишет теги, не делает финальное имя и не делает ingest.

Ключевые документы:

- `docs/INGESTION_ARCHITECTURE.md` §7 (планирование/download), §7.4 (staging), §7.5 (прогресс), §8 (yt-dlp);
- `docs/INGESTION_RESEARCH.md` §3 (форматы), §7 (прогресс/отмена), §9 (metadata boundary).

---

## Цель

`service.download(DownloadRequest)` скачивает выбранные варианты в staging и возвращает реальные пути + sidecars/ошибки. Никаких тегов и финальных имён.

## Границы

**Входит:**

- `DownloadRequest`/`ItemSelection`/`DownloadOptions`;
- `YtDlpPlanner` + `DownloadPlan`;
- один файл;
- merge/extract audio/remux;
- progress/cancel;
- плейлист: несколько items, partial failures;
- flat staging names + `1/`, `2/`;
- `.info.json`/thumbnail sidecars — опционально;
- cookies/proxy при download.

**Не входит:**

- пользовательский выбор/CLI (L3);
- финальное переименование/теги (L4);
- VK/Pinterest (L5).

---

## Задачи

### 1. `DownloadRequest`/planner

- `DownloadRequest(source_info, selections, options)`.
- `ItemSelection(item_id, variant_id)`.
- `DownloadOptions`:
  - `target_dir`;
  - `staging_name` override;
  - `collision=SUBFOLDER`;
  - `write_thumbnail`, `write_info_json`;
  - `subtitles`, `write_lyrics`;
  - `keep_temp`, `audio_only`, `container`.
- `YtDlpPlanner`:
  - находит item/variant по ID;
  - error `VariantUnavailable`, если нет;
  - `output_stem = safe_stem(options.staging_name or item.suggested_stem or flat_stem(item))`;
  - коллизия внутри плана — `1/`, `2/`;
  - точный `selector` + fallback;
  - проверка `downloadable`.

### 2. Один файл

В `YtDlpLoader.download`:

- для каждого item:
  - `YoutubeDL` opts с `format=selector`, `paths.home=job_dir`, `outtmpl=stem.%(ext)s`;
  - `extract_info(url, download=True)`;
  - `sanitize_info`;
  - реальные пути собрать, не доверять шаблону;
  - `DownloadedFile(item_id, path, item, sidecars)`.
- `noplaylist=True` — элемент уже выбран.
- `cookies/proxy` из профиля.
- не вызывать `MediaFile.save()`.

### 3. Postprocessing

- merge video-only+audio-only (`requires_merge`);
- `FFmpegExtractAudio` для audio-only;
- `FFmpegVideoRemuxer` для контейнера;
- `FFmpegSubtitlesConvertor` для `lrc` — опционально;
- **не** `FFmpegMetadata`/`EmbedThumbnail`;
- проверять ffmpeg до запуска, typed `PostprocessError`.

### 4. Progress/cancel

- `progress_hooks` → `ProgressEvent`:
  - bytes/estimate, speed, eta, fragments;
  - `item_index/count`.
- `postprocessor_hooks` → merge/extract/remux.
- `CancellationToken` → `DownloadCancelled` → `CancelledError`.
- отмена: staging сохранить/почистить по `--keep-temp`.
- не показывать фейковые 100% при неизвестном total.

### 5. Плейлист/несколько файлов

- последовательно по умолчанию;
- один item падает — остальные продолжаются;
- `DownloadResult.files` + `.failures`;
- каждый item — свой stem/подпапка при коллизии;
- sidecars переезжают вместе с медиа.

### 6. Yandex preview guard

После скачивания Yandex:

- `ffprobe` duration;
- сравнить с ожидаемой;
- если 13–30 секунд / `quality=smart_preview` → `PreviewOnly`, не считать успехом.

### 7. Тесты

- `test_download_single.py` — fake `YoutubeDL` + local HTTP server;
- `test_download_multi.py` — один успех, один fail;
- `test_staging_collision.py` — `1/`, sidecars;
- `test_progress.py` — hook mapping/cancel;
- `test_yandex_preview.py` — короткий файл;
- `test_no_tags.py` — после download метаданные не меняются.

---

## Ручная проверка на реальных данных

1. Выбрать `variant_id` из `inspect`:

```python
source = service.inspect(url)
item = source.items[0]
variant = item.recommendations.video or item.variants[0]
request = DownloadRequest(
    source_info=source,
    selections=(ItemSelection(item.id, variant.id),),
    options=DownloadOptions(target_dir=staging_dir),
)
result = service.download(request)
print(result.files, result.failures)
```

Ожидаемо:

- файл в staging;
- расширение соответствует варианту;
- теги не изменены;
- прогресс виден;
- отмена работает.

2. Плейлист: 2–3 item, один недоступен → partial failure.

3. Yandex: проверить duration через `ffprobe`.

4. Проверить, что финальный каталог не тронут; файл только в staging.

---

## Критерии готовности

- [ ] single download работает для audio/video.
- [ ] merge/extract/remux работают; ffmpeg missing → typed error.
- [ ] progress/cancel работают.
- [ ] плейлист partial failures.
- [ ] staging flat, коллизии `1/`, sidecars вместе.
- [ ] Yandex preview guard.
- [ ] никаких тегов/финального имени.
- [ ] `uv run pytest -q` зелёный.
- [ ] ручной smoke на YouTube + Yandex.

---

## Что не делать

- Не делать интерактивный выбор.
- Не переименовывать в финальный путь.
- Не писать теги/обложки.
- Не добавлять БД/архив.
- Не использовать subprocess yt-dlp.

---

## Передача дальше

Следующий чат получает:

- `DownloadRequest`/planner/result;
- staging/progress/cancel;
- partial failures;
- подтверждение скачивания.

Дальше — [L3_SELECTION_CLI.md](./L3_SELECTION_CLI.md).
