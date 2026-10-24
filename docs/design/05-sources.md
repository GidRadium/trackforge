# 05. Sources

> Статус: **черновик v0.3**. Фиксирует, откуда TrackForge берёт аудио и метаданные, как устроены адаптеры и где границы поддержки.

## 1. Область

Источники делятся на четыре класса:

| Класс | Примеры | Что даёт | Поддержка в MVP |
|---|---|---|---|
| Локальные файлы | файл, папка | байты, существующие теги, путь | Да |
| `yt-dlp`-совместимые URL | YouTube, SoundCloud, Bandcamp, Vimeo... | байты, source metadata, playlist | Да (один трек) |
| Официальные/публичные API | MusicBrainz, AcoustID, CAA, LRCLIB, Deezer, iTunes | метаданные, обложки, lyrics, identity | Да |
| Кастомные сайты | парсеры/API конкретных площадок | байты/метаданные | Только как plugins, не в ядре |

Политика (Q27): **`yt-dlp` + официальные/публичные API + plugins**. Встроенных кастомных парсеров в v1 нет.

## 2. Контракт источника

### 2.1. `SourceResolver`

```text
SourceResolver:
  id, version
  supports(ref: SourceRef) -> bool
  resolve(ref: SourceRef, opts) -> Resolution
```

`Resolution` — либо:

- `CandidateList` (один трек, несколько вариантов качества/формата);
- `Collection` (плейлист/альбом/папка, упорядоченные элементы);
- `TextSearchResult` (кандидаты по описанию).

`resolve` не скачивает байты; он только обнаруживает и описывает.

### 2.2. `Candidate`

```text
Candidate:
  id
  source_ref
  external_id
  title, uploader, duration
  formats[]           # codec, bitrate, sample_rate, language, size, url, drm
  thumbnails[]
  extra               # source-specific
```

`Candidate` не является треком. Пользователь/политика выбирает его до acquire.

### 2.3. `Collection`

```text
Collection:
  kind                # playlist | album | channel | folder | other
  source_ref
  title, owner, description
  context_claims[]    # album, cover, year, genre, ...
  items[]             # position, candidate, item_id?, status
```

Коллекция — контекст, а не трек. Её метаданные могут использоваться политикой, но не должны автоматически перезаписывать данные recording.

## 3. Локальные файлы

### 3.1. Поддержка

- Один файл: `LocalFileSource`.
- Папка: `LocalFolderSource` → `Collection(kind=folder)`.
- Расширения MVP: `.mp3`, `.m4a`, `.flac`, `.ogg`, `.opus`.
- Симлинки: не следовать за пределы указанного root без явного разрешения.

### 3.2. Наблюдения

- `initial.tags.v1` — существующие теги (не стираем).
- `audio.probe.v1` — контейнер/кодек/длительность.
- `path.structure.v1` — путь/папки как слабое доказательство (`Artist/Album/Track`).
- `audio.fingerprint.v1` — Chromaprint для AcoustID.
- Embedded art — как кандидат, но не автоматически «правильная» обложка.

### 3.3. Импорт

- Байты копируются в CAS; исходник не изменяется.
- `FileInstance(role=source)` хранит исходный путь.
- Дубликаты по хэшу: один `MediaObject`, несколько `FileInstance`/`Item` (если разные источники/решения).
- Папка может стать `Collection` с `context_claims` из имён папок, но это лишь гипотезы.

## 4. `yt-dlp`

### 4.1. Почему subprocess

- Изоляция от нестабильного Python API и plugin-конфликтов.
- Легко зафиксировать версию/обновлять отдельно.
- JSON-вывод (`--dump-single-json`) стабильнее, чем внутренние объекты.
- Проще обрабатывать падения/таймауты.

`yt-dlp` вызывается списком аргументов (без shell), с таймаутом и ограничением вывода.

### 4.2. Основные режимы

| Задача | Режим |
|---|---|
| Метаданные одного трека | `--dump-single-json --no-download` |
| Список плейлиста | `--flat-playlist --dump-single-json` |
| Скачивание аудио | `-f bestaudio/best` + output template в temp |
| Прогресс | `--newline` + `--progress-template` (JSON-строка) |
| Cookies | `--cookies <path>` |
| Лимиты | `--sleep-requests`, `--min-sleep-interval`, `--max-sleep-interval`, `--retries` |
| Плейлист частично | `--playlist-start`, `--playlist-end`, `--playlist-items` |

Точные флаги зависят от версии `yt-dlp`; адаптер проверяет версию и использует поддерживаемые опции. Это фиксируется в spike.

### 4.3. Форматы

- Приоритет: audio-only (opus/m4a), затем bestaudio, затем best.
- Учитывать: codec, bitrate, sample rate, channels, language, DRM.
- DRM/платный контент — не поддерживаем; ошибка `DrmProtected`.
- Если контейнер не совпадает с целевым, render делает remux/transcode; исходные байты остаются в CAS.

### 4.4. Метаданные

Из `info.json` берём:

- `id`, `title`, `fulltitle`, `uploader`, `channel`, `upload_date`;
- `duration`, `tags`, `description`;
- `thumbnails`, `formats`;
- `track`, `artist`, `album`, `release_year`, `language` (если есть);
- `webpage_url`, `extractor`, `extractor_key`;
- `chapters` (для аудиокниг/подкастов позже).

Сырой JSON сохраняется как `source.snapshot.v1`; нормализованные поля — `source.metadata.v1`. Никогда не выбрасываем исходник: он нужен для аудита/повторной интерпретации.

### 4.5. Cookies

- Один файл на сайт: `~/.config/trackforge/cookies/<site>.txt`.
- Формат Netscape; права `0600`.
- Не логировать содержимое.
- Пользователь обновляет вручную (экспорт из браузера) или через будущий GUI.
- Ошибки: `AuthRequired`, `CookiesExpired`, `RateLimited`.
- Разные сайты — разные cookies; адаптер выбирает по домену.

### 4.6. JS runtime

Современный YouTube часто требует JS-челлендж. Нужен актуальный `yt-dlp` + JS runtime (например, Deno) и, возможно, `yt-dlp-ejs`. Точная конфигурация — в spike; `doctor` проверяет наличие и версию. Без runtime часть YouTube может быть недоступна — это видимая деградация, а не тихий сбой.

### 4.7. Плейлисты

- Enumeration: `--flat-playlist --dump-single-json` (без скачивания).
- `Collection(kind=playlist)` с позициями и `context_claims` (title/owner/description).
- Пользователь выбирает треки/диапазон; MVP — только один трек, плейлисты — следующий срез.
- Целевой максимум раннего релиза — **до 500 треков**; больше — позже.
- Скачивание по одному: не «всё сразу»; лимиты/паузы; resume по `CollectionItem.status`.
- `--ignore-errors` на уровне плейлиста, но каждый item получает свой статус.
- Ban avoidance: не параллелить aggressively; уважать `sleep`; хранить cookies; не ретраить бесконечно.

### 4.8. Ошибки

| Ошибка | Причина | Действие |
|---|---|---|
| `UnsupportedUrl` | нет поддерживающего resolver'а | user error/plugin |
| `LoginRequired` | нужны cookies | попросить cookies |
| `CookiesExpired` | cookies устарели | обновить |
| `RateLimited` | слишком много запросов | backoff/пауза |
| `GeoBlocked` | регион | сообщить |
| `DrmProtected` | DRM | не поддерживаем |
| `Unavailable` | удалено/приватное | сообщить |
| `ExtractorBroken` | изменился сайт | обновить yt-dlp/плагин |

Ошибки маппятся в доменные типы; сообщения — человекочитаемые, без стектрейсов в CLI.

## 5. Официальные/публичные API

### 5.1. MusicBrainz

- Клиент: `musicbrainzngs` (BSD-2).
- Rate limit: 1 req/s; обязательный `User-Agent` с контактом.
- Использование: search recordings/releases/artists; lookup по MBID; relationships (remix/cover), genres/tags.
- Кэш: ответы — артефакты с TTL; не долбить повторно.
- Ограничение: не все треки есть; MBID — сильный, но не обязательный якорь.

### 5.2. AcoustID

- `fpcalc` (Chromaprint) → fingerprint → AcoustID lookup.
- Нужен API key (бесплатный); rate limits.
- Результат: candidates с score + recording MBIDs.
- `fpcalc` — внешний бинарник; `doctor` проверяет.
- Лицензия Chromaprint — LGPL; вызываем как отдельный процесс.

### 5.3. Cover Art Archive

- Обложки по release/release-group MBID.
- Front/back/original; выбираем по размеру/качеству.
- Rate limits; кэш.
- Если нет — fallback на iTunes/Deezer/thumbnail источника.

### 5.4. LRCLIB

- Plain/LRC lyrics по title/artist/album/duration.
- Бесплатно, без auth; быть вежливым.
- Если нет — Genius (осторожно с ToS) или ASR.
- LRC может быть рассинхронизирован; confidence/валидация.

### 5.5. Deezer / iTunes

- Поиск треков/альбомов, обложки.
- Deezer: `cover_xl`; iTunes: artwork URL upscale.
- Без auth для публичных данных; rate limits.
- Использовать как альтернативу, не как истину.

### 5.6. Опциональные

- Discogs (токен, credits/labels).
- Last.fm (теги/похожие; API key).
- TheAudioDB (artwork/bios; key).
- Genius (lyrics; ToS/скрапинг).
- Spotify (метаданные; без аудио; условия API).
- Web search/fetch (Tavily/Brave/SearXNG + `trafilatura`) — для текстовых подсказок; только с ограничениями и логами.

### 5.7. Общие правила

- Один HTTP-клиент (`httpx`) с таймаутами/ретраями.
- Per-host rate limiter.
- `User-Agent` с названием/версией/контактом.
- Кэш ответов; TTL; `--refresh`.
- Не отправлять cookies туда, где они не нужны.
- Уважать ToS/robots; не обходить DRM/платный доступ.
- Секреты — только из env/keyring, не из БД.

## 6. Текстовый поиск

Вход: «примерное название, автор, описание».

Процесс:

1. Нормализовать запрос.
2. Поиск в нескольких провайдерах (MusicBrainz, Deezer, iTunes, YouTube search через yt-dlp, web search).
3. Собрать кандидатов с provenance.
4. Ranker/LLM упорядочивает.
5. Пользователь выбирает (или autopilot).
6. Дальше — обычный acquire/observe.

Ограничения:

- Текстовый запрос — это гипотеза, не факт.
- Нельзя автоматически скачивать первый результат без политики/подтверждения.
- Для MVP текстовый поиск можно отложить (MVP — файл/ссылка).

## 7. Кастомные источники (plugins)

### 7.1. Контракт

Плагин реализует `SourceResolver`/`Acquirer` и манифест:

```text
id, version, contract_version
kind: source | acquirer
supports(ref)
resolve(ref)
license
```

Регистрация — entry point `trackforge.sources` / `trackforge.acquirers`.

### 7.2. Правила

- Ядро не содержит кастомных парсеров.
- Плагин отвечает за свою легальность/ToS.
- Плагин не получает доступ к секретам других источников.
- Ошибка плагина не роняет ядро.
- Версия контракта проверяется; несовместимый плагин не загружается.

## 8. Кэш, лимиты, устойчивость

- HTTP-кэш: `hishel`/аналоги; TTL; ключ по URL+headers (без cookies!).
- Per-host rate limiter; глобальный лимит.
- Retry: только retryable (429/5xx/таймауты); exponential backoff + jitter.
- Circuit breaker: после N ошибок провайдер временно `degraded`.
- Cookies не кэшировать в HTTP-кэше.
- Логи: URL без query-секретов; ошибки без cookies.

## 9. Тестирование

- `yt-dlp` JSON-фикстуры (YouTube/SoundCloud/Bandcamp).
- HTTP-кассеты для MusicBrainz/AcoustID/CAA/LRCLIB.
- Fake `fpcalc`/`ffprobe` для unit-тестов.
- Live-тесты — отдельный marker, не в CI по умолчанию.
- Проверка, что cookies не попадают в логи/артефакты.

## 10. Открытые вопросы

- Точная конфигурация JS runtime для YouTube (Deno/yt-dlp-ejs) — spike.
- Пиннинг версии `yt-dlp`: обновлять часто или фиксировать? Предложение: диапазон + `doctor` предупреждает.
- Нужен ли keyring для cookies/API keys. Позже.
- Какой web search провайдер по умолчанию (Tavily/Brave/SearXNG). Позже.
- Нужен ли Spotify/Discogs в v1. Скорее нет.
- Политика плейлистов (выбор, лимиты, паузы) — следующий срез.
