# 08. Metadata & Tagging

> Статус: **черновик v0.3**. Канонические поля, mapping в форматы, обложка, lyrics, нейминг и проверка записи.
> Цель — чтобы «правильные метаданные» были предсказуемы и одинаково понимались всеми слоями.

## 1. Принципы

1. **Каноническая модель — одна.** Формат — это отображение, а не источник истины.
2. **Не терять смысл.** `artist` и `albumartist`, `date` и `originaldate`, `title` и `derivative.title` — разные поля.
3. **Multi-value — это список, а не строка с разделителем.** На уровне канона; в формате — как поддерживается.
4. **Никаких выдуманных ID.** MBID/ISRC пишутся только из валидных claims.
5. **Оригинал не трогаем.** Пишем в output copy; read-back обязателен.
6. **Неизвестное — не пишем.** Absent field = unknown; не подставляем `""`/`0`/`Unknown` без решения.
7. **Совместимость важнее полноты.** Лучше меньше полей, но они корректно читаются плеерами.

## 2. Каноническая схема

### 2.1. Основные поля

| Поле | Тип | Multi | Комментарий |
|---|---|---|---|
| `title` | string | нет | Финальное display-title |
| `artists` | list[ArtistCredit] | да | main/featured/remixer с ролями |
| `albumartist` | string | нет | Для album/compilation |
| `album` | string | нет | Release или collection |
| `date` | date/string | нет | Дата release/загрузки |
| `originaldate` | date/string | нет | Дата оригинала |
| `track` / `tracktotal` | int | нет | Номер/всего |
| `disc` / `disctotal` | int | нет | Номер/всего |
| `genre` | list[string] | да | Жанры |
| `style` | list[string] | да | Стили (опционально) |
| `mood` | list[string] | да | Настроение (опционально) |
| `bpm` | int/float | нет | Измеренный BPM |
| `key` | string | нет | `F#m`, `Db`, ... |
| `language` | string | нет | ISO 639 |
| `script` | string | нет | ISO 15924 (опционально) |
| `label` | string | нет | Лейбл |
| `catalog_number` | string | нет | Каталожный номер |
| `barcode` | string | нет | UPC/EAN |
| `isrc` | string | нет | ISRC |
| `compilation` | bool | нет | Сборник |
| `comment` | string | нет | Короткий комментарий |
| `description` | string | нет | Описание (осторожно с длиной) |
| `source_url` | string | нет | URL источника |
| `source_id` | string | нет | ID на площадке |
| `copyright` | string | нет | Copyright |
| `license` | string | нет | Лицензия |
| `lyrics` | string | нет | Plain lyrics |
| `synced_lyrics` | string | нет | LRC/ELRC (sidecar по умолчанию) |
| `artwork` | image ref | нет | Front cover |
| `replaygain_*` | float | нет | Опционально |

### 2.2. Идентификаторы

| Поле | Формат |
|---|---|
| `mb_recording_id` | UUID |
| `mb_release_id` | UUID |
| `mb_release_group_id` | UUID |
| `mb_artist_id` | UUID |
| `mb_albumartist_id` | UUID |
| `acoustid` | UUID/string |
| `isrc` | 12 символов |

ID — отдельные поля, не «всё в comment». Валидируются.

### 2.3. ArtistCredit

```text
ArtistCredit:
  name
  role        # main | featured | remixer | composer | lyricist | producer
  mbid?
  sort_name?
  join_phrase?  # "feat.", "&", ...
```

- `artists` — упорядоченный список.
- Для тега `artist` обычно join main/featured; remixer — отдельно/в comment.
- `albumartist` — строка, не список (для совместимости).

### 2.4. Sort-поля

- `artist_sort`, `albumartist_sort`, `album_sort`, `title_sort`.
- Заполняются, если есть из MB/данных; иначе не пишем.
- Не генерируем «на глаз» для не-латиницы без правил.

### 2.5. Отсутствие значений

- `None`/absent — не пишем тег.
- Пустая строка — не пишем.
- `0` для track/disc — не пишем, если неизвестно.
- `Unknown` — только если политика явно решила; иначе absent.

## 3. Форматы

### 3.1. Матрица поддержки

| Поле | MP3 (ID3v2.4) | M4A (MP4) | FLAC | OGG Vorbis | Opus |
|---|---|---|---|---|---|
| title | TIT2 | ©nam | TITLE | TITLE | TITLE |
| artist | TPE1 | ©ART | ARTIST | ARTIST | ARTIST |
| albumartist | TPE2 | aART | ALBUMARTIST | ALBUMARTIST | ALBUMARTIST |
| album | TALB | ©alb | ALBUM | ALBUM | ALBUM |
| date | TDRC | ©day | DATE | DATE | DATE |
| originaldate | TDOR | freeform | ORIGINALDATE | ORIGINALDATE | ORIGINALDATE |
| track | TRCK | trkn | TRACKNUMBER | TRACKNUMBER | TRACKNUMBER |
| disc | TPOS | disk | DISCNUMBER | DISCNUMBER | DISCNUMBER |
| genre | TCON | ©gen | GENRE | GENRE | GENRE |
| bpm | TBPM | tmpo | BPM | BPM | BPM |
| key | TKEY | freeform | KEY | KEY | KEY |
| language | TLAN | freeform | LANGUAGE | LANGUAGE | LANGUAGE |
| label | TPUB | freeform | LABEL | LABEL | LABEL |
| catalog | TXXX | freeform | CATALOGNUMBER | CATALOGNUMBER | CATALOGNUMBER |
| barcode | TXXX | freeform | BARCODE | BARCODE | BARCODE |
| isrc | TXXX | freeform | ISRC | ISRC | ISRC |
| MBIDs | TXXX/UFID | freeform | MUSICBRAINZ_* | MUSICBRAINZ_* | MUSICBRAINZ_* |
| compilation | TCMP | cpil | COMPILATION | COMPILATION | COMPILATION |
| comment | COMM | ©cmt | COMMENT | COMMENT | COMMENT |
| lyrics | USLT | ©lyr | LYRICS | LYRICS | LYRICS |
| synced lyrics | sidecar | sidecar | sidecar | sidecar | sidecar |
| cover | APIC | covr | picture block | METADATA_BLOCK_PICTURE | METADATA_BLOCK_PICTURE |
| replaygain | TXXX/RVA2 | freeform | REPLAYGAIN_* | REPLAYGAIN_* | REPLAYGAIN_* |

«—» = нет стандартного/надёжного способа; используем sidecar.

### 3.2. MP3 / ID3

- ID3v2.4 по умолчанию; ID3v2.3 — опция совместимости.
- UTF-8/UTF-16; не latin-1.
- Multi-value: null-separated в v2.4; для v2.3 — разделитель (`/` или `;`) с caution.
- APIC: front cover, MIME, description.
- USLT: plain lyrics. SYLT не используем в v1; LRC/ELRC — sidecar.
- TXXX: `MusicBrainz Track Id`, `MusicBrainz Album Id`, `MusicBrainz Artist Id`, `Acoustid Id`, `CATALOGNUMBER`, `BARCODE`, `ISRC`, `TRACKFORGE_*`.
- `TCMP` для compilation (iTunes).
- `TDOR` для original date.
- `TSOT`/`TSOP`/`TSOA` для sort.
- `RVA2`/TXXX для ReplayGain — опционально.
- ID3v2.4 vs 2.3: некоторые старые плееры/Windows Explorer читают 2.3 лучше; дефолт 2.4, переключаемо.

### 3.3. M4A / MP4

- Atoms: `©nam`, `©ART`, `aART`, `©alb`, `©day`, `trkn`, `disk`, `©gen`, `tmpo`, `©lyr`, `covr`, `©cmt`, `©wrt`, `©too`, `cprt`, `cpil`.
- Freeform: `----:com.apple.iTunes:MusicBrainz Track Id`, `MusicBrainz Release Id`, `MusicBrainz Artist Id`, `MusicBrainz Album Artist Id`, `Acoustid Id`, `ISRC`, `LANGUAGE`, `KEY`, `LABEL`, `CATALOGNUMBER`, `BARCODE`, `TRACKFORGE_*`.
- Multi-value: MP4 atoms часто single-value; несколько артистов — несколько atoms или join; плееры читают по-разному. Для v1 — main artist + featured в строке, полный список — в sidecar.
- Lyrics: `©lyr` plain; synced — sidecar.
- Cover: `covr` JPEG/PNG.
- Sort: `soal`, `soar`, `soaa`, `sonm`.
- Date: `©day` ISO; original date — freeform.

### 3.4. FLAC / OGG / Opus

- Vorbis comments: UTF-8, `KEY=value`, multi-value — повторяющиеся ключи.
- Ключи uppercase: `TITLE`, `ARTIST`, `ALBUMARTIST`, `ALBUM`, `DATE`, `ORIGINALDATE`, `TRACKNUMBER`, `TRACKTOTAL`, `DISCNUMBER`, `DISCTOTAL`, `GENRE`, `BPM`, `KEY`, `LANGUAGE`, `LABEL`, `CATALOGNUMBER`, `BARCODE`, `ISRC`, `COMPILATION`, `COMMENT`, `LYRICS`, `UNSYNCEDLYRICS`, `MUSICBRAINZ_*`, `ACOUSTID_ID`, `REPLAYGAIN_*`.
- FLAC picture block; Ogg/Opus — `METADATA_BLOCK_PICTURE` (base64).
- Sort: `ARTISTSORT`, `ALBUMARTISTSORT`, `ALBUMSORT`, `TITLESORT`.
- Multi-value: repeat `ARTIST`, `GENRE`.
- Lyrics: `LYRICS`/`UNSYNCEDLYRICS`; synced — sidecar.
- FLAC tags length limits; avoid huge descriptions.

### 3.5. Что не пишем

- `encoder`/`encoded_by` — можно, но не приоритет.
- `rating`/`POPM` — пользовательские, не автоматически.
- `copyright`/`license` — только если есть валидный claim.
- `description` — если длиннее лимита, sidecar.
- `source_url` — в TXXX/freeform/comment, не в title.

## 4. Обложка

### 4.1. Выбор

- Front cover; CAA → iTunes → Deezer → source thumbnail.
- Разрешение: prefer >= 1000×1000; не upscale.
- Формат: JPEG (quality ~90) или PNG; MIME корректный.
- Ограничения: max ~10 MB, max ~4000×4000 (настраиваемо).
- Проверка: не битая, aspect ~1:1 (допустимы исключения), нет watermark (эвристика/LLM — позже).

### 4.2. Запись

- MP3: APIC (front cover, description empty/`Cover`).
- M4A: `covr`.
- FLAC: picture block (type 3 = front).
- Ogg/Opus: `METADATA_BLOCK_PICTURE`.
- Sidecar `cover.jpg` — опционально (по умолчанию embed only; user Q30).
- Если обложки нет — не пишем; warning `cover_missing`.

### 4.3. Старая обложка

- В output copy старые APIC/covr/pictures удаляем перед записью новой, чтобы не было дублей.
- Если новая не выбрана — можно сохранить старую, если она валидна; политика.

## 5. Lyrics

### 5.1. Форматы

- Plain — в тег, если формат поддерживает.
- LRC — **только sidecar** `<basename>.lrc`; в файл не встраиваем.
- ELRC — **только sidecar** `<basename>.elrc`, опционально.
- SYLT/встраивание синхронизированных lyrics — не используем в v1; можно добавить позже явной опцией.

### 5.2. Правила

- Не встраивать LRC в `©lyr`/`LYRICS` как plain: это ломает отображение.
- Plain lyrics — без таймкодов.
- LRC — с `[mm:ss.xx]`; ELRC — с word timestamps.
- Если lyrics нет — не пишем; `unknown`.
- Если lyrics на другом языке — language tag + warning.
- Максимальный размер — 256 KB; больше — sidecar + warning.
- Кодировка UTF-8.

### 5.3. Sidecar naming

```text
Artist - Title (Nightcore).m4a
Artist - Title (Nightcore).lrc
Artist - Title (Nightcore).elrc
trackforge.json
```

Если несколько output с одинаковым basename — collision suffix.

## 6. Нейминг

### 6.1. Шаблон

Default (Q28):

```text
{artist} - {title} ({qualifier})
```

- `{artist}` — основной артист (первый main); если несколько — `Artist feat. X` или первый + `&` (политика).
- `{title}` — финальный title без qualifier.
- `{qualifier}` — `Nightcore`, `Remix`, ...; если пусто — скобки не добавляются.
- Если title уже содержит qualifier (case-insensitive) — не дублируем.

### 6.2. Sanitization

- Запрещённые: `/ \ : * ? " < > |`, control chars, `\0`.
- Trim, collapse whitespace; убрать trailing dots/spaces.
- Не допускать `.`/`..`.
- Max length: 180 bytes на компонент (ext4 255, оставляем запас); обрезать по границе слова.
- Unicode — сохраняем; транслитерация — опционально, не по умолчанию.
- Reserved names Windows (`CON`, `PRN`, `AUX`, `NUL`, `COM1`...) — учитываем на будущее; sanitize уже сейчас.
- Эмодзи — можно, но не в начале/имени; по желанию.

### 6.3. Fallback

- Нет artist → `Unknown Artist`.
- Нет title → `Unknown Title`.
- Нет обоих → `Unknown Artist - Unknown Title [<short-id>]`.
- Нет collection → flat.
- Collection title пустой → `Collection [<short-id>]`.

### 6.4. Коллизии

- Если путь занят: ` (1)`, ` (2)`, ...
- Никогда не перезаписывать существующий файл.
- Если тот же bundle уже опубликован — считать publish идемпотентным.

### 6.5. Layout

- Одиночный трек: `library_dir/<artist> - <title> (<qualifier>).<ext>`.
- Плейлист/альбом: `library_dir/<collection>/<artist> - <title> (<qualifier>).<ext>`.
- Sidecar и lyrics — рядом с аудио.
- Детали — в `04-storage.md`.

## 7. Tag policy

### 7.1. Профили

| Профиль | Поля |
|---|---|
| `minimal` | title, artist, album, date, track, genre, cover |
| `standard` (default) | + albumartist, comment, lyrics, MBIDs, ISRC, language, source |
| `full` | + style/mood, label, catalog, barcode, replaygain, sort, custom |

### 7.2. Custom tags

Префикс `TRACKFORGE_`:

- `TRACKFORGE_SOURCE_URL`
- `TRACKFORGE_SOURCE_ID`
- `TRACKFORGE_ITEM_ID`
- `TRACKFORGE_DECISION_ID`
- `TRACKFORGE_DERIVATIVE`
- `TRACKFORGE_ORIGINAL_TITLE`
- `TRACKFORGE_ORIGINAL_ARTIST`

- ID3: TXXX.
- MP4: freeform `----:com.apple.iTunes:TRACKFORGE_*`.
- Vorbis: `TRACKFORGE_*`.
- Не пихать всё в `COMMENT`; comment — короткий.

### 7.3. Что не пишем автоматически

- rating/POPM;
- play count;
- copyright, если нет данных;
- license, если нет данных;
- encoder — опционально;
- огромный description.

## 8. Запись

### 8.1. Pipeline

1. Взять output audio (remux/transcode).
2. Прочитать существующие теги (если скопированы).
3. Построить новый tag set из `ResolvedMetadata` + tag policy.
4. Удалить конфликтующие старые поля (artwork, lyrics, MBIDs и т.п.).
5. Записать теги.
6. Записать обложку.
7. Записать lyrics (embed/sidecar).
8. `read-back`: прочитать и сравнить с каноном.
9. Если mismatch — ошибка рендера, не публиковать.
10. Записать sidecar.

### 8.2. Атомарность

- Всегда писать в temp-файл в том же root.
- `fsync` + `rename`.
- Для in-place (позже) — backup.
- Не менять исходный файл в CAS.

### 8.3. Сохранение неизвестных тегов

- По умолчанию **не сохраняем** неизвестные теги в output: они могут быть мусором/неверными.
- Source snapshot сохраняет исходные теги.
- Опция `preserve_unknown_tags=true` — для пользователей, которым нужно.
- Известные, но не выбранные поля не копируем автоматически.

## 9. Round-trip и валидация

- Для каждого формата: write → read → compare canonical.
- Проверять: title, artist, album, date, track, genre, BPM, key, cover, lyrics, MBIDs.
- Multi-value: сравнение как set/list с учётом порядка, где важно.
- Text encoding: ID3 UTF-8/UTF-16, Vorbis UTF-8.
- ID3v2.4 vs 2.3 — отдельные тесты.
- MP4 atoms — отдельные тесты.
- FLAC/OGG/Opus — picture block и comments.
- Ошибка read-back — fail render.

## 10. Примеры

### 10.1. Nightcore MP3

```text
TIT2 = Song (Nightcore)
TPE1 = Artist
TPE2 = Artist
TALB = Album
TDRC = 2019-05-01
TCON = Nightcore
TBPM = 172
TKEY = F#m
TLAN = eng
COMM = Nightcore edit; source: https://...
USLT = <plain lyrics>
APIC = <front cover>
TXXX:MusicBrainz Track Id = ...
TXXX:TRACKFORGE_DERIVATIVE = nightcore
```

### 10.2. Nightcore M4A

```text
©nam = Song (Nightcore)
©ART = Artist
aART = Artist
©alb = Album
©day = 2019-05-01
©gen = Nightcore
tmpo = 172
©lyr = <plain lyrics>
covr = <front cover>
----:com.apple.iTunes:MusicBrainz Track Id = ...
----:com.apple.iTunes:TRACKFORGE_DERIVATIVE = nightcore
```

### 10.3. Nightcore FLAC

```text
TITLE=Song (Nightcore)
ARTIST=Artist
ALBUMARTIST=Artist
ALBUM=Album
DATE=2019-05-01
GENRE=Nightcore
BPM=172
KEY=F#m
LANGUAGE=eng
LYRICS=<plain>
MUSICBRAINZ_TRACKID=...
TRACKFORGE_DERIVATIVE=nightcore
```

### 10.4. Sidecar

`trackforge.json` рядом; содержит item/decision/bundle, source, canonical metadata, identity, warnings, unknowns. Полный provenance — в БД/артефактах; sidecar — портативный снимок.

## 11. Открытые вопросы

- LRC/ELRC: решено — только sidecar, без embed в v1.
- Нужен ли `cover.jpg` sidecar при embed? Предложение: опционально, по умолчанию нет.
- Сохранять ли неизвестные теги? Предложение: нет, но source snapshot хранит.
- Писать ли ReplayGain по умолчанию? Предложение: нет, отдельный шаг.
- ID3v2.4 или 2.3 по умолчанию? Предложение: 2.4, 2.3 — опция.
- Как представлять нескольких артистов в MP4? Предложение: main + join; полный список — в sidecar.
- Максимальный размер обложки/lyrics.
- Транслитерация имён файлов: опция, не default.
- Нужны ли главы для аудиокниг в v1? Позже.
