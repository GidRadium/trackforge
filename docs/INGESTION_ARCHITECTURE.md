# Архитектура получения медиа (input → download → ingest)

**Статус:** L1 завершён; `SourceInfo v1` frozen (2026-11-10)

**Дата:** 2026-11-09  
**Связанные документы:** [IDEA.md](./IDEA.md), [METADATA_RESEARCH.md](./METADATA_RESEARCH.md), [INGESTION_RESEARCH.md](./INGESTION_RESEARCH.md), [INGESTION_IMPLEMENTATION_PLAN.md](./INGESTION_IMPLEMENTATION_PLAN.md)

---

## 0. Решения коротко

| № | Решение | Почему |
|---|---|---|
| 1 | yt-dlp — **библиотека**, не subprocess | структурированный `info_dict`, hooks, отмена, per-source настройки, нет парсинга stdout |
| 2 | Пять слоёв: input → search/resolve → selection → download → ingest | каждый слой тестируется отдельно; загрузчик не знает про теги |
| 3 | База: `MediaLoader`; универсальный `YtDlpLoader`; сайтовые наследники; `VkApiLoader` от базы | повторяет предложенную иерархию, но без «божественного» класса |
| 4 | `SourceInfo` — плоский: `items`, а не рекурсивное дерево | плейлист, пост и одиночное медиа описываются одним объектом |
| 5 | `classify_url() -> UrlHint` вместо отдельных `is_playlist_url`/`is_post` | многие URL неоднозначны без сети; окончательно решает `get_info` |
| 6 | Загрузчик **не пишет теги и не встраивает обложки** | единственный писатель — существующий `trackforge.metadata` |
| 7 | Куки/прокси/токены — per-source `SourceProfile` | разные источники требуют разного; глобальные настройки опасны |
| 8 | Плейлисты — двухстадийно: flat-список → полный resolve выбранных | иначе сотни запросов до первого вопроса пользователю |
| 9 | Скачивание в staging + атомарный перенос | нет частичных финальных файлов; можно безопасно отменить |
| 10 | Никакой БД, истории и индекса | инструмент, а не медиасервер; только TOML, temp и in-memory cache |
| 11 | Поиск, thumbnail и recommended-варианты — методы `MediaLoader` | source-specific: YouTube/Yandex/VK сами знают, как искать, проверять ссылки и выбирать картинку |

### Изменения по итогам обсуждения

- `LoaderRegistry.resolve(url)` сам перебирает loaders и сам подставляет `SourceProfile` по `config_key`; `--source` — только override.
- Поиск перенесён в `MediaLoader.search()`; `SearchService` остался только оркестратором.
- Thumbnail выбирает `MediaLoader.choose_thumbnail()` (с site-specific fallback), а `fetch_thumbnail()` отдаёт байты для ботов.
- `is_playlist_url`/`is_post` удалены; остаётся только `classify_url()`.
- Добавлены `Recommendations` (`audio`/`video`/`image`/`document`) и `MediaLoader.recommend()`.
- Добавлен сценарий встраивания в Telegram-бота без бота в модуле.

### Минимальное ядро

Чтобы архитектура не разрослась, в MVP достаточно небольшого набора типов:

`Input` → `SourceInfo` (`MediaItem` + `DownloadVariant`) → `MediaLoader` → `LoaderRegistry` → `DownloadRequest` → `ProgressSink` → `DownloadResult` → `IngestPipeline`.

Всё остальное в документе — расширения и уточнения:

- `SearchService` нужен только для текстового ввода; он лишь опрашивает `MediaLoader.search()`;
- `LocalSourceReader` — только для локальных путей;
- `VkApiLoader`, `DirectAssetLoader`, `PinterestLoader` — отдельные источники;
- `Claim`/`MetadataProvider` — ingest-слой, не загрузчик;
- `Analyzer`/LLM — будущее, не блокирует MVP.

---

## 1. Цели и не-цели

### Цели

- Один пользовательский ввод: URL, текстовый запрос, локальный файл/папка.
- Модуль пригоден для встраивания (Telegram-бот, веб-сервис): без CLI-зависимостей, с JSON-моделью, thumbnail-байтами и progress-callback.
- Для текста — список кандидатов, выбор, получение URL.
- Для URL — название, автор/канал/публикатор, превью, варианты качества/формата.
- Скачивание с прогрессом и отменой.
- Расширяемые источники: yt-dlp, кастомные API (VK), прямые файлы/картинки.
- Куки, прокси, токены, лимиты — в конфиге на источник.
- После скачивания — передача файла и site-metadata в ingest-пайплайн TrackForge.

### Не-цели

- Медиасервер, библиотека, история загрузок, база данных.
- Обход DRM, платного доступа, приватности и ограничений сайтов.
- Полноценный редактор изображений/документов.
- LLM-анализ как обязательная часть MVP (оставляем протокол `Analyzer` на будущее).
- GUI/веб-интерфейс и Telegram-бот (архитектура должна позволять встраивание, но бот не входит в модуль).

---

## 2. Общий конвейер

```text
                         ┌──────────────────────┐
raw input ──────────────▶│    InputResolver     │
                         └──────────┬───────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        URL (http/https)       TEXT (query)          LOCAL (path)
              │                     │                     │
              ▼                     ▼                     ▼
   LoaderRegistry.resolve     SearchService       LocalSourceReader
              │                     │                     │
              ▼                     ▼                     │
        get_info(url)         SearchCandidate[]          │
              │                     │                     │
              └──────────┬──────────┘                     │
                         ▼                                │
                    SourceInfo ◀──────────────────────────┘
                         │
                         ▼
                  Selection / UI
                         │
                         ▼
                   DownloadPlan
                         │
                         ▼
             MediaLoader.download(...)
                         │
                         ▼
              staging + progress/cancel
                         │
                         ▼
                  DownloadResult
                         │
                         ▼
                 IngestPipeline
                         │
                         ▼
        final media + tags + .lrc/.elrc + artwork
```

Три сценария:

1. **URL**: `InputResolver` → `LoaderRegistry.resolve` → `get_info` → `SourceInfo` → выбор варианта → `download` → `ingest`.
2. **Текст**: `InputResolver` → `SearchService` (опрашивает `MediaLoader.search`) → `SearchCandidate[]` → выбор кандидата (URL) → дальше как URL.
3. **Локальный путь**: `InputResolver` → `LocalSourceReader` → `SourceInfo` с `variants = original` → `download` пропускается → `ingest`.

Важно: «resolve» (получить `SourceInfo`) и «download» (получить файл) — разные операции. `download` не должен повторно спрашивать пользователя и не должен сам менять выбранный вариант.

---

## 3. Доменная модель

Модель — это value objects без зависимости от yt-dlp. Специфика источника остаётся в адаптере.

### 3.1. Ввод и источник

```python
from enum import StrEnum
from pathlib import Path

class InputKind(StrEnum):
    URL = "url"
    TEXT = "text"
    LOCAL_FILE = "local_file"
    LOCAL_DIR = "local_dir"

class SourceKind(StrEnum):
    SINGLE = "single"        # одно медиа
    POST = "post"            # текст + одно/несколько медиа
    COLLECTION = "collection"  # плейлист/альбом/доска/канал

class UrlHint(StrEnum):
    SINGLE = "single"
    POST = "post"
    COLLECTION = "collection"
    UNKNOWN = "unknown"      # без сети определить нельзя

@dataclass(frozen=True, slots=True)
class Input:
    raw: str
    kind: InputKind
    url: str | None = None
    query: str | None = None
    paths: tuple[Path, ...] = ()

@dataclass(frozen=True, slots=True)
class SourceRef:
    """Ссылка на источник, которую можно передать в download()."""
    source: str          # имя loader'а: youtube, yandex_music, vk, direct
    url: str
    id: str | None = None
```

`InputResolver`:

```python
def resolve(raw: str) -> Input:
    value = raw.strip()
    if value.startswith(("http://", "https://")):
        return Input(raw=value, kind=InputKind.URL, url=value)
    path = Path(value).expanduser()
    if path.exists():
        kind = InputKind.LOCAL_DIR if path.is_dir() else InputKind.LOCAL_FILE
        return Input(raw=value, kind=kind, paths=(path,))
    if "://" in value:
        raise UnsupportedScheme(value)
    return Input(raw=value, kind=InputKind.TEXT, query=value)
```

Правила:

- `file://` не поддерживаем по умолчанию (только `--allow-file-urls`).
- Если строка похожа на существующий путь — это путь, даже если в ней есть `-`.
- Иначе — текстовый запрос. Это делает `tfg 'Автор - Название'` безопасным.
- Несколько URL/путей можно поддержать позже; MVP — один ввод.

### 3.2. Медиа и варианты

```python
class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    AUDIO_VIDEO = "audio_video"
    IMAGE = "image"
    ANIMATED_IMAGE = "animated_image"
    DOCUMENT = "document"
    UNKNOWN = "unknown"

class StreamRole(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"

@dataclass(frozen=True, slots=True)
class Thumbnail:
    url: str
    width: int | None = None
    height: int | None = None
    ext: str | None = None
    mime: str | None = None
    filesize: int | None = None
    preference: int = 0
    source: str | None = None          # например, "youtube:maxres"
    http_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

@dataclass(frozen=True, slots=True)
class ThumbnailAsset:
    """Байты картинки — то, что нужно Telegram-боту или preview."""
    data: bytes
    mime: str
    width: int | None = None
    height: int | None = None

@dataclass(frozen=True, slots=True)
class StreamInfo:
    role: StreamRole
    format_id: str | None = None
    codec: str | None = None
    ext: str | None = None
    url: str | None = field(default=None, repr=False)  # подписанный/временный
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    language: str | None = None
    dynamic_range: str | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    protocol: str | None = None
    drm: bool = False

@dataclass(frozen=True, slots=True)
class DownloadVariant:
    """То, что видит пользователь: один выходной файл."""
    id: str
    label: str
    kind: MediaKind
    container: str | None
    streams: tuple[StreamInfo, ...]
    selector: str | None = None          # yt-dlp format expression
    filesize: int | None = None
    filesize_approx: int | None = None
    requires_merge: bool = False
    requires_ffmpeg: bool = False
    downloadable: bool = True
    unavailable_reason: str | None = None

    @property
    def size_label(self) -> str:
        size = self.filesize or self.filesize_approx
        if size is None:
            return "?"
        prefix = "" if self.filesize else "~"
        return f"{prefix}{size / 1_000_000:.1f} MB"

class ResolutionState(StrEnum):
    FULL = "full"
    FLAT = "flat"            # есть метаданные, нет форматов
    UNRESOLVED = "unresolved"

class MediaUse(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"
    DOCUMENT = "document"

@dataclass(frozen=True, slots=True)
class Recommendations:
    """variant_id, которые loader считает лучшими для каждого сценария."""
    audio: str | None = None
    video: str | None = None
    image: str | None = None
    document: str | None = None

    def for_use(self, use: MediaUse) -> str | None:
        return getattr(self, use.value)

@dataclass(frozen=True, slots=True)
class MediaItem:
    id: str
    title: str
    suggested_stem: str | None = None   # что предлагает источник/loader
    kind: MediaKind = MediaKind.UNKNOWN
    creator: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    album_artists: tuple[str, ...] = ()
    duration: float | None = None
    release_date: PartialDate | None = None
    page_url: str | None = None
    thumbnails: tuple[Thumbnail, ...] = ()
    thumbnail: Thumbnail | None = None
    variants: tuple[DownloadVariant, ...] = ()
    recommendations: Recommendations = field(default_factory=Recommendations)
    resolution: ResolutionState = ResolutionState.FULL
    position: int | None = None
    group_id: str | None = None
    extras: Mapping[str, object] = field(default_factory=dict, repr=False)
```

`SourceInfo`:

```python
@dataclass(frozen=True, slots=True)
class SourceInfo:
    source: str
    url: str
    kind: SourceKind
    id: str | None
    title: str | None
    creator: str | None
    description: str | None
    thumbnails: tuple[Thumbnail, ...]
    items: tuple[MediaItem, ...]
    warnings: tuple[str, ...] = ()
    thumbnail: Thumbnail | None = None
    # publisher/label — только если сайт явно его отдаёт; uploader != publisher.
    publisher: str | None = None

    def ref(self) -> SourceRef:
        return SourceRef(self.source, self.url, self.id)

    @property
    def recommendations(self) -> Recommendations:
        return self.items[0].recommendations if self.items else Recommendations()
```

Почему так:

- **Одно медиа** — `kind=SINGLE`, `items=(один,)`.
- **Пост** — `kind=POST`, `description` — текст, `items` — все медиа; у них общий `group_id`.
- **Плейлист** — `kind=COLLECTION`, `items` — элементы с `position`.
- Не нужна рекурсивная структура `Playlist(entries=[Playlist...])`; вложенные плейлисты разворачиваются в плоский список с `parent_id` в `extras`, если понадобится.
- `raw`/`info_dict` **не входит** в публичную модель. Адаптер держит его внутри; наружу — только нормализованные данные. Это защищает от гигантских словарей и изменения схемы yt-dlp.

> `PartialDate` переиспользуем из `trackforge.metadata`, чтобы не плодить второй тип даты.

### 3.2.1. `SourceInfo` v1 — зафиксировано

**Дата фиксации:** 2026-11-10.  **`schema_version`: 1.**

Публичный JSON-контракт реализован в
`trackforge.acquire.serialization` (`source_info_to_dict`,
`source_info_to_json`); у `SourceInfo` есть `schema_version`, `to_dict()` и
`to_json()`.  Для машинного вывода CLI используется только этот
сериализатор, а не `dataclasses.asdict` и не сырой `info_dict`.

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

Правила v1:

- набор полей на всех трёх уровнях фиксирован; меняется только вместе с
  `schema_version`;
- `kind` — `single`/`post`/`collection`; `resolution` — `full`/`flat`/
  `unresolved`; `variant.kind` — значение `MediaKind`;
- для flat-режима каждый item имеет `resolution: "flat"` и `variants: []`;
  список items при этом ограничен `--limit`/настройкой источника;
- `variants` не содержат подписанных media URL, `format_id`-селекторов,
  `http_headers` и потоков; для скачивания L2 переизвлекает формат по
  `variant.id`/`item.page_url`;
- `thumbnail` — только URL и размеры, без байтов и заголовков;
- `recommendations` в v1 — только `audio` и `video`;
  `image`/`document` появятся в новой версии вместе с L5;
- `filesize` — точный размер, если источник его дал, иначе приблизительный
  (`filesize_approx`); отдельного признака точности в v1 нет;
- **никогда не сериализуются:** `cookies`, `http_headers`, заголовки
  авторизации, токены, proxy, `extras`, `subtitles`, `StreamInfo.url`,
  `DownloadVariant.selector`, сырой `info_dict`;
- новые source-specific данные сначала попадают только в
  `MediaItem.extras`; чтобы вывести их в JSON, нужен `schema_version: 2`.

Публичный API:

```python
info = AcquisitionService(...).inspect(url, flat=False, limit=None, source=None)
payload = info.to_dict()       # schema_version == 1
text = info.to_json()          # тот же объект, ensure_ascii=False
```

CLI: `tfg inspect URL [--json] [--flat] [--limit N] [--source NAME]`.
Коды выхода: `0` — успех, `1` — ошибка получения/конфигурации, `2` —
неверные аргументы.  Команда не задаёт вопросов и не скачивает медиа.

### 3.3. Поиск

```python
@dataclass(frozen=True, slots=True)
class SearchCandidate:
    source: str
    id: str
    url: str
    title: str
    creator: str | None = None
    duration: float | None = None
    thumbnails: tuple[Thumbnail, ...] = ()
    thumbnail: Thumbnail | None = None
    published_at: PartialDate | None = None
    score: float = 0.0
```

`SearchCandidate` — лёгкий: без `formats`. После выбора вызывается `get_info(candidate.url)`.

### 3.4. План скачивания и результат

```python
class CollisionPolicy(StrEnum):
    ERROR = "error"          # упасть, ничего не перезаписывать
    SKIP = "skip"            # пропустить файл
    SUBFOLDER = "subfolder"  # положить в 1/, 2/, ... рядом
    OVERWRITE = "overwrite"  # заменить

@dataclass(frozen=True, slots=True)
class ItemSelection:
    item_id: str
    variant_id: str
    # для будущего: audio_variant_id, start/end, отдельные субтитры

@dataclass(frozen=True, slots=True)
class DownloadOptions:
    target_dir: Path
    staging_name: str | None = None       # override для имени в staging
    collision: CollisionPolicy = CollisionPolicy.SUBFOLDER
    write_thumbnail: bool = True
    write_info_json: bool = False
    subtitles: tuple[str, ...] = ()
    subtitles_to_lrc: bool = False
    write_lyrics: bool = True
    keep_temp: bool = False
    audio_only: bool = False
    container: str | None = None

@dataclass(frozen=True, slots=True)
class DownloadRequest:
    source_info: SourceInfo
    selections: tuple[ItemSelection, ...]
    options: DownloadOptions

    @property
    def source(self) -> SourceRef:
        return self.source_info.ref()

@dataclass(frozen=True, slots=True)
class DownloadedFile:
    item_id: str
    path: Path
    item: MediaItem
    sidecars: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class DownloadFailure:
    item_id: str
    message: str
    retryable: bool = False

@dataclass(frozen=True, slots=True)
class DownloadResult:
    files: tuple[DownloadedFile, ...]
    failures: tuple[DownloadFailure, ...] = ()
    warnings: tuple[str, ...] = ()
```

`DownloadRequest` заменяет условный `download(data, chosen_variant)`:

- `source_info` — уже разрешённый источник (варианты и метаданные на момент выбора);
- `selections` — что именно (несколько элементов плейлиста/поста);
- `options` — как (каталог, имена, sidecars, перезапись).

`DownloadRequest` хранит снимок `SourceInfo`, а не только URL: загрузчику нужны выбранные `DownloadVariant`/`selector`. Сам медиа-URL yt-dlp всё равно переизвлечёт при скачивании, поэтому устаревшие подписанные ссылки не проблема; устаревший `format_id` даст `VariantUnavailable`.

Это позволяет одному вызову скачать 20 треков, не теряя прогресс и ошибки по каждому.

### 3.5. Прогресс и отмена

```python
class ProgressStage(StrEnum):
    RESOLVE = "resolve"
    SEARCH = "search"
    DOWNLOAD = "download"
    MERGE = "merge"
    EXTRACT_AUDIO = "extract_audio"
    REMUX = "remux"
    CONVERT = "convert"
    SUBTITLES = "subtitles"
    TAG = "tag"
    LYRICS = "lyrics"
    MOVE = "move"
    DONE = "done"

@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: ProgressStage
    job_id: str
    item_index: int | None = None
    item_count: int | None = None
    message: str | None = None
    filename: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: float | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None

class ProgressSink(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...

class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError()
```

Реализации: `RichProgressSink`, `JsonLinesSink`, `NullProgressSink`, `RecordingSink` (тесты).

### 3.5. Публичный API (в т.ч. для бота)

```python
class AcquisitionService:
    def search(self, query: str, *, limit: int = 10, source: str | None = None) -> list[SearchCandidate]: ...
    def inspect(self, url: str, *, flat: bool = False, limit: int | None = None, source: str | None = None) -> SourceInfo: ...
    def download(self, request: DownloadRequest, *, on_progress: ProgressSink | None = None) -> DownloadResult: ...
    def fetch_thumbnail(self, thumbnail: Thumbnail, *, max_bytes: int | None = None) -> ThumbnailAsset | None: ...
```

- `inspect` сам вызывает `LoaderRegistry.resolve(url)` и подставляет профиль; вызывающему не нужно знать про loader.
- `SourceInfo.thumbnail` / `MediaItem.thumbnail` уже заполнены; `SearchCandidate.thumbnail` — тоже.
- `SourceInfo.recommendations` (свойство) отдаёт рекомендации первого item; для поста/плейлиста смотрите `items[i].recommendations`.
- `download` блокирующий; для async-бота — `asyncio.to_thread`.
- `fetch_thumbnail` не требует CLI и возвращает байты; `SourceInfo`/`MediaItem` сериализуемы без секретов.

---

## 4. Иерархия загрузчиков

### 4.1. Базовый класс

```python
@dataclass(frozen=True, slots=True)
class LoaderCapabilities:
    search: bool = False
    playlists: bool = False
    posts: bool = False
    images: bool = False
    documents: bool = False
    live: bool = False
    subtitles: bool = False
    cookies: bool = False
    proxy: bool = False
    auth: bool = False

class MediaLoader(ABC):
    name: ClassVar[str]
    # Ключ секции [sources.<config_key>] в TOML. None => использовать name.
    config_key: ClassVar[str | None] = None
    # Дополнительные yt-dlp-плагины, без которых loader не работает.
    required_plugins: ClassVar[tuple[str, ...]] = ()
    capabilities: ClassVar[LoaderCapabilities]
    priority: ClassVar[int] = 100

    @abstractmethod
    def supports_url(self, url: str) -> bool:
        """Только offline-проверка. Не делает сетевых запросов."""

    def classify_url(self, url: str) -> UrlHint:
        """Offline-подсказка. Не является окончательной истиной."""
        return UrlHint.UNKNOWN

    @abstractmethod
    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> SourceInfo: ...

    @abstractmethod
    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult: ...

    # Поиск — ответственность конкретного источника.
    # YtDlpLoader даёт generic-реализацию; YouTube/Yandex/VK могут переопределить.
    def search(
        self,
        query: str,
        *,
        limit: int,
        ctx: LoaderContext,
    ) -> list[SearchCandidate]:
        raise SearchNotSupported(self.name)

    # Thumbnail: выбрать лучший из info/API и, при необходимости, скачать байты.
    def choose_thumbnail(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        return select_best_thumbnail(item.thumbnails)

    def fetch_thumbnail(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int | None = None,
    ) -> ThumbnailAsset | None:
        return fetch_thumbnail_http(thumbnail, ctx=ctx, max_bytes=max_bytes)

    # Рекомендованные варианты отдельно для аудио и видео.
    def recommend(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Recommendations:
        return default_recommendations(item)

    @classmethod
    def profile_name(cls) -> str:
        return cls.config_key or cls.name

    def is_ready(self, profile: "SourceProfile") -> bool:
        """Хватает ли конфигурации, чтобы loader мог работать (например, VK token)."""
        return True
```

`LoaderContext`:

```python
@dataclass(slots=True)
class LoaderContext:
    job_id: str
    profile: "SourceProfile"
    progress: ProgressSink
    cancel: CancellationToken
    staging: StagingArea
    http: HttpClient
    logger: logging.Logger
    cache: ResolutionCache
```

`SourceProfile` — конфиг источника (см. §9):

```python
@dataclass(frozen=True, slots=True)
class SourceProfile:
    name: str
    enabled: bool = True
    cookies_file: Path | None = None
    cookies_from_browser: tuple[str, ...] | None = None
    proxy: str | None = None
    user_agent: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    impersonate: str | None = None
    extractor_args: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    retries: int = 3
    socket_timeout: float = 20.0
    rate_limit: str | None = None
    geo_bypass: bool = False
    token: str | None = field(default=None, repr=False)
    extra: Mapping[str, object] = field(default_factory=dict, repr=False)
```

### 4.2. Почему сигнатуры отличаются от исходной идеи

| Исходная идея | Предлагается | Причина |
|---|---|---|
| `supports_url(url) -> bool` | без изменений | хороший offline-контракт |
| `is_playlist_url(url) -> bool` | **удалено**, только `classify_url() -> UrlHint` | URL может быть одновременно видео и плейлистом; без сети нельзя ответить `False`; отдельный `is_*` не нужен |
| `is_post(url) -> bool` | **удалено**, только `classify_url() -> UrlHint` | пост определяется источником и `info_dict`, а не только URL |
| `get_info(url)` | `get_info(SourceRef, flat=..., limit=...)` | нужен контекст, лимит и flat-режим |
| `download(data, chosen_variant)` | `download(DownloadRequest)` | несколько элементов, опции, sidecars, прогресс, отмена |
| — | `LoaderContext`, `ProgressSink`, `CancellationToken` | прогресс/отмена/конфиг без глобального состояния |
| — | `search()` у `MediaLoader` | поиск source-specific: YouTube/Yandex/VK сами проверяют ссылки и доступность |
| — | `choose_thumbnail()` / `fetch_thumbnail()` | лучший thumbnail не всегда в `info_dict`; loader знает API и fallback-URL |
| — | `recommend()` | рекомендованный вариант зависит от источника (MP4/M4A для YouTube и т.п.) |
| — | `LocalSourceReader` | локальный файл не надо «скачивать» |

### 4.3. Универсальный `YtDlpLoader`

```python
class YtDlpLoader(MediaLoader):
    name = "yt-dlp"
    config_key = "yt_dlp"
    priority = 1000
    capabilities = LoaderCapabilities(
        search=True, playlists=True, posts=True,
        live=True, subtitles=True, cookies=True, proxy=True,
    )

    def supports_url(self, url: str) -> bool:
        # Универсальный fallback: GenericIE попробует любой http(s)-URL.
        # Специализированные loader'ы стоят раньше по priority.
        return is_http_url(url)

    def get_info(self, ref, *, ctx, flat=False, limit=None) -> SourceInfo:
        info = self._extract(ref.url, ctx=ctx, flat=flat, limit=limit)
        source = YtDlpMapper(ctx.profile).map(info, ref, flat=flat)
        return self._finalize(source, ctx)

    def _finalize(self, source: SourceInfo, ctx: LoaderContext) -> SourceInfo:
        items = tuple(
            replace(
                item,
                thumbnail=self.choose_thumbnail(item, ctx=ctx) or item.thumbnail,
                recommendations=self.recommend(item, ctx=ctx),
            )
            for item in source.items
        )
        thumb = next((i.thumbnail for i in items if i.thumbnail), None) or source.thumbnail
        return replace(source, items=items, thumbnail=thumb)

    def search(self, query: str, *, limit: int, ctx: LoaderContext) -> list[SearchCandidate]:
        # Generic-поиск; сайтовые наследники переопределяют под свой API.
        info = self._extract(f"ytsearch{limit}:{query}", ctx=ctx, flat=True, limit=limit)
        return [
            self._candidate(entry)
            for entry in info.get("entries") or ()
            if entry.get("id") and (entry.get("webpage_url") or entry.get("url"))
        ]

    def _candidate(self, entry: Mapping[str, Any]) -> SearchCandidate:
        thumbs = map_thumbnails(entry)
        return SearchCandidate(
            source=(entry.get("extractor_key") or "youtube").lower(),
            id=str(entry["id"]),
            url=entry.get("webpage_url") or entry["url"],
            title=entry.get("title") or "",
            creator=entry.get("uploader") or entry.get("channel"),
            duration=entry.get("duration"),
            thumbnails=thumbs,
            thumbnail=select_best_thumbnail(thumbs),
        )

    def download(self, request, *, ctx) -> DownloadResult:
        # Варианты берём из уже разрешённого SourceInfo (снимок выбора).
        plan = YtDlpPlanner(request.source_info, request, job_id=ctx.job_id).plan()
        downloaded: list[DownloadedFile] = []
        failures: list[DownloadFailure] = []
        warnings: list[str] = []

        for index, item_plan in enumerate(plan.items, start=1):
            opts = self._download_options(item_plan, plan, request.options, ctx)
            hook = YtDlpProgressHook(ctx.progress, ctx.cancel, plan, item_plan, index)
            opts["progress_hooks"] = [hook.download]
            opts["postprocessor_hooks"] = [hook.postprocessor]
            try:
                with YoutubeDL(opts) as ydl:
                    # Один публичный вызов: extraction + download. yt-dlp сам
                    # переизвлечёт URL, поэтому подписанные ссылки не устаревают.
                    url = item_plan.item.page_url or request.source_info.url
                    info = ydl.extract_info(url, download=True)
                    info = ydl.sanitize_info(info)
                if not info:
                    raise DownloadFailed(f"no result for {item_plan.item.id}")
                downloaded.append(collect_downloaded_file(
                    request=request,
                    item_plan=item_plan,
                    staging=ctx.staging,
                    info=info,
                ))
            except DownloadCancelled:
                raise
            except DownloadError as exc:
                if len(plan.items) == 1:
                    raise map_ytdlp_error(exc) from exc
                failures.append(DownloadFailure(
                    item_id=item_plan.item.id,
                    message=humanize_error(exc),
                    retryable=is_retryable(exc),
                ))
            warnings.extend(hook.warnings)

        return DownloadResult(
            files=tuple(downloaded),
            failures=tuple(failures),
            warnings=tuple(warnings),
        )
```

`supports_url` не делает сети и не импортирует все экстракторы. Для универсального loader'а это честный контракт: он умеет *попробовать* любой http(s)-URL через generic-экстрактор; окончательный ответ даёт `get_info`, который может вернуть `UnsupportedUrl`. Сайтовые наследники используют быстрый regex и не строят индекс экстракторов. `gen_extractor_classes`/`suitable` имеет смысл только в `doctor`/диагностике, не в горячем пути.

`_extract`:

```python
def _extract(self, url, *, ctx, flat, limit):
    opts = self._base_options(ctx)
    opts.update({
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
        "extract_flat": "in_playlist" if flat else False,
        "playlist_items": f"1:{limit}" if limit else None,
        "logger": YtDlpLoggerAdapter(ctx.logger),
    })
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)
```

Замечания:

- `sanitize_info` нужен, чтобы результат был JSON-сериализуемым и без внутренних объектов; для очень больших плейлистов лучше мапить только нужные поля.
- `playlist_items` — строка вида `"1:50"`; `limit` применяется к коллекциям.
- `extract_flat="in_playlist"` не разворачивает элементы внутри плейлиста, но оставляет верхний уровень.
- `noplaylist` выставляется отдельно в зависимости от пользовательского выбора (`--playlist/--no-playlist`), не здесь.

#### Маппинг `info_dict` → `SourceInfo`

```python
class YtDlpMapper:
    def map(self, info: Mapping[str, Any], ref: SourceRef, *, flat: bool) -> SourceInfo:
        kind = self._kind(info, ref)
        items = tuple(
            self._item(entry, position=index)
            for index, entry in enumerate(self._entries(info), start=1)
        )
        return SourceInfo(
            source=ref.source,
            url=ref.url,
            kind=kind,
            id=str_or_none(info.get("id")),
            title=info.get("title") or info.get("playlist_title"),
            creator=info.get("uploader") or info.get("channel"),
            publisher=info.get("publisher"),  # редко; обычно None
            description=info.get("description"),
            thumbnails=self._thumbnails(info),
            thumbnail=select_best_thumbnail(self._thumbnails(info)),
            items=items,
        )
```

`map_thumbnails(entry)` — общий helper, который нормализует `thumbnails`/`thumbnail` и заполняет `Thumbnail`.

`_entries`:

- если `_type in {"playlist", "multi_video"}` → `info.get("entries") or ()`;
- если `_type in {"url", "url_transparent"}` → адаптер уже должен был развернуть; иначе ошибка `UnresolvedRedirect`;
- иначе → `(info,)`.

`_item`:

- `title`: `entry.get("track")` или `entry.get("title")`;
- `artists`: `entry.get("artists")`; fallback `creator`/`uploader`;
- `album`, `album_artists`, `track_number`, `disc_number`;
- `release_date`: `release_date`/`upload_date` → `PartialDate`;
- `page_url`: `webpage_url` или `url` (нужен для скачивания элемента плейлиста);
- `suggested_stem`: `entry.get("title")` или `"{creator} - {title}"`; это только staging-имя, не финальное;
- `thumbnails`: `self._thumbnails(entry)`;
- `thumbnail`: `select_best_thumbnail(...)`; сайтовый loader может заменить на свой лучший вариант;
- `variants`: `YtDlpVariantBuilder(entry).build()` если не `flat`;
- `recommendations`: заполняются loader'ом после маппинга (`replace(item, recommendations=self.recommend(item, ctx=ctx))`);
- `resolution`: `FLAT` если `flat`, иначе `FULL` (или `UNRESOLVED`, если нет форматов и нет `url`).

#### Ошибки

`DownloadError`/`ExtractorError` переводятся в типизированные ошибки:

- `UnsupportedUrl` — ни один экстрактор;
- `AuthRequired` — `Sign in`, `login`, `cookies`, `private`, `members-only`;
- `GeoBlocked` — `not available in your country`, `geo`;
- `DrmProtected` — `DRM`, `has_drm`;
- `RateLimited` — `429`, `Too Many Requests`;
- `Unavailable` — удалено/приватное;
- `NoFormats` — нет форматов;
- `DownloadFailed` — всё остальное.

Точные тексты зависят от версии; поэтому основная логика — по классам исключений и полям `info_dict`, а строки — только для сообщения пользователю.

### 4.4. Сайтовые наследники

Все они наследуются от `YtDlpLoader`, переопределяя только политику.

```python
class YouTubeLoader(YtDlpLoader):
    name = "youtube"
    config_key = "youtube"
    priority = 10
    _URL_RE = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/")

    def supports_url(self, url: str) -> bool:
        return bool(self._URL_RE.match(url))

    def classify_url(self, url: str) -> UrlHint:
        if "/playlist" in url or "list=" in url:
            return UrlHint.COLLECTION
        return UrlHint.SINGLE

    def search(self, query, *, limit, ctx):
        # Берём с запасом и отбрасываем недоступное/эфиры/Shorts.
        candidates = super().search(query, limit=limit * 2, ctx=ctx)
        return [c for c in candidates if self._is_playable(c, ctx)][:limit]

    def choose_thumbnail(self, item, *, ctx):
        candidates = list(item.thumbnails)
        if item.id and item.position in (None, 1):
            # yt-dlp не всегда отдаёт maxres; проверяем CDN только для primary item,
            # чтобы не делать 2 HEAD × 100 для плейлиста.
            for url, source, width, height in (
                (f"https://i.ytimg.com/vi/{item.id}/maxresdefault.jpg", "youtube:maxres", 1920, 1080),
                (f"https://i.ytimg.com/vi/{item.id}/sddefault.jpg", "youtube:sd", 640, 480),
            ):
                if ctx.http.exists(url):  # HEAD, ограниченно; не для всех кандидатов
                    candidates.append(Thumbnail(
                        url=url, width=width, height=height, source=source,
                    ))
        return select_best_thumbnail(candidates)
```

```python
class YandexMusicLoader(YtDlpLoader):
    name = "yandex_music"
    config_key = "yandex_music"
    priority = 20
    # Встроенный Yandex-экстрактор yt-dlp сломан; нужен плагин.
    required_plugins = ("yt-dlp-yandex-music",)
    _URL_RE = re.compile(r"https?://music\.yandex\.(?:ru|kz|ua|by|com)/")

    def supports_url(self, url: str) -> bool:
        return bool(self._URL_RE.match(url))

    def is_ready(self, profile: SourceProfile) -> bool:
        # Без плагина built-in сломан; без cookies get_info вернёт AuthRequired.
        return yandex_plugin_available()

    def classify_url(self, url: str) -> UrlHint:
        if "/track/" in url:
            return UrlHint.SINGLE
        if any(part in url for part in ("/album/", "/playlists/", "/artist/")):
            return UrlHint.COLLECTION
        return UrlHint.UNKNOWN

    def get_info(self, ref, *, ctx, flat=False, limit=None) -> SourceInfo:
        if not (ctx.profile.cookies_file or ctx.profile.cookies_from_browser):
            raise AuthRequired("Yandex Music requires a logged-in session cookie")
        source = super().get_info(ref, ctx=ctx, flat=flat, limit=limit)
        return warn_on_smart_preview(normalize_yandex_titles(source))

    def recommend(self, item, *, ctx):
        # best available: FLAC, если сервер отдал, иначе MP3 320/192.
        audio = best_audio_variant(item.variants)
        return Recommendations(audio=audio.id if audio else None)

    def search(self, query, *, limit, ctx):
        # Yandex Music не имеет публичного поиска в yt-dlp; используем API/веб.
        return self._search_yandex(query, limit=limit, ctx=ctx)
```

`yandex_plugin_available()` проверяет установленный дистрибутив/модуль плагина (`importlib.metadata.version("yt-dlp-yandex-music")` + `find_spec("yt_dlp_plugins.extractor.yandex_music_v2")`) или запускает `check_install.py`; `yt-dlp --list-extractors` и имена built-in-классов для этого не подходят. `warn_on_smart_preview()` смотрит `quality`/duration из ответа и добавляет `PreviewOnly`, если сессия протухла и API вернул 13–30-секундные превью. L2 дополнительно проверяет длительность через ffprobe.

```python
def normalize_yandex_titles(source: SourceInfo) -> SourceInfo:
    """Yandex отдаёт title = "Artist - Track"; убираем дубль, если есть track."""
    items = []
    for item in source.items:
        if item.title and item.creator and item.title.startswith(f"{item.creator} - "):
            item = replace(item, title=item.title.removeprefix(f"{item.creator} - "))
        items.append(item)
    return replace(source, items=tuple(items))
```

```python
class PinterestLoader(YtDlpLoader):
    name = "pinterest"
    config_key = "pinterest"
    priority = 20
    # Pinterest-поиск не реализован; generic ytsearch тут не подходит.
    capabilities = replace(YtDlpLoader.capabilities, search=False)
    _URL_RE = re.compile(r"https?://(?:[\w-]+\.)?pinterest\.[\w.]+/")

    def supports_url(self, url: str) -> bool:
        return bool(self._URL_RE.match(url))

    def classify_url(self, url: str) -> UrlHint:
        if "/pin/" in url:
            return UrlHint.POST
        return UrlHint.COLLECTION

    def get_info(self, ref, *, ctx, flat=False, limit=None):
        info = super().get_info(ref, ctx=ctx, flat=flat, limit=limit)
        if any(item.variants for item in info.items):
            return info
        # image-only pin: yt-dlp не даёт formats; нужен Pinterest API/AssetLoader
        return self._with_images(info, ctx)
```

`PinterestLoader._with_images` — отдельная задача; важно, что базовый класс не ломается и что модель умеет `IMAGE`.

```python
class VkApiLoader(MediaLoader):
    name = "vk"
    config_key = "vk"
    priority = 10
    capabilities = LoaderCapabilities(
        search=True, playlists=True,
        cookies=True, proxy=True, auth=True,
    )
    # Только музыкальные URL. VK-видео и стены обрабатывает YtDlpLoader.
    _URL_RE = re.compile(r"https?://(?:m\.)?vk\.com/(?:audio|music|audios)")

    def supports_url(self, url: str) -> bool:
        return bool(self._URL_RE.match(url))

    def is_ready(self, profile: "SourceProfile") -> bool:
        return bool(profile.token)

    def get_info(self, ref, *, ctx, flat=False, limit=None):
        return self._client(ctx.profile).get_info(ref, limit=limit)

    def search(self, query, *, limit, ctx):
        return self._client(ctx.profile).search(query, limit=limit)

    def recommend(self, item, *, ctx):
        audio = best_variant(
            item.variants, kind=MediaKind.AUDIO,
            prefer_container="m4a", order_by=("abr",),
        )
        return Recommendations(audio=audio.id if audio else None)

    def download(self, request, *, ctx):
        return self._client(ctx.profile).download(request, ctx=ctx)
```

`VkApiLoader` не наследуется от `YtDlpLoader`; он использует `VkAudioClient` (httpx/urllib) и `HttpStreamDownloader`. VK-видео/стены остаются на `YtDlpLoader`; это разные URL-паттерны и разные ограничения. `vk.com/wall...` с аудио-вложениями yt-dlp умеет; музыкальные `/audio...` и `/music/...` — нет.

```python
class DirectAssetLoader(MediaLoader):
    name = "direct"
    config_key = "direct"
    priority = 100
    _EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".pdf", ".zip"}

    def supports_url(self, url: str) -> bool:
        return is_http_url(url) and Path(urlparse(url).path).suffix.lower() in self._EXTS

    def get_info(self, ref, *, ctx, flat=False, limit=None):
        response = ctx.http.head(ref.url, allow_redirects=True)
        return SourceInfo(... items=(asset_item(ref, response),))

    def recommend(self, item, *, ctx):
        return Recommendations(image=item.variants[0].id if item.variants else None)

    def download(self, request, *, ctx):
        return download_direct(request, ctx=ctx)
```

`LocalSourceReader` — не `MediaLoader`:

```python
class LocalSourceReader:
    def read(self, paths: Sequence[Path]) -> SourceInfo:
        files = expand_local_paths(paths)  # файл или discover_media_files(dir)
        items = tuple(local_item(path) for path in files)
        return SourceInfo(
            source="local",
            url=paths[0].as_uri(),
            kind=SourceKind.SINGLE if len(items) == 1 else SourceKind.COLLECTION,
            id=None,
            title=None,
            creator=None,
            description=None,
            thumbnails=(),
            items=items,
        )
```

Локальный `DownloadVariant` — `id="original"`, `kind` по magic bytes/расширению, `streams=(StreamInfo(role=..., url=path.as_uri()),)`. `download` не вызывается; ingest получает путь напрямую.

### 4.5. Реестр загрузчиков

```python
@dataclass(frozen=True, slots=True)
class ResolvedLoader:
    loader: MediaLoader
    profile: "SourceProfile"

class LoaderRegistry:
    def __init__(self, loaders: Iterable[MediaLoader], config: "AppConfig") -> None:
        # Меньший priority — раньше. YtDlpLoader всегда последний.
        self._loaders = sorted(loaders, key=lambda loader: loader.priority)
        self._config = config

    def resolve(self, url: str, *, source: str | None = None) -> ResolvedLoader:
        candidates = [self.by_name(source)] if source else self._loaders
        for loader in candidates:
            profile = self._config.profile_for(loader.profile_name())
            if not profile.enabled or not loader.is_ready(profile):
                if source:
                    raise ConfigError(f"loader {loader.name!r} is disabled or not configured")
                continue
            if loader.supports_url(url):
                return ResolvedLoader(loader=loader, profile=profile)
        raise UnsupportedUrl(url, source=source)

    def by_name(self, name: str) -> MediaLoader:
        ...
```

Правила:

- **Никакого ручного выбора**: `AcquisitionService` вызывает `registry.resolve(url)`, реестр перебирает `MediaLoader` по `priority` и берёт первый, у которого `supports_url(url)` истинно.
- **Конфигурация подставляется автоматически**: для выбранного loader'а берётся `profile_for(loader.profile_name())`; это merged-профиль `[sources.default] + [sources.<config_key>]`. Пользователю не нужно указывать, что «этот URL — YouTube».
- `config_key` — явное имя секции; если не задан, используется `name`. Для `YtDlpLoader` (`name="yt-dlp"`) лучше `config_key="yt_dlp"`, чтобы TOML-ключ был без дефиса.
- `YtDlpLoader` — последний (1000), чтобы не перехватывать специализированные.
- `DirectAssetLoader` — 100, чтобы картинки не уходили в generic yt-dlp.
- `is_ready(profile)` — дешёвая проверка конфигурации: VK без токена не участвует, и URL уходит к yt-dlp (если тот поддерживает).
- Локальный ввод вообще не идёт через реестр.
- Автоматический fallback **после** ошибки (auth/geo/DRM) не делаем: это скрывает причину. Пользователь может явно указать `--source yt-dlp`.
- `--source` — только override для отладки/особых случаев; он проверяет `supports_url` и `is_ready`.

```python
resolved = registry.resolve(url)                      # loader + merged profile
ctx = LoaderContext(
    job_id=new_job_id(),
    profile=resolved.profile,
    progress=sink,
    cancel=token,
    staging=staging,
    http=http,
    logger=logger,
    cache=cache,
)
source = resolved.loader.get_info(SourceRef(resolved.loader.name, url), ctx=ctx)
```

### 4.6. Как добавить новый источник

1. Если сайт уже поддерживается yt-dlp — не писать новый загрузчик. Добавить `YtDlpLoader`-наследника только ради:
   - URL-паттерна и приоритета;
   - cookies/proxy/`extractor_args` по умолчанию;
   - собственного поиска и проверки найденных ссылок;
   - `choose_thumbnail`/`recommend` под сайт;
   - нормализации полей (например, `artist - title`).
2. Если нужен чужой API (как VK) — наследовать `MediaLoader`, реализовать `supports_url`, `get_info`, `download`; использовать общий `HttpStreamDownloader`/`ProgressSink`.
3. Если нужен только новый тип файлов (картинки/гиф/документы) — `DirectAssetLoader` уже покрывает прямые URL; для сайта-альбома добавить `AssetLoader`.
4. Зарегистрировать:
   - встроенный — в `default_registry()`;
   - внешний — entry point `trackforge.loaders` (код плагина = доверенный код).
5. Добавить тесты: `supports_url` (offline), маппинг `info_dict`, варианты, ошибки, прогресс.

---

## 5. Классификация URL

`classify_url` — только подсказка для UI и выбора стратегии. Окончательно тип задаёт `get_info`.

| Пример URL | `UrlHint` | Кто решает окончательно |
|---|---|---|
| `youtube.com/watch?v=...` | `SINGLE` | yt-dlp `_type=video` |
| `youtube.com/watch?v=...&list=...` | `COLLECTION` или `UNKNOWN` | флаг `--playlist`/`--no-playlist` + `_type` |
| `youtube.com/playlist?list=...` | `COLLECTION` | `_type=playlist` |
| `youtu.be/...` | `SINGLE` | `_type=video` |
| `music.yandex.ru/album/1/track/2` | `SINGLE` | `_type=video` |
| `music.yandex.ru/album/1` | `COLLECTION` | `_type=playlist` |
| `pinterest.com/pin/...` | `POST` | `_type` + наличие images/videos |
| `pinterest.com/user/board/` | `COLLECTION` | `_type=playlist` |
| `vk.com/wall-1_2` | `POST` | `_type=playlist`/`multi_video` |
| `vk.com/audio-1_2` | `SINGLE` | VK API |
| `vk.com/music/playlist/-1_2` | `COLLECTION` | VK API |
| `https://.../cover.jpg` | `SINGLE` | `DirectAssetLoader` |
| `https://.../watch?v=...` без известного экстрактора | `UNKNOWN` | `YtDlpLoader`/`UnsupportedUrl` |

`supports_url`-контракт:

1. Никаких сетевых запросов, DNS, HEAD.
2. Никаких исключений на мусорный URL — `False`.
3. Специализированные loader'ы используют regex по хосту; универсальный `YtDlpLoader` может вернуть `True` для любого `http(s)`, потому что он последний и умеет `GenericIE`. Окончательный ответ даёт `get_info`.
4. Не пытаться «угадать» по title/query.

---

### 5.1. Thumbnail: выбор и получение

`info_dict["thumbnail"]` — не гарантия лучшей картинки. В `thumbnails` может не быть `maxres`, могут попасть placeholder/аватарка, а у Яндекса/Pinterest/VK есть свои CDN-варианты. Поэтому выбор thumbnail — ответственность loader'а, а не generic-кода.

Модель:

- `Thumbnail` — URL + размеры + `mime`/`filesize` + `source` + `http_headers`;
- `ThumbnailAsset` — байты для Telegram/preview;
- `MediaItem.thumbnail` и `SourceInfo.thumbnail` — уже выбранный лучший вариант;
- `thumbnails` — исходный список (для UI/отладки);
- `SearchCandidate.thumbnail` — лучшая картинка результата поиска.

API loader'а:

```python
class MediaLoader:
    def choose_thumbnail(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        return select_best_thumbnail(item.thumbnails)

    def fetch_thumbnail(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int | None = None,
    ) -> ThumbnailAsset | None:
        return fetch_thumbnail_http(thumbnail, ctx=ctx, max_bytes=max_bytes)
```

`get_info` вызывает `choose_thumbnail` для каждого `MediaItem` и для `SourceInfo` (обычно первый/лучший item). Специфичные loader'ы переопределяют:

| Источник | Как выбирается thumbnail |
|---|---|
| YouTube | `thumbnails` + fallback на `maxresdefault.jpg`/`sddefault.jpg` от `id`; проверить HEAD/размер; выбрать максимальную площадь, 16:9 |
| Yandex Music | `coverUri` из API, заменить `%%` на `orig`; если нет — `thumbnails` |
| Pinterest | `images` из PinResource/BoardFeed; самая большая; для видео — poster |
| VK | `coverUrl`/`cover_url` из аудио-объекта; несколько размеров |
| Generic/yt-dlp | `thumbnails` → `thumbnail`; дедуп и сортировка по площади/preference |
| Direct | для изображения — сам файл; иначе `None` |

Алгоритм `select_best_thumbnail`:

1. Собрать кандидатов из `thumbnails`, `thumbnail`, loader-specific extras.
2. Дедуп по нормализованному URL.
3. Отбросить заведомо плохие: width/height < 320×180, placeholder-имена (`no_thumbnail`, `default`, `avatar`), битые URL.
4. Оценить: площадь (`width*height`), `preference`, формат (JPEG/PNG > WebP для совместимости), близость к 16:9.
5. Если размеры неизвестны — опционально проверить 1–3 лучших URL через HEAD/GET (content-type, content-length); не делать это для всех кандидатов.
6. Вернуть лучший; `source` заполнить (например, `youtube:maxres`).

```python
def select_best_thumbnail(
    candidates: Sequence[Thumbnail],
    *,
    min_width: int = 320,
    min_height: int = 180,
) -> Thumbnail | None:
    scored = []
    for thumb in dedupe_by_url(candidates):
        w, h = thumb.width or 0, thumb.height or 0
        if w and h and (w < min_width or h < min_height):
            continue
        area = w * h
        ratio_bonus = 1.0 if w and h and abs(w / h - 16 / 9) < 0.1 else 0.9
        format_bonus = 1.05 if thumb.ext in {"jpg", "jpeg", "png"} else 1.0
        scored.append((area * ratio_bonus * format_bonus + thumb.preference, thumb))
    return max(scored, key=lambda pair: pair[0], default=(0, None))[1]
```

Для Telegram-бота:

```python
source = service.inspect(url)
thumb = source.thumbnail                    # URL + headers
if thumb:
    asset = service.fetch_thumbnail(thumb, max_bytes=9_000_000)
    await bot.send_photo(chat_id, photo=asset.data, caption=source.title)
```

- `fetch_thumbnail` не тянет Pillow; возвращает `bytes` + `mime`.
- Если URL требует `Referer`/cookies, loader/HTTP-клиент используют `Thumbnail.http_headers`/профиль.
- `max_bytes` ограничивает скачивание; бот сам решает, конвертировать ли в JPEG.
- Thumbnail можно кэшировать in-memory по URL; не хранить на диске как библиотеку.

`get_info` может делать thumbnail-запросы, но они должны быть ограничены: 1–3 HEAD/GET для primary item; для остальных элементов коллекции — только уже полученные `thumbnails`, без массовых probe.

---

## 6. Поиск

Поиск — **метод конкретного `MediaLoader`**, а не отдельный `SearchProvider`. Причина: поиск source-specific.

- YouTube: нужно применить свои cookies/клиенты, отфильтровать Shorts/эфиры/недоступные видео, проверить `webpage_url`.
- Yandex Music: другой API, другие поля, другая нормализация `artist - title`.
- VK: `audio.search` с токеном, свои лимиты.
- SoundCloud: `scsearch`, свой формат.

Если вынести это в отдельный `SearchProvider`, получится либо дублирование логики, либо «провайдер», который знает про все сайты. Поэтому:

```python
class SearchService:
    def __init__(self, loaders: Sequence[MediaLoader], config: "AppConfig") -> None:
        self._loaders = loaders
        self._config = config

    def search(
        self,
        query: str,
        *,
        limit: int,
        ctx: LoaderContext,
        source: str | None = None,
    ) -> list[SearchCandidate]:
        candidates: list[SearchCandidate] = []
        for loader in self._loaders:
            if not loader.capabilities.search:
                continue
            if source and source not in {loader.name, loader.profile_name()}:
                continue
            profile = self._config.profile_for(loader.profile_name())
            if not profile.enabled or not loader.is_ready(profile):
                continue
            loader_ctx = replace(ctx, profile=profile)
            try:
                candidates.extend(loader.search(query, limit=limit, ctx=loader_ctx))
            except SearchNotSupported:
                continue
            except AcquireError as exc:
                ctx.logger.warning("search %s failed: %s", loader.name, exc)
        return rank_and_dedupe(candidates, query)[:limit]
```

`MediaLoader.search()` — optional; базовая реализация бросает `SearchNotSupported`. `YtDlpLoader` даёт generic-реализацию через `ytsearchN:`:

```python
class YtDlpLoader(MediaLoader):
    def search(self, query, *, limit, ctx):
        info = self._extract(f"ytsearch{limit}:{query}", ctx=ctx, flat=True, limit=limit)
        return [
            self._candidate(entry)
            for entry in info.get("entries") or ()
            if entry.get("id") and (entry.get("webpage_url") or entry.get("url"))
        ]
```

Сайтовые наследники переопределяют:

```python
class YouTubeLoader(YtDlpLoader):
    def search(self, query, *, limit, ctx):
        # Свои cookies/клиенты; берём с запасом и фильтруем недоступное/Shorts/эфиры.
        candidates = super().search(query, limit=limit * 2, ctx=ctx)
        return [c for c in candidates if self._is_playable(c, ctx)][:limit]
```

```python
class YandexMusicLoader(YtDlpLoader):
    def search(self, query, *, limit, ctx):
        return self._search_via_yandex_api(query, limit=limit, ctx=ctx)
```

```python
class VkApiLoader(MediaLoader):
    def search(self, query, *, limit, ctx):
        return self._client(ctx.profile).search(query, limit=limit)
```

Правила:

- `SearchService` — только оркестрация, дедуп и ранжирование; никакой site-specific логики.
- `MediaLoader.search` возвращает уже канонические URL, а не `ytsearch:` pseudo-URL.
- `YtDlpLoader._candidate` можно переопределить, чтобы применить сайтовый `choose_thumbnail` и фильтры к результатам поиска.
- YouTube-кандидаты проверяются на доступность/тип; Yandex/VK — своими API.
- `MusicBrainz` **не** `SearchService`: он не даёт URL для скачивания. Это `MetadataProvider` из ingest-слоя.
- По умолчанию не более 10 кандидатов; `--source` может ограничить набор loader'ов.
- Поиск никогда не запускается из `supports_url`.

**Разбор запроса.** Не надо aggressively парсить. Достаточно:

```python
def parse_query(raw: str) -> Query:
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
        return Query(raw=raw, artist=artist.strip(), title=title.strip())
    return Query(raw=raw)
```

`feat.`, `remix`, `nightcore`, `official` остаются частью title; их обработка — задача ingest/идентификации, а не поиска.

---

## 7. Планирование скачивания

### 7.1. Построение вариантов

```python
def build_variants(item: Mapping[str, Any]) -> list[DownloadVariant]:
    formats = list(item.get("formats") or ())
    if not formats and item.get("url"):
        formats = [item]  # direct/progressive single format

    video_only = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
    audio_only = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    progressive = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]

    variants: list[DownloadVariant] = []
    variants.extend(pair_video_with_audio(video_only, audio_only))
    variants.extend(map_progressive(progressive))
    variants.extend(map_audio(audio_only))
    return dedupe_and_sort(variants)
```

`pair_video_with_audio`:

- для каждого video-потока выбрать audio:
  1. тот же контейнер (`mp4` → `m4a`, `webm` → `webm`/`opus`);
  2. язык оригинала;
  3. максимальный `abr`, но не экзотический кодек без необходимости;
- `selector = f"{v['format_id']}+{a['format_id']}"`;
- `label = f"{height}p {container.upper()} ({vcodec}/{acodec})"`;
- `requires_merge=True`, `requires_ffmpeg=True`.

`map_audio`:

- `original` — audio-only с максимальным качеством; `selector=format_id`;
- `mp3` — только если пользователь явно попросил transcode; `selector=ba/b`, postprocessor `FFmpegExtractAudio(preferredcodec="mp3")`;
- `label = f"Audio {acodec} {abr}k"`.

`map_progressive`:

- `selector=format_id`; `requires_merge=False`;
- если есть `height`, label `"{height}p {ext}"`.

`downloadable`:

- `False`, если `has_drm is True` или `"maybe"` (для maybe — предупреждение, не блокировка);
- `False`, если `availability in {"private", "premium_only", "subscriber_only"}` без авторизации;
- `False`, если нет `url`/`fragments`;
- `unavailable_reason` — человекочитаемо.

### 7.1.1. Рекомендованные варианты

Loader сам говорит, что считать лучшим для аудио и для видео. Это не «первый вариант в списке» и не глобальная эвристика: у YouTube приоритет MP4/M4A, у Yandex Music — лучшее доступное (FLAC, иначе MP3 320/192), у VK — m4a/mp3.

Модель — `Recommendations` из §3.2. `MediaItem.recommendations` заполняется в `get_info` после построения вариантов. `MediaLoader.recommend()` — hook с default-реализацией `default_recommendations(item)`.

Политика по источникам:

| Источник | `recommendations.video` | `recommendations.audio` |
|---|---|---|
| YouTube | MP4 (H.264 + AAC) максимального разрешения; fallback — лучший MP4, затем лучший доступный | M4A/AAC с максимальным битрейтом; fallback — лучший audio-only |
| Yandex Music | нет (если видео нет) | FLAC, если сервер отдал; иначе MP3 320/192 (плагин `yt-dlp-yandex-music`) |
| VK | MP4, если есть видео | M4A/MP3 с максимальным битрейтом |
| Pinterest | MP4 для видео | нет |
| Generic/yt-dlp | MP4/H.264 максимального разрешения; fallback — лучший | M4A/AAC; fallback — лучший original, без transcode |
| Direct | — | — (image/document — original) |

Алгоритм `default_recommendations`:

1. Отбросить `downloadable=False`/DRM.
2. Для видео: приоритет `container="mp4"`, `vcodec` H.264/AVC, наличие AAC; затем максимальные `height`, `fps`, `tbr`.
3. Для аудио: приоритет `m4a`/AAC, максимальный `abr`; если нет — лучший audio-only без перекодирования.
4. Не рекомендовать transcode, если есть original.
5. Если для сценария нет варианта — `None`.
6. Для image/document — original-вариант (без transcode).

```python
def default_recommendations(item: MediaItem) -> Recommendations:
    video = best_video_variant(item.variants)   # mp4/h264 -> max height/fps
    audio = best_audio_variant(item.variants)   # m4a/aac -> max abr
    return Recommendations(
        video=video.id if video else None,
        audio=audio.id if audio else None,
        image=first_variant_id(item.variants, MediaKind.IMAGE),
        document=first_variant_id(item.variants, MediaKind.DOCUMENT),
    )
```

`YouTubeLoader` переопределяет `recommend`:

```python
class YouTubeLoader(YtDlpLoader):
    def recommend(self, item, *, ctx):
        video = best_variant(
            item.variants,
            kind=MediaKind.AUDIO_VIDEO,
            prefer_container="mp4",
            prefer_codec="h264",
            order_by=("height", "fps", "tbr"),
        )
        audio = best_variant(
            item.variants,
            kind=MediaKind.AUDIO,
            prefer_container="m4a",
            prefer_codec="aac",
            order_by=("abr",),
        )
        return Recommendations(
            video=video.id if video else None,
            audio=audio.id if audio else None,
        )
```

Использование:

- CLI: `--yes` для URL берёт `recommendations.video` (или audio, если `--audio`); в списке вариантов рекомендованные помечаются `★`.
- `--audio` = `recommendations.audio`, `--video` = `recommendations.video`.
- Telegram-бот: получает `item.recommendations` и показывает/отправляет кнопки с этими `variant_id`; ничего не угадывает сам.
- `--variant recommended:audio`/`recommended:video` — удобный алиас; `DownloadRequest` всё равно получает конкретный `variant_id`.
- Ограничение максимального разрешения — конфиг (`max_video_height`), но по умолчанию «максимально возможное», как просили.

### 7.2. `DownloadPlan`

```python
@dataclass(frozen=True, slots=True)
class ItemPlan:
    item: MediaItem
    variant: DownloadVariant
    selector: str
    output_stem: str

@dataclass(frozen=True, slots=True)
class DownloadPlan:
    job_id: str
    source: SourceRef
    source_info: SourceInfo
    items: tuple[ItemPlan, ...]
    options: DownloadOptions
```

`YtDlpPlanner`:

- берёт `SourceInfo`/`DownloadRequest`, находит `MediaItem` по `item_id`, `DownloadVariant` по `variant_id`;
- если `MediaItem.resolution` равен `FLAT`/`UNRESOLVED` (элемент плейлиста ещё не развёрнут), это ошибка контракта: `AcquisitionService` должен был развернуть выбранный элемент через `get_info(item.page_url)` до создания `DownloadRequest`; иначе — `VariantUnavailable`;
- проверяет `downloadable`;
- формирует `selector` (точный, с fallback);
- `output_stem = safe_stem(options.staging_name or item.suggested_stem or flat_stem(item))` — **плоское** имя в staging, без `job_id` и `format_id`; `job_id` уже уникален как каталог;
- если такой stem уже занят в текущем плане (плейлист/пост) — к `output_stem` добавляется префикс `1/`, `2/`, ...; файл остаётся плоским внутри подпапки;
- финальное имя и каталог появятся только после ingest; staging-имя не обязано быть «красивым».

Двухстадийность:

```python
collection = service.inspect(url, flat=True)          # быстро: title/url/thumb
chosen = collection.items[0]
full = service.inspect(chosen.page_url)               # форматы только для выбранного
request = DownloadRequest(source_info=full, selections=(...), options=...)
```

### 7.3. Этапы download

1. **Prepare** — проверить диск, создать staging, проверить cookies (используются in place), проверить `ffmpeg` при `requires_ffmpeg`.
2. **Download** — `YoutubeDL.extract_info(..., download=True)` с `format`, `paths`, `outtmpl`; прогресс.
3. **Postprocess** — merge/remux/extract audio/subtitles; postprocessor hooks.
4. **Collect** — найти реальные файлы (не доверять шаблону), sidecars; вернуть `DownloadResult`.
5. **Ingest** — отдельный вызов; загрузчик не пишет теги.

### 7.4. Staging и атомарность

```python
@dataclass(slots=True)
class StagingArea:
    root: Path

    def job_dir(self, job_id: str) -> Path:
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=False)
        return path

    def commit(
        self,
        source: Path,
        target: Path,
        *,
        policy: CollisionPolicy = CollisionPolicy.SUBFOLDER,
    ) -> Path | None:
        target = allocate_target(target, policy)
        if target is None:          # SKIP
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)  # только в пределах одной ФС
        return target
```

`allocate_target`:

```python
def allocate_target(
    target: Path,
    policy: CollisionPolicy = CollisionPolicy.SUBFOLDER,
) -> Path | None:
    if not target.exists():
        return target
    if policy is CollisionPolicy.ERROR:
        raise FileExistsError(target)
    if policy is CollisionPolicy.SKIP:
        return None
    if policy is CollisionPolicy.OVERWRITE:
        return target
    # SUBFOLDER: Author - Title.ext -> 1/Author - Title.ext -> 2/...
    n = 1
    while (target.parent / str(n) / target.name).exists():
        n += 1
    return target.parent / str(n) / target.name
```

- `SUBFOLDER` — политика по умолчанию; имя файла не портится `(1)`, `(2)`.
- Для видео/аудио с sidecars используем `commit_group(media, sidecars)`: подпапка `1/` выбирается один раз для всей группы, иначе `.lrc` останется в корне.
- staging лучше держать на той же файловой системе, что и `target_dir`; иначе `os.replace` не сработает, нужен copy+fsync+replace.
- при отмене/ошибке staging сохраняется по `--keep-temp`, иначе удаляется.
- `.part`-файлы yt-dlp остаются для докачки, если `continuedl=True`; это не БД, а файловое состояние.

### 7.5. Прогресс

`YtDlpProgressHook`:

```python
class YtDlpProgressHook:
    def __init__(self, sink, cancel, plan, item_plan, item_index):
        self._sink = sink
        self._cancel = cancel
        self._plan = plan
        self._item_plan = item_plan
        self._item_index = item_index
        self._item_count = len(plan.items)
        self.warnings: list[str] = []

    def download(self, data: dict) -> None:
        self._cancel.raise_if_cancelled()
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            self._sink.emit(ProgressEvent(
                stage=ProgressStage.DOWNLOAD,
                job_id=self._plan.job_id,
                item_index=self._item_index,
                item_count=self._item_count,
                filename=data.get("filename"),
                downloaded_bytes=data.get("downloaded_bytes"),
                total_bytes=total,
                speed=data.get("speed"),
                eta=data.get("eta"),
                fragment_index=data.get("fragment_index"),
                fragment_count=data.get("fragment_count"),
            ))
        elif status == "finished":
            self._sink.emit(ProgressEvent(
                stage=ProgressStage.DOWNLOAD,
                job_id=self._plan.job_id,
                item_index=self._item_index,
                item_count=self._item_count,
                filename=data.get("filename"),
                downloaded_bytes=data.get("downloaded_bytes"),
                total_bytes=data.get("total_bytes"),
            ))
        elif status == "error":
            self._sink.emit(ProgressEvent(
                stage=ProgressStage.DOWNLOAD,
                job_id=self._plan.job_id,
                item_index=self._item_index,
                item_count=self._item_count,
                message="download error",
            ))

    def postprocessor(self, data: dict) -> None:
        self._cancel.raise_if_cancelled()
        mapping = {
            "FFmpegVideoRemuxer": ProgressStage.REMUX,
            "FFmpegExtractAudio": ProgressStage.EXTRACT_AUDIO,
            "FFmpegVideoConvertor": ProgressStage.CONVERT,
            "FFmpegSubtitlesConvertor": ProgressStage.SUBTITLES,
        }
        stage = mapping.get(data.get("postprocessor"), ProgressStage.CONVERT)
        self._sink.emit(ProgressEvent(
            stage=stage,
            job_id=self._plan.job_id,
            item_index=self._item_index,
            item_count=self._item_count,
            message=data.get("status"),
        ))
```

Для VK/direct HTTP — свой `stream_to_file` с `Content-Length` и `Range`, эмитит те же события.

Агрегация для плейлиста:

```python
overall = (completed_items + current_item_fraction) / total_items
```

Если `total_bytes` неизвестен — indeterminate progress, без фейковых 100%.

---

## 8. yt-dlp adapter: детали

### 8.1. `YoutubeDL` options

```python
def _base_options(profile: SourceProfile, logger: logging.Logger) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
        "socket_timeout": profile.socket_timeout,
        "retries": profile.retries,
        "fragment_retries": profile.retries,
        "extractor_retries": profile.retries,
        "js_runtimes": {"deno": {"path": deno.find_deno_bin()}},
        "remote_components": [],       # не тянем ejs из сети, используем пакет
        "logger": YtDlpLoggerAdapter(logger),
        "cachedir": False,  # не строим persistent-кэш
    }
    if profile.cookies_file:
        opts["cookiefile"] = str(profile.cookies_file)
    if profile.cookies_from_browser:
        opts["cookiesfrombrowser"] = profile.cookies_from_browser
    if profile.proxy:
        opts["proxy"] = profile.proxy
    if profile.impersonate:
        opts["impersonate"] = profile.impersonate
    if profile.extractor_args:
        opts["extractor_args"] = {
            key: {k: list(v) for k, v in value.items()}
            for key, value in profile.extractor_args.items()
        }
    if profile.headers:
        opts["http_headers"] = dict(profile.headers)
    if profile.rate_limit:
        opts["ratelimit"] = profile.rate_limit
    return opts
```

`impersonate` в API — `ImpersonateTarget`, не просто строка; адаптер должен преобразовать (`yt_dlp.networking.impersonate.ImpersonateTarget.from_str` или аналог). В docs показываем строку конфига, в коде — преобразование. `js_runtimes` берёт путь из `deno.find_deno_bin()`; если используется Node/Bun/QuickJS — подставить соответствующий ключ и проверить в `doctor`.

`download_options` добавляет (вызывается на каждый `ItemPlan`):

```python
def _download_options(item_plan, plan, options, ctx) -> dict:
    staging_root = ctx.staging.root / plan.job_id
    staging_root.mkdir(parents=True, exist_ok=True)
    return {
        **_base_options(ctx.profile, ctx.logger),
        "format": item_plan.selector,
        # home — корень job'а; outtmpl может содержать "1/" при коллизии.
        "paths": {"home": str(staging_root), "temp": str(staging_root / "tmp")},
        "outtmpl": {"default": f"{item_plan.output_stem}.%(ext)s"},
        "noplaylist": True,  # мы уже выбрали конкретный item
        "writethumbnail": options.write_thumbnail,
        "writeinfojson": options.write_info_json,
        "writesubtitles": bool(options.subtitles),
        "subtitleslangs": list(options.subtitles),
        "overwrites": options.collision is CollisionPolicy.OVERWRITE,
        "continuedl": True,
        "merge_output_format": options.container,
        "postprocessors": postprocessors(item_plan, options),
    }
```

### 8.2. Postprocessors

| Задача | Postprocessor | Аргументы |
|---|---|---|
| Извлечь аудио | `FFmpegExtractAudio` | `preferredcodec`, `preferredquality` |
| Сменить контейнер без перекодирования | `FFmpegVideoRemuxer` | `preferedformat` (именно так в yt-dlp) |
| Конвертировать видео | `FFmpegVideoConvertor` | `preferedformat` |
| Конвертировать субтитры | `FFmpegSubtitlesConvertor` | `format="lrc"` |
| Встроить субтитры | `FFmpegEmbedSubtitle` | — |
| Метаданные | **не использовать** | TrackForge пишет теги |
| Обложка | **не использовать** | TrackForge пишет artwork |

Порядок: extract/remux → subtitles → (позже ingest). `FFmpegMetadata` и `EmbedThumbnail` отключены, чтобы не было двойной записи.

### 8.3. Ошибки и коды возврата

`ydl.extract_info(..., download=True)` возвращает info-dict; при ошибках yt-dlp бросает `DownloadError`. Адаптер:

- ловит `DownloadError`, `ExtractorError`, `UnsupportedError`, `GeoRestrictedError`, `DownloadCancelled`;
- смотрит `info_dict["availability"]`, `has_drm`, `live_status`;
- добавляет подсказки (`--cookies`, `--proxy`, обновить yt-dlp);
- не парсит stdout/stderr для бизнес-логики.

### 8.4. Кэш и подписанные URL

- `SourceInfo` можно кэшировать в памяти на 1–5 минут для UI, но **не URL потоков**.
- Перед `download` всегда нужен свежий resolve; yt-dlp всё равно делает его внутри.
- Не сохранять `info_dict` с URL в БД/на диск по умолчанию. `.info.json` — только опциональный sidecar для отладки/провенанса.
- Если пользователь выбрал вариант, а формат исчез — `VariantUnavailable`, предложить повторный `get_info`.

### 8.5. Потоки и cookies

- Один `YoutubeDL` на job; не переиспользовать между потоками.
- Cookies-файл используется напрямую (in place); yt-dlp может обновлять его. Не запускать несколько job с одним cookie-файлом одновременно.
- `cachedir=False` или отдельный temp на job; иначе общий кэш yt-dlp может конфликтовать.
- Плагины yt-dlp загружаются глобально; не устанавливать недоверенные.

### 8.6. Зависимости

- `yt-dlp` — core dependency (или optional extra `trackforge[download]`, если нужен metadata-only install).
- `yt-dlp[default]` — удобно: `mutagen`, `requests`, `certifi`, `brotli`, `websockets`, `pycryptodomex` и **`yt-dlp-ejs`** (на 2026.10.21 — `0.8.0`).
- **`yt-dlp-ejs` + JS runtime** — обязательны для полноценного YouTube:
  - EJS-пакет даёт JS-код, но сам его не исполняет;
  - runtime: Deno (рекомендуется), Node.js, Bun, QuickJS;
  - **Deno ставится Python-пакетом `deno>=2.9.8`** — официальный py3-none-wheel с бинарником от Deno authors; системный runtime не нужен; добавляем в `pyproject.toml`;
  - путь берём через `deno.find_deno_bin()` и передаём явно: `js_runtimes={"deno": {"path": deno.find_deno_bin()}}`;
  - wheel ~40 MB; платформы: Linux x86_64/arm64 (glibc 2.27+), macOS x86_64/arm64, Windows x64; на Alpine/musl нужен fallback (Node/системный Deno);
  - без runtime yt-dlp может исключить `web`/`tv_samsung`-клиентов и потерять часть форматов;
  - `remote_components` по умолчанию не включаем: EJS уже установлен, сетевые компоненты не нужны;
  - `doctor` проверяет и пакет `yt_dlp_ejs`, и `deno.find_deno_bin()`.
- `yt-dlp-yandex-music` (>= 2026.9.24) — сторонний MIT-плагин, обязательный для Yandex Music; ставится в то же окружение, что и `yt-dlp`, и пинится. Встроенный Yandex-экстрактор считается сломанным; без плагина `YandexMusicLoader` не готов.
- `curl_cffi` — опционально для `impersonate` (TLS fingerprint).
- `yt-dlp[deno]` extra делает то же самое (`deno>=2.9.8`), но явная зависимость `deno` в `pyproject.toml` понятнее и не зависит от extras.
- `ffmpeg`/`ffprobe` — единственные внешние бинарники; нужны для merge/remux/extract audio/probe; проверять в `doctor`. Полностью «python-only» install (static-ffmpeg/imageio-ffmpeg) — отдельное решение, не в этом цикле.

| Компонент | Зачем | Обязателен |
|---|---|---|
| `yt-dlp-ejs` | JS-код YouTube | для полноценного YouTube |
| `deno` (PyPI) | выполнить EJS | да, если нужен EJS |
| `ffmpeg`/`ffprobe` | merge/remux/audio/probe | для видео и L2 |
| `yt-dlp-yandex-music` | Yandex Music API | для Yandex Music |
| `curl_cffi` | impersonate | опционально |

---

## 9. Конфигурация

### 9.1. Файлы и приоритет

- Пользовательский: `$XDG_CONFIG_HOME/trackforge/config.toml` или `~/.config/trackforge/config.toml` (Windows — `%APPDATA%\trackforge\config.toml`).
- Явный: `tfg --config path.toml ...`.
- **Не** читать `.trackforge.toml` из текущего каталога автоматически: недоверенный репозиторий может подсунуть прокси/путь к cookies. Только явно.
- Приоритет: CLI > env > `--config` > user config > defaults.
- Env: `TRACKFORGE_*`; секреты — через `${VAR}`.

### 9.2. Пример

```toml
[general]
output_dir = "~/Music/TrackForge"
temp_dir = "~/.cache/trackforge/tmp"
language = "ru"

[naming]
mode = "flat"                    # flat | template
flat = "{artist} - {title}"
template = "{artist}/{album}/{track:02d} - {title}"   # используется только при mode="template"
collision = "subfolder"          # subfolder | error | skip | overwrite

[download]
retries = 3
concurrent_fragments = 4
write_thumbnail = true
write_info_json = false
audio_format = "original"   # original | mp3 | m4a
subtitle_langs = ["ru", "en"]

[search]
loaders = ["youtube"]   # имена loader'ов; пусто = все search-capable
limit = 10

[sources.default]
socket_timeout = 20
retries = 3
# proxy = "${TRACKFORGE_PROXY}"

[sources.yt_dlp]
# generic fallback; сюда попадут сайты без отдельного профиля
socket_timeout = 20

[sources.youtube]
cookies = "~/.config/trackforge/cookies/youtube.txt"
# cookies_from_browser = ["chrome"]  # взаимоисключающе с cookies
proxy = "${YOUTUBE_PROXY}"
impersonate = "chrome"
extractor_args = { youtube = { player_client = ["web", "tv"] } }

[sources.yandex_music]
cookies = "~/.config/trackforge/cookies/yandex.txt"
proxy = "${YANDEX_PROXY}"

[sources.vk]
token = "${VK_TOKEN}"
api_version = "5.199"
rate_limit = 3
proxy = "${VK_PROXY}"

[sources.pinterest]
cookies = "~/.config/trackforge/cookies/pinterest.txt"
```

### 9.3. Модель конфига

```python
@dataclass(frozen=True, slots=True)
class AppConfig:
    general: GeneralConfig
    download: DownloadConfig
    search: SearchConfig
    sources: Mapping[str, SourceProfile]

    def profile_for(self, name: str) -> SourceProfile:
        base = self.sources.get("default", SourceProfile(name="default"))
        specific = self.sources.get(name)
        return merge_profiles(base, specific) if specific else replace(base, name=name)
```

Правила:

- `LoaderRegistry.resolve(url)` сам вызывает `profile_for(loader.profile_name())`; пользователь не сопоставляет URL и секцию вручную;
- `profile_for(name)` = `[sources.default]` + `[sources.<name>]`; `config_key` задаёт имя, иначе `name`;
- неизвестные ключи — warning, не падение (облегчает миграции);
- `cookies` и `cookies_from_browser` взаимоисключающие;
- `token`/`cookies` не попадают в `repr`, логи, JSON;
- `~` раскрывается; относительные пути — относительно файла конфига, не cwd;
- файл cookies проверяется на существование/права, но не читается в лог;
- `proxy` может быть `"none"` (явно без прокси), `"env"` (из окружения) или URL;
- `impersonate` проверяется через `yt-dlp --list-impersonate-targets`/API; если `curl_cffi` нет — предупреждение.

### 9.4. Секреты

- `${VAR}` — только подстановка env, без выполнения команд.
- Не поддерживать `!command` и shell-evaluation.
- `tfg doctor` показывает «настроено/не настроено», но не значения.
- Cookies используются из настроенного файла напрямую; yt-dlp может обновлять его.
- В JSON-выводе `http_headers`, `token`, `cookies`, `proxy` не сериализуются.

---

## 10. CLI

### 10.1. Команды

| Команда | Назначение |
|---|---|
| `tfg 'Автор - Название'` | текстовый поиск → выбор → скачивание → ingest |
| `tfg https://...` | URL → выбор варианта → скачивание → ingest |
| `tfg ./track.mp3` | локальный файл → ingest |
| `tfg ./music/` | локальная папка → ingest |
| `tfg input '...'` | явный алиас для всего выше |
| `tfg i '...'` | короткий алиас |
| `tfg search '...'` | только поиск, без скачивания |
| `tfg inspect URL` | только `SourceInfo`/варианты, без скачивания |
| `tfg sources` | список загрузчиков и их возможностей |
| `tfg doctor` | проверка yt-dlp, `yt-dlp-ejs`, JS runtime, `yt-dlp-yandex-music`, ffmpeg, cookies, proxy |
| `tfg info PATH` | существующая команда метаданных (не трогаем) |
| `tfg set ...`, `tfg scan ...` | существующие команды |

Реализация Typer:

```python
@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    source: str | None = typer.Argument(None),
    ...
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if source is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    run_input(source, ...)
```

`input`/`i` регистрируются как отдельные команды, вызывающие тот же `run_input`. Это позволяет и `tfg '...'`, и `tfg input '...'`.

### 10.2. Интерактивный сценарий

```text
$ tfg "Skillet - Feel Invincible"

Search: 8 results (youtube)
 #  Source   Title                                        Channel        Duration
 1  youtube  Skillet - Feel Invincible (Official Video)    Skillet        4:12
 2  youtube  Skillet - Feel Invincible (Lyric Video)       Skillet        4:11
 ...
Select [1-8, q]: 1

Title:     Skillet - Feel Invincible (Official Video)
Channel:   Skillet
Duration:  4:12
Thumbnail: https://.../maxres.jpg (1280x720)

Formats:
 #  Kind   Quality                  Container  Codecs          Size
 1  video  1080p ★ recommended      mp4        h264/aac        ~48 MB
 2  video  720p                     mp4        h264/aac        ~25 MB
 3  audio  original (aac 128k) ★    m4a        aac             ~4.1 MB
 4  audio  mp3 320 (transcode)      mp3        mp3             ~9.8 MB
Select [1-4, a=recommended audio, v=recommended video, q]: 1

[download] 42%  18.2 MB / 48.0 MB  3.1 MB/s  ETA 10s
[merge]     ...
[ingest]    identify... MusicBrainz match 0.97
[tag]       ...
[done]      ~/Music/TrackForge/Skillet - Feel Invincible.m4a
```

`SourceInfo.creator` — это `uploader`/`channel`/владелец страницы. В UI подписываем его как «Автор/канал», а не «публикатор»: настоящий publisher/label появляется только из metadata-провайдеров (MusicBrainz/Discogs) или из явного поля сайта. В модели для него есть отдельное `SourceInfo.publisher`, которое чаще всего `None`.

Правила:

- по умолчанию показываем 3–7 вариантов, полный список — `--all-formats`;
- если вариант один — всё равно показываем summary, но можно `Enter`;
- если `stdin` не TTY и нет `--pick`/`--variant` — печатаем список и выходим с кодом 2;
- `--yes` не должен молча выбирать первый результат текстового поиска; для текста нужен `--pick N` (или `--yes --pick N`);
- для URL с одним элементом `--yes` выбирает рекомендованный вариант.

### 10.3. Не-интерактивный режим

```bash
tfg "Artist - Track" --pick 1 --variant 1080p --yes
tfg "https://..." --audio recommended --yes
tfg "https://..." --format 137+140 --yes
tfg "https://..." --all --yes
tfg "https://..." --dry-run --json
tfg search "Artist - Track" --json
tfg inspect "https://..." --json
```

- `--pick` — индекс результата поиска;
- `--variant` — id варианта, `recommended:audio`/`recommended:video`, `best`/`1080p`;
- `--format` — точный yt-dlp selector (advanced);
- `--audio` — shortcut: `recommendations.audio`; `--video` — `recommendations.video`;
- `--all` — все элементы коллекции;
- `--items 1,3-5` — выборочные элементы;
- `--dry-run` — resolve + plan, без скачивания;
- `--json` — финальный JSON; `--events jsonl` — прогресс-события;
- `--source NAME` — принудительный loader (`youtube`, `yandex_music`, `vk`, `direct`, `yt-dlp`);
- `--cookies PATH` — разовый override cookies для источника;
- `--proxy URL` — разовый override прокси;
- `--config PATH` — явный конфиг (не читаем `.trackforge.toml` из cwd).

### 10.4. JSON (download result; будущие L2/L3)

Для `tfg inspect` действует отдельная зафиксированная схема из §3.2.1
(`schema_version: 1`, без `input`/`result`). Ниже — эскиз будущего JSON
результата скачивания; он не должен подменять inspect-контракт.

```json
{
  "schema_version": 1,
  "input": {"kind": "url", "raw": "https://..."},
  "source": {
    "source": "youtube",
    "kind": "single",
    "title": "...",
    "creator": "...",
    "items": [
      {
        "id": "...",
        "title": "...",
        "thumbnail": {"url": "https://.../maxres.jpg", "width": 1280, "height": 720},
        "recommendations": {"audio": "audio-m4a-128", "video": "1080p-mp4"},
        "variants": [
          {"id": "1080p-mp4", "label": "1080p MP4", "kind": "video",
           "container": "mp4", "height": 1080, "filesize": 48000000,
           "requires_merge": true},
          {"id": "audio-m4a-128", "label": "Audio M4A 128k", "kind": "audio",
           "container": "m4a", "filesize": 4100000}
        ]
      }
    ]
  },
  "result": {
    "files": [{"path": "...", "sidecars": ["..."]}],
    "failures": []
  }
}
```

- `schema_version` — чтобы автоматизация не ломалась;
- `raw`, `http_headers`, cookies/tokens не выводятся;
- `--json` и `--events jsonl` — разные потоки: результат в stdout, события в stderr (или отдельный файл).

### 10.5. Превью

- В модели — `Thumbnail.url` + размеры + `http_headers`.
- По умолчанию CLI печатает URL и размеры; в TTY можно сделать OSC 8 hyperlink.
- `--preview` — опционально скачать во временный файл и вызвать `chafa`/`viu`/`img2txt`, если установлены; иначе — предупреждение.
- Не тащить тяжёлую графическую зависимость в core.
- Для JSON — только URL/размеры.

### 10.6. Коды выхода

| Код | Значение |
|---|---|
| 0 | успех |
| 1 | операционная ошибка (сеть, сайт, ffmpeg, ingest) |
| 2 | нужен выбор/неверные аргументы |
| 130 | отменено пользователем |

Частичный успех плейлиста: файлы сохранены, `failures` заполнены, код 1.

---

### 10.7. Встраивание в Telegram-бота (без бота в модуле)

`acquire`/`ingest` не должны зависеть от Typer/CLI. Публичный API — обычные Python-объекты и callbacks; бот может вызывать их из worker-потока.

Сценарий:

1. `service.search(query)` → `SearchCandidate[]`; у каждого уже есть `thumbnail`.
2. Бот показывает inline-кнопки: title, creator, duration, thumbnail.
3. Пользователь выбирает кандидата → `service.inspect(candidate.url)`.
4. Бот отправляет фото (`source.thumbnail` через `service.fetch_thumbnail(...)`) и варианты:
   - `item.recommendations.video` / `.audio`;
   - остальные `DownloadVariant` — как дополнительные кнопки.
5. Пользователь выбирает вариант → `DownloadRequest` с конкретным `variant_id`.
6. `service.download` принимает `ProgressSink`; бот обновляет сообщение/прогресс.
7. Результат — `DownloadResult`; при необходимости ingest; бот отправляет файл.

```python
import asyncio

async def on_url(bot, chat_id: int, url: str) -> None:
    source = await asyncio.to_thread(service.inspect, url)
    item = source.items[0]
    thumb = None
    if source.thumbnail:
        thumb = await asyncio.to_thread(
            service.fetch_thumbnail, source.thumbnail, max_bytes=9_000_000
        )
    # bot.send_photo(chat_id, photo=thumb.data, caption=source.title)
    # кнопки: item.recommendations.video / item.recommendations.audio + остальные
```

Требования к модулю:

- `SourceInfo`/`MediaItem`/`DownloadVariant` — JSON-сериализуемые (без `raw`, cookies, headers);
- `thumbnail` — URL + размеры; `fetch_thumbnail` — байты;
- `recommendations` — готовые `variant_id`, бот не угадывает;
- `download` — блокирующий; бот оборачивает в `asyncio.to_thread`/executor;
- `ProgressSink` — thread-safe callback; бот сам решает, как троттлить;
- никакой БД/истории: состояние диалога — на стороне бота.

---

## 11. Ingest после скачивания

Загрузчик отдаёт `DownloadedFile` + `MediaItem` + sidecars. Дальше — отдельный слой.

### 11.1. Пайплайн

```text
DownloadedFile
  → probe (ffprobe/MediaFile.open)
  → fingerprint (fpcalc) → AcoustID → MusicBrainz MBID
  → metadata providers + source hints
  → candidate ranking / user confirmation
  → artwork + lyrics
  → MediaFile.save() (tags)
  → rename + sidecars (.lrc/.elrc)
  → IngestResult
```

### 11.2. Протоколы

```python
@dataclass(frozen=True, slots=True)
class Claim[T]:
    value: T
    source: str
    confidence: float
    url: str | None = None

@dataclass(frozen=True, slots=True)
class MetadataCandidate:
    provider: str
    id: str
    score: float
    claims: Mapping[str, list[Claim]]
    mbids: Mapping[str, str] = field(default_factory=dict)

class Identifier(Protocol):
    def identify(
        self,
        media: MediaFile,
        hints: MediaItem,
        *,
        ctx: IngestContext,
    ) -> list[MetadataCandidate]: ...

class MetadataProvider(Protocol):
    def lookup(self, query: MetadataQuery, *, ctx: IngestContext) -> list[MetadataCandidate]: ...
    def artwork(self, candidate: MetadataCandidate, *, ctx: IngestContext) -> list[Artwork]: ...
    def lyrics(self, candidate: MetadataCandidate, *, ctx: IngestContext) -> LyricsDocument | None: ...
```

- `SourceHints` (`MediaItem`) — один из источников claims с низким/средним весом.
- `AcoustIDIdentifier` — основной, если есть `fpcalc` и ключ.
- `TextIdentifier` — fallback по `artist`/`title`/`duration`.
- `MusicBrainzProvider`, `CoverArtProvider`, `LRCLIBLyricsProvider` — конкретные реализации.
- `Analyzer` (LLM) — позже; не влияет на базовый контракт.

### 11.3. Слияние и уверенность

Приоритет:

1. явное значение пользователя;
2. fingerprint/MusicBrainz с высоким score;
3. консенсус провайдеров;
4. source hints;
5. эвристика.

Правила:

- не перезаписывать пользовательские правки;
- конфликты фиксировать в `IngestReport.warnings`;
- если confidence ниже порога — не менять поле, оставить source hint;
- `--offline` — только source hints + существующие теги;
- `--yes` — автопринятие только при высоком score; иначе показать кандидатов.

### 11.4. Имена и sidecars

Соглашаемся: не надо делать иерархию `{artist}/{album}/...` дефолтом. Это лишнее до анализа. Нужно разделить **два имени**:

1. **staging name** — плоское, временное, в staging-каталоге job'а. По умолчанию `Author - Title.ext`; если loader дал `MediaItem.suggested_stem`, используем его. Это имя не обязано быть финальным.
2. **final name** — после идентификации и записи метаданных. По умолчанию тоже **плоское**: `Author - Title.ext`. Иерархия — только по явному требованию (`naming.mode = "template"`).

```python
class NamingMode(StrEnum):
    FLAT = "flat"          # output_dir/Author - Title.ext
    TEMPLATE = "template"  # output_dir/{artist}/{album}/...

DEFAULT_FLAT = "{artist} - {title}"
DEFAULT_TEMPLATE = "{artist}/{album}/{track:02d} - {title}"

@dataclass(frozen=True, slots=True)
class NamingPolicy:
    mode: NamingMode = NamingMode.FLAT
    flat: str = DEFAULT_FLAT
    template: str = DEFAULT_TEMPLATE
    collision: CollisionPolicy = CollisionPolicy.SUBFOLDER

@dataclass(frozen=True, slots=True)
class OutputPlan:
    media: Path
    sidecars: tuple[Path, ...] = ()
    collision: bool = False

class OutputPlanner:
    def plan(
        self,
        item: MediaItem,
        metadata: MediaFile,
        extension: str,
        *,
        root: Path,
        naming: NamingPolicy = NamingPolicy(),
    ) -> OutputPlan:
        ...

def flat_stem(item: MediaItem) -> str:
    creator = item.creator or (item.artists[0] if item.artists else "Unknown Artist")
    return f"{creator} - {item.title}"
```

Правила:

- **FLAT (default)**: `root / sanitize("{artist} - {title}").ext`. Никаких `album/track` по умолчанию.
- **TEMPLATE (opt-in)**: `root / sanitize(render(naming.template)).ext`; можно вернуть иерархию, если пользователь-программист так захотел.
- Если `artist`/`title` нет: fallback на `title`, `suggested_stem`, `id`; не оставлять пустое имя.
- `suggested_stem` от loader'а используется **только для staging**; финальное имя всегда строится из финальных метаданных.
- **CollisionPolicy.SUBFOLDER (default)**: `Author - Title.ext` уже есть → `1/Author - Title.ext`; есть и там → `2/...`. Имя файла не портится `(1)`, `(2)`.
- `ERROR`/`SKIP`/`OVERWRITE` — остальные политики; `SUBFOLDER` — дефолт.
- Если включён `TEMPLATE` и коллизия в `Artist/Album/`, подпапка `1/` создаётся внутри `Artist/Album/`.
- `sanitize`: запрещённые символы, Windows reserved names, trailing dots/spaces, max length, NFC; не допускать `..` и абсолютных путей из шаблона.
- sidecars используют тот же stem и живут рядом с медиа:
  - `<stem>.lrc`, `<stem>.elrc`;
  - `<stem>.cover.jpg` (по умолчанию; в плоской папке `cover.jpg` может конфликтовать между артистами);
  - `<stem>.info.json` — только по `--keep-info-json`.
- При коллизии sidecars переезжают в ту же подпапку `1/`, `2/`, что и медиа; иначе `.lrc` потеряется.
- `--no-rename`, `--no-tag`, `--dry-run`; `--flat`/`--template` для выбора режима.

Пример flat (default):

```text
output/
  Skillet - Feel Invincible.m4a
  Skillet - Feel Invincible.lrc
  Skillet - Feel Invincible.cover.jpg
```

Коллизия:

```text
output/
  Skillet - Feel Invincible.m4a
  1/
    Skillet - Feel Invincible.m4a
    Skillet - Feel Invincible.lrc
```

Пример opt-in template:

```text
output/
  Skillet/
    Unleashed/
      05 - Feel Invincible.m4a
```

### 11.5. Никакой БД

- Нет индекса, истории, download archive.
- In-memory cache провайдеров с TTL; не переживает процесс.
- `.part` и staging — файловое состояние, не база.
- Конфиг — TOML; cookies/tokens — файлы/env.
- Если позже понадобится кэш провайдеров — это cache-файлы с TTL, не библиотека.

---

## 12. Ошибки и retry

```python
class AcquireError(Exception):
    retryable = False
    hint: str | None = None

class UnsupportedInput(AcquireError): ...
class UnsupportedScheme(AcquireError): ...
class UnsupportedUrl(AcquireError): ...
class AmbiguousInput(AcquireError): ...
class SearchUnavailable(AcquireError): ...
class AuthRequired(AcquireError): ...
class CookieError(AcquireError): ...
class ProxyError(AcquireError): ...
class GeoBlocked(AcquireError): ...
class DrmProtected(AcquireError): ...
class RateLimited(AcquireError): ...
class Unavailable(AcquireError): ...
class NoFormats(AcquireError): ...
class VariantUnavailable(AcquireError): ...
class DownloadFailed(AcquireError): ...
class PostprocessError(AcquireError): ...
class CancelledError(AcquireError): ...
class ConfigError(AcquireError): ...
```

| Ошибка | Сообщение/подсказка |
|---|---|
| `AuthRequired` | «нужны cookies; `tfg doctor --source youtube`; см. docs» |
| `CookieError` | «cookies не читаются/просрочены; обновите файл» |
| `ProxyError` | «прокси недоступен; проверьте `[sources.X].proxy`» |
| `GeoBlocked` | «контент недоступен в регионе; попробуйте прокси» |
| `DrmProtected` | «DRM; скачивание невозможно» |
| `RateLimited` | «лимит; повтор через N секунд» |
| `Unavailable` | «удалено/приватно/требует авторизации» |
| `NoFormats` | «нет доступных форматов» |
| `VariantUnavailable` | «формат исчез; обновите список» |
| `UnsupportedUrl` | «источник не поддерживается; `tfg search` или `--source`» |

Retry:

- сеть/429/5xx — retryable с backoff и jitter;
- auth/geo/DRM/unsupported — не retryable;
- ffmpeg/postprocess — не retryable, но можно предложить другой вариант;
- плейлист — partial failure, продолжать остальные.

---

## 13. Конкурентность и производительность

- По умолчанию `--jobs 1`; опционально до 4.
- Каждый job — свой `YoutubeDL`, свой staging, своя копия cookies.
- Per-source semaphore; не более N одновременных запросов к источнику.
- `concurrent_fragment_downloads` — внутри одного файла, отдельно от `--jobs`.
- Плейлист: flat resolve; полный resolve только выбранных; скачивание последовательное или ограниченно параллельное.
- Ленивый импорт yt-dlp: локальный `tfg info` не должен платить за импорт.
- `sanitize_info` только для нужных полей; не тащить весь `info_dict` в память для 1000-элементного плейлиста.
- Thumbnail preview — только по запросу.
- In-memory cache `SourceInfo` с TTL; не кэшировать signed URLs.
- Не использовать `download_archive` (это ещё и файловое состояние, и нежелательная «история»).

---

## 14. Безопасность

- Разрешать только `http`/`https`; `file://` — только явным флагом.
- Не передавать URL/параметры через shell; библиотека yt-dlp.
- Не включать `enable_file_urls`.
- Не использовать `--exec`, `--external-downloader`, произвольные `postprocessor_args` без валидации.
- Санитизировать имена файлов; не допускать `..`, абсолютных путей из title, NUL.
- Не логировать cookies, `Authorization`, `Cookie`, токены, proxy credentials.
- Cookies-файлы — 0600; копия на job; оригинал не мутировать.
- Не читать `.trackforge.toml` из cwd автоматически.
- Плагины yt-dlp — доверенный код; не устанавливать сторонние без понимания.
- SSRF: для локального CLI не критично; для будущего сервера — блокировать private IP/loopback и ограничивать redirects.
- Уважать ToS/авторские права; не обходить DRM/платный доступ; VK/YouTube cookies — ответственность пользователя.

---

## 15. Тестирование

### Unit

- `InputResolver`: URL/text/path, `file://`, несуществующий путь, Windows-путь.
- `LoaderRegistry`: priority, автоматический перебор, `supports_url`, `--source`, merged profile, `is_ready` (VK без токена), отсутствие loader'а.
- `classify_url`: таблица URL → hint; `None` для неоднозначных; отсутствие `is_playlist_url`/`is_post`.
- `YtDlpMapper`: видео, плейлист, `multi_video`, `url_transparent`, музыкальные поля, даты, thumbnails, `recommendations`.
- `VariantBuilder`: video-only+audio-only, progressive, audio-only, HDR, DRM, missing sizes, duplicate formats.
- `Recommendations`: YouTube MP4/M4A, Yandex FLAC/MP3, generic fallback, нет варианта → `None`, DRM не рекомендуется.
- `ThumbnailSelector`: площадь/preference/format, dedupe, placeholder, min-size, fallback-URL YouTube, `fetch_thumbnail` bytes/max_bytes.
- `SearchService`: вызывает `MediaLoader.search`, не `SearchProvider`; source-specific фильтры, дедуп/rank, ошибка одного loader'а не роняет остальные.
- `YtDlpProgressHook`: downloading/finished/error, bytes/estimate, postprocessor mapping, cancel.
- `OutputPlanner`: sanitize, reserved names, collisions, missing fields.
- `LrcWriter`: line sync, ELRC word sync, no fake timings.
- `Config`: precedence, `${ENV}`, mutually exclusive cookies, relative paths, unknown keys.
- `Errors`: mapping and hints.

### Integration

- Fake `YoutubeDL`/`extract_info` с записанными `info.json` (YouTube, Yandex, Pinterest, VK).
- Local HTTP server с маленьким mp3/mp4/jpg; `DirectAssetLoader` download + progress.
- `CliRunner` + fake registry: `tfg URL --json`, `tfg 'query' --pick 1`, `tfg file.mp3`.
- Staging commit: same FS, collision, cancel, cleanup.
- Ingest: local file → tags → rename → sidecar.

### Network (optional)

- `pytest -m network`, отключено по умолчанию.
- Не в CI; только вручную, с cookies/прокси.
- Не хранить реальные cookies/токены в репозитории.

### Security

- Секреты не попадают в `repr`, logs, JSON.
- Path traversal через title.
- Cookie file permissions.
- Proxy credentials redaction.
- Malformed info dict не роняет маппер.

---

## 16. Структура модулей

```text
src/trackforge/
  acquire/
    __init__.py          # публичный API: resolve, search, inspect, download
    models.py            # Input, SourceInfo, MediaItem, DownloadVariant, ...
    errors.py
    config.py            # AppConfig, SourceProfile, TOML loader
    progress.py          # ProgressEvent, ProgressSink, CancellationToken
    staging.py           # StagingArea, atomic commit
    http.py              # HttpClient, stream_to_file, HEAD
    registry.py          # LoaderRegistry, default loaders
    service.py           # AcquisitionService
    search.py            # SearchService, rank/dedupe (поиск — в loader'ах)
    thumbnails.py        # select_best_thumbnail, fetch_thumbnail_http
    variants.py          # grouping, default_recommendations
    loaders/
      __init__.py
      base.py            # MediaLoader, LoaderCapabilities, LoaderContext
      ytdlp.py           # YtDlpLoader, mapper, planner, progress hook
      youtube.py
      yandex_music.py
      pinterest.py
      vk.py              # VkApiLoader + VkAudioClient
      direct.py          # DirectAssetLoader
      local.py           # LocalSourceReader
  ingest/
    __init__.py
    pipeline.py          # IngestPipeline
    identify.py          # Identifier, AcoustID/Text identifiers
    providers.py         # MusicBrainz, CoverArt, LRCLIB
    lyrics.py            # LRC/ELRC writer, LyricsDocument
    naming.py            # OutputPlanner, sanitize
    sidecars.py
  cli/
    app.py               # default command + subcommands
    commands/
      input.py
      search.py
      inspect.py
      sources.py
      doctor.py
    render.py            # таблицы, прогресс, JSON
```

`metadata/` не трогаем; `acquire/` не импортирует `ingest/`; `ingest/` может импортировать оба. `acquire.models` может переиспользовать `PartialDate` из `metadata`, но не должен зависеть от бэкендов/metadata-файлов.

---

## 17. Дорожная карта

### P0 — каркас и данные

1. `models`, `errors`, `config`, `progress`, `registry` с автоматическим перебором и `profile_for`.
2. `SourceProfile` cookies/proxy + `doctor` gate **до** первого запроса к YouTube.
3. `YtDlpLoader` + mapper для одного видео; `recommendations`, `thumbnail`.
4. `YouTubeLoader` + плейлисты (flat/full inspect).
5. `YandexMusicLoader` (plugin `yt-dlp-yandex-music`) + SoundCloud.
6. `tfg inspect URL --json`; без скачивания и выбора.
7. Аудит Vimeo/Bandcamp/Pinterest-VK probes; фиксация `SourceInfo` v1.

### P1 — скачивание и выбор

1. `DownloadRequest`/planner, staging, flat names/`1/`.
2. `extract_info(download=True)`, merge/extract/remux, progress/cancel.
3. Плейлисты: partial failures.
4. `MediaLoader.search` + `SearchService`; `ytsearch`/`scsearch`.
5. CLI `--variant`/`--audio`/`--video`/`--yes`/`--json`.
6. Локальный файл → ingest boundary.

### P2 — расширение

1. `POST`-модель: Pinterest, VK wall, несколько медиа.
2. `DirectAssetLoader` (images/gif/pdf).
3. `VkApiLoader` (audio/playlists) + token/proxy.
4. `PinterestLoader` images.
5. Concurrency, per-source limits.
6. `.lrc` из субтитров/провайдеров.

### P3 — качество

1. AcoustID/MusicBrainz identification.
2. Cover Art / LRCLIB.
3. ELRC при word timings.
4. Naming/collision/atomic ingest.
5. `Analyzer` (LLM) как опциональный слой.
6. Server/worker mode, если понадобится.

---

## 18. Открытые вопросы

1. **yt-dlp обязателен или optional extra?** Если проект остаётся metadata-first — `trackforge[download]`; если download — основная функция, core dependency.
2. **Канал yt-dlp: stable или nightly?** Nightly чаще чинит сайты, но менее предсказуем. Для инструмента, завязанного на сайты, nightly может быть практичнее; решение — после MVP.
3. **Дефолтное качество видео.** Пользователь просил максимально возможное; предложение — max MP4/H.264/AAC, с опциональным `max_video_height` для ботов/слабых каналов.
4. **Дефолт для музыки.** YouTube — M4A/AAC; остальные — лучший original без transcode; MP3 — явная опция.
5. **Поиск по умолчанию.** Только YouTube или YouTube + SoundCloud? Каждый loader — отдельный запрос; для MVP один.
6. **Порог автовыбора в ingest.** Какой score считать достаточным? Нужны данные после первых прогонов.
7. **Хранить ли `.info.json` sidecar.** Полезно для провенанса/отладки, но это «мусор» рядом с медиа. Предложение: только `--keep-info-json`.
8. **ELRC.** Генерировать только при реальных word timings; иначе не обещать.
9. **VK API.** Доступность токена/методов и ToS; держать за флагом, не включать по умолчанию.
10. **Картинки.** Ограничиться direct/Pinterest или делать полноценный asset-пайплайн? Модель готова, реализация — по спросу.
11. **Thumbnail preview.** Внешние `chafa`/`viu` или только URL? Не тащить зависимость в core.
12. **JSON schema.** Версионировать с первого дня; какие поля считать стабильными?
13. **Плагины.** Entry points для сторонних loader'ов или только встроенные? Плагины — это код, вопрос доверия.
14. **Windows.** Санитизация имён, длина пути, `os.replace`, кодировки — нужен отдельный прогон.
15. **Бюджет thumbnail.** Сколько HEAD/GET допустимо в `get_info` (1–3?) и кэшировать ли результаты между вызовами.
16. **Поиск по сайтам.** Yandex/VK search через API может быть недоступен; держать ли его за флагом или включённым по умолчанию.

---

## Приложение A. Пример использования API

```python
from pathlib import Path
from trackforge.acquire import (
    AcquisitionService, DownloadOptions, DownloadRequest, ItemSelection,
    load_config,
)

config = load_config(Path("~/.config/trackforge/config.toml"))
service = AcquisitionService.from_config(config)

source = service.inspect("https://www.youtube.com/watch?v=...")
item = source.items[0]
print(source.title, source.creator)
print(source.thumbnail.url if source.thumbnail else None)
print(item.recommendations.video, item.recommendations.audio)
for variant in item.variants:
    print(variant.id, variant.label, variant.size_label)

request = DownloadRequest(
    source_info=source,
    selections=(ItemSelection(
        item_id=item.id,
        variant_id=item.recommendations.video or item.variants[0].id,
    ),),
    options=DownloadOptions(target_dir=Path("~/Downloads").expanduser()),
)
result = service.download(request, on_progress=my_sink)
print(result.files)
```

Для текста:

```python
candidates = service.search("Skillet - Feel Invincible", limit=10)
chosen = candidates[0]
source = service.inspect(chosen.url)
```

## Приложение B. Соответствие исходной иерархии

| Исходная идея | В архитектуре |
|---|---|
| 1. Базовый класс загрузчика | `MediaLoader` + `LoaderCapabilities` + `LoaderContext` |
| `supports_url` | без изменений, offline |
| `is_playlist_url` / `is_post` | **не нужны**; только `classify_url() -> UrlHint` |
| `get_info` | `get_info(SourceRef, flat, limit) -> SourceInfo` |
| `download(data, chosen_variant)` | `download(DownloadRequest) -> DownloadResult` |
| 2. Универсальный yt-dlp | `YtDlpLoader` |
| 3. Сайтовые наследники | `YouTubeLoader`, `YandexMusicLoader`, `PinterestLoader` |
| 4. VK API | `VkApiLoader` (от `MediaLoader`, не от yt-dlp) |
| 5. Прочее | `DirectAssetLoader`, `LocalSourceReader`, будущие `MediaLoader` |
| Поиск | `MediaLoader.search()`; `SearchService` только оркестрирует |
| Thumbnail | `MediaLoader.choose_thumbnail()` + `fetch_thumbnail()` |
| Рекомендации | `MediaLoader.recommend()` + `MediaItem.recommendations` |
| Прогресс | `ProgressSink` + `ProgressEvent` + hooks |
| Куки/прокси | `SourceProfile` per source, автоматически по `config_key` |
| Без БД | TOML + staging + in-memory cache |

---

## Приложение C. Быстрый чек-лист реализации

- [ ] `supports_url` не делает сеть.
- [ ] Реестр сам перебирает loaders по `priority` и берёт первый подходящий.
- [ ] Профиль (`cookies`/`proxy`/`token`) подставляется автоматически по `config_key`.
- [ ] `classify_url` возвращает `UNKNOWN`, если не уверен; отдельных `is_*` нет.
- [ ] Поиск реализован в конкретном `MediaLoader`, а не в общем провайдере.
- [ ] `get_info` отдаёт лучший `thumbnail` и `recommendations.audio/video`.
- [ ] `fetch_thumbnail` умеет вернуть байты для Telegram без CLI.
- [ ] `get_info` не скачивает медиа.
- [ ] `download` не пишет теги/обложки.
- [ ] Все URL-ы проходят через `http/https`-валидацию.
- [ ] Cookies используются in place, не копируются и не логируются.
- [ ] Proxy — per-source, не глобальный.
- [ ] Плейлист сначала flat, потом полный resolve выбранных.
- [ ] Signed URL не кэшируются.
- [ ] Прогресс не врёт при неизвестном `total_bytes`.
- [ ] Отмена оставляет/чистит staging по политике.
- [ ] Финальный файл появляется только атомарным переносом.
- [ ] Дефолтное имя — плоское `Author - Title.ext`; иерархия только по `naming.mode="template"`.
- [ ] Коллизия — подпапка `1/`, `2/`, без суффикса `(1)`.
- [ ] Sidecars используют тот же stem и переезжают в ту же подпапку.
- [ ] Ошибки типизированы и содержат hint.
- [ ] JSON не содержит секретов.
- [ ] `tfg 'Artist - Title'`, `tfg URL`, `tfg PATH`, `tfg input ...` работают одинаково предсказуемо.
