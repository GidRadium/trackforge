# L0 — каркас

**Предусловие:** нет.  
**Следующий чекпоинт:** [L1_0_AUTH.md](./L1_0_AUTH.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Ты работаешь над `trackforge` — инструментом получения медиа и последующей работы с метаданными. Сейчас реализуется только **L0: каркас**. Никаких сетевых запросов, никакого реального yt-dlp-извлечения, никакого скачивания.

Ключевые документы:

- архитектура: `docs/INGESTION_ARCHITECTURE.md` — модели, `MediaLoader`, registry, staging, конфиг;
- исследование: `docs/INGESTION_RESEARCH.md` — зачем нужны cookies/proxy/yt-dlp/плагины.

Уже добавлены зависимости в `pyproject.toml`:

```toml
dependencies = [
    "deno>=2.9.8",
    "mutagen>=1.47",
    "typer>=0.15",
    "yt-dlp[default]>=2026.10.21",
    "yt-dlp-yandex-music>=2026.9.24",
]
```

`uv.lock` уже обновлён. L0 может использовать `deno`/`yt-dlp` только для проверки импортов, не для сети.

---

## Цель

Получить тестируемую доменную модель и контракты, на которых дальше строятся все загрузчики.

## Границы

**Входит:**

- `Input`, `SourceRef`, `SourceInfo`, `MediaItem`, `DownloadVariant`, `Recommendations`, `Thumbnail`, `DownloadRequest`, `DownloadResult`;
- ошибки `AcquireError` и базовые наследники;
- `AppConfig`/`SourceProfile` (структура + загрузка TOML/env, без реальной сетевой проверки);
- `ProgressEvent`/`ProgressSink`/`CancellationToken`;
- `LoaderRegistry` с автоматическим перебором и `profile_for`;
- `StagingArea` с плоскими именами и `1/`, `2/` при коллизии;
- `MediaLoader` ABC + `LoaderCapabilities` + `LoaderContext`;
- скелет `YtDlpLoader` (без реального `extract_info`);
- `FakeLoader` для тестов.

**Не входит:**

- реальные запросы к YouTube/Yandex/SoundCloud;
- cookies/proxy gate (это L1.0);
- `inspect` CLI (L1.5);
- скачивание (L2);
- теги/renaming/sidecars (L4).

---

## Задачи

### 1. Зависимости и pytest

- Убедиться, что `uv sync` проходит.
- Добавить маркер:

```toml
[tool.pytest.ini_options]
markers = ["network: real network access"]
```

- Проверить импорты:

```bash
uv run python -c "import deno, yt_dlp, yt_dlp_ejs; print(deno.find_deno_bin())"
```

### 2. Модели

Создать `src/trackforge/acquire/models.py`.

Минимум:

- `InputKind`, `Input`;
- `SourceKind`, `SourceRef`, `UrlHint`;
- `MediaKind`, `StreamRole`, `StreamInfo`;
- `Thumbnail`, `ThumbnailAsset`;
- `MediaUse`, `Recommendations`;
- `DownloadVariant` (включая `selector`, `downloadable`, `requires_merge`);
- `MediaItem` (включая `suggested_stem`, `thumbnail`, `recommendations`, `resolution`);
- `SourceInfo` (items, thumbnails, thumbnail, publisher, recommendations property);
- `SearchCandidate`;
- `CollisionPolicy`, `ItemSelection`, `DownloadOptions`, `DownloadRequest`;
- `DownloadedFile`, `DownloadFailure`, `DownloadResult`;
- `ProgressStage`, `ProgressEvent`, `ProgressSink`, `CancellationToken`.

Ориентир — §3 архитектуры. Не добавлять поля «на будущее» без необходимости; неизвестное — в `extras`.

### 3. Ошибки

Создать `src/trackforge/acquire/errors.py`.

Минимум:

`AcquireError`, `UnsupportedInput`, `UnsupportedScheme`, `UnsupportedUrl`, `AmbiguousInput`, `SearchUnavailable`, `AuthRequired`, `CookieError`, `ProxyError`, `GeoBlocked`, `DrmProtected`, `RateLimited`, `Unavailable`, `NoFormats`, `VariantUnavailable`, `DownloadFailed`, `PostprocessError`, `CancelledError`, `ConfigError`, `SearchNotSupported`.

У каждой ошибки — `retryable` и, где полезно, `hint`.

### 4. Конфиг

Создать `src/trackforge/acquire/config.py`.

- `SourceProfile` с `cookies_file`, `cookies_from_browser`, `proxy`, `impersonate`, `extractor_args`, `retries`, `socket_timeout`, `token` (repr=False), `extra`.
- `AppConfig` с `general`, `download`, `search`, `sources`.
- `profile_for(name)` = `[sources.default] + [sources.<name>]`.
- Загрузка TOML через `tomllib`; env `${VAR}` — только подстановка, без shell.
- Приоритеты: CLI > env > `--config` > user config > defaults.
- Не читать `.trackforge.toml` из cwd автоматически.
- Секреты не попадают в `repr`.

### 5. Прогресс и отмена

Создать `src/trackforge/acquire/progress.py`.

- `ProgressEvent` с `stage`, `job_id`, `item_index/count`, bytes/speed/eta/fragments.
- `ProgressSink` protocol.
- `NullProgressSink`, `RecordingSink` для тестов.
- `CancellationToken` на `threading.Event`, `raise_if_cancelled`.

### 6. Registry

Создать `src/trackforge/acquire/registry.py`.

- `ResolvedLoader(loader, profile)`.
- `LoaderRegistry(loaders, config)`.
- `resolve(url, source=None)` перебирает по `priority`; берёт первый `supports_url`.
- Профиль подставляет автоматически через `profile_for(loader.profile_name())`.
- `is_ready(profile)` проверяется; disabled — skip.
- `--source` — override, проверяет `supports_url`/`is_ready`.
- `by_name`/`by_profile_name`.

### 7. Staging

Создать `src/trackforge/acquire/staging.py`.

- `StagingArea.root`, `job_dir(job_id)`.
- `allocate_target(target, policy=SUBFOLDER)`:
  - `ERROR` → `FileExistsError`;
  - `SKIP` → `None`;
  - `OVERWRITE` → target;
  - `SUBFOLDER` → `1/`, `2/`, ... .
- `commit` и `commit_group(media, sidecars)` — одна подпапка на группу.
- Тесты на flat и коллизии.

### 8. Base loader и FakeLoader

Создать `src/trackforge/acquire/loaders/base.py`.

- `LoaderCapabilities`, `LoaderContext`, `MediaLoader`.
- `supports_url` offline, `classify_url`, `get_info`, `download`.
- `search`/`choose_thumbnail`/`fetch_thumbnail`/`recommend` — default/`SearchNotSupported`.
- `required_plugins`, `config_key`, `profile_name`, `is_ready`.
- `loaders/ytdlp.py` — только скелет с `supports_url` и `NotImplemented`/TODO, без сетевых вызовов.
- `FakeLoader` в `tests/acquire/conftest.py` или `tests/acquire/fakes.py`.

### 9. Тесты

Создать:

- `tests/acquire/test_models.py`;
- `tests/acquire/test_errors.py`;
- `tests/acquire/test_config.py`;
- `tests/acquire/test_registry.py`;
- `tests/acquire/test_staging.py`;
- `tests/acquire/test_fake_loader.py`.

Проверить:

- registry auto-resolve + profile merge;
- `is_ready`/disabled;
- секреты не в repr;
- `SUBFOLDER` даёт `1/`, `2/`, не `(1)`;
- `commit_group` переносит sidecars в ту же папку;
- `FakeLoader` возвращает `SourceInfo` без сети.

---

## Ручная проверка

1. `uv sync`.
2. `uv run pytest -q`.
3. Проверить импорты:

```bash
uv run python - <<'PY'
import deno, yt_dlp, yt_dlp_ejs
print('deno', deno.find_deno_bin())
print('yt-dlp', yt_dlp.version.__version__)
PY
```

4. Прогнать маленький fake-сценарий:

```python
from trackforge.acquire.config import AppConfig
from trackforge.acquire.registry import LoaderRegistry
# FakeLoader из тестов или локальный
```

Ожидаемо: `resolve` возвращает FakeLoader + merged profile; никаких сетевых запросов.

---

## Критерии готовности

- [ ] `uv sync` и `uv run pytest -q` зелёные.
- [ ] Модели созданы и покрыты тестами.
- [ ] `SourceProfile` мержится `default + specific`.
- [ ] `LoaderRegistry.resolve` автоматически выбирает loader и профиль.
- [ ] `StagingArea` делает `1/`, `2/`, sidecars едут вместе.
- [ ] `MediaLoader` не содержит `is_playlist_url`/`is_post`.
- [ ] `FakeLoader` работает без сети.
- [ ] Секреты не попадают в `repr`/JSON.

---

## Что не делать

- Не реализовывать реальный `YtDlpLoader.get_info`.
- Не ходить в сеть.
- Не добавлять CLI `inspect`/`download`.
- Не писать теги/финальные имена.
- Не добавлять БД/историю.

---

## Передача дальше

Следующий чат получает:

- работающие модели/ошибки/конфиг/registry/staging;
- `MediaLoader` + `FakeLoader`;
- тесты L0;
- решение о структуре `SourceInfo` (пока без реальных сайтов).

Дальше — [L1_0_AUTH.md](./L1_0_AUTH.md): cookies/proxy/doctor **до** первого запроса к YouTube.
