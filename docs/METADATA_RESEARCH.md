# Метаданные аудио и видео: форматы, поля и требования сервисов

**Статус:** исследование для проекта TrackForge  
**Дата:** 2026-11-08  
**Ветка:** `dev-slow`  
**Связанные документы:** [IDEA.md](./IDEA.md)

---

## 0. Краткие выводы

1. **Универсальный класс возможен, но только для ограниченного набора полей.** Для 12–15 «ядерных» полей (title, artist, album, albumartist, track/disc, date, genre, comment, composer, lyrics, cover) можно сделать единый нормализованный API поверх большинства контейнеров. Для произвольных полей — нельзя: у RIFF INFO и ID3v1 фиксированные схемы, у MP4 и ASF — ограниченные наборы, у Vorbis/APEv2/Matroska — свободные ключи.
2. **Формат файла ≠ формат тегов.** Один контейнер может содержать несколько независимых теговых систем: MP3 — ID3v1 + ID3v2 + APEv2; WAV — RIFF INFO + ID3 chunk + BWF `bext`; MP4 — iTunes `ilst` + QuickTime `udta` + `mdta`; Matroska — несколько уровней `Tag`/`TargetTypeValue`. Универсальный класс должен сначала определить контейнер, затем теговую систему, и только потом читать поля.
3. **Разделять классы стоит — но по домену, а не по формату.** Разумная схема: общий `CommonMetadata`, `AudioMetadata` с альбомом/треком/диском и `VideoMetadata` с шоу/сезоном/эпизодом. Разбор контейнеров при этом общий, в виде набора адаптеров (`mutagen`, `ffmpeg`, `TagLib`), а не два независимых парсера.
4. **Обложку лучше вынести сразу.** Это бинарные данные с MIME-типом, типом изображения (front/back/artist), описанием, размерами и множественностью. В разных контейнерах она лежит в разных местах: ID3 `APIC`, FLAC `PICTURE`, Ogg `METADATA_BLOCK_PICTURE` (base64), MP4 `covr`, ASF `WM/Picture`, APEv2 binary item, Matroska attachment.
5. **Запись должна быть «capability-aware».** Не молча терять поля: для каждого формата нужна матрица «поле → поддержка → тип → кратность → лимит», а результат записи должен возвращать отчёт: что записано, что пропущено, что coerced/truncated.
6. **Приоритет форматов для MVP:** MP3, FLAC, M4A/MP4 audio, OGG/Opus, MP4/MOV video, MKV/WebM. Второй эшелон: WAV, AIFF, WMA/ASF, APE/WavPack/Musepack, MKA, AVI. Третий: DSF/DFF, FLV, MPEG-TS/PS, OGV.

---

## 1. Что считать «текстовыми метаданными»

В исследовании разделены три категории:

| Категория | Примеры | Откуда берётся | Входит в универсальный класс? |
|---|---|---|---|
| Описательные метаданные | title, artist, album, genre, comment, lyrics, composer, cover | теги контейнера | **Да**, это целевой объект |
| Технические свойства | duration, codec, bitrate, sample rate, channels, resolution, HDR, color space | заголовки контейнера/кодека | Нет, читаются ffprobe/парсером |
| Структурные данные | chapters, subtitle tracks, attachments, cues | контейнер | Нет, отдельные модели |

Граница не всегда чистая: `BPM`, `ISRC`, `ReplayGain`, `LANGUAGE`, `TLEN` — это теги, но они либо вычисляемые, либо справочные. Их можно включать в модель как **опциональные поля с типом**, но не смешивать с «описанием контента».

**Практическое правило:** универсальный класс работает с authored-полями; всё, что можно получить из декодера, он не хранит, а при необходимости запрашивает у отдельного `Probe`.

---

## 2. Ландшафт контейнеров и теговых систем

| Семейство | Контейнеры | Где лежат теги | Кодировка текста | Произвольные ключи | Мультизначения |
|---|---|---|---|---|---|
| **ID3v2** | MP3; AIFF/WAV/DSF/DFF через chunk | бинарные фреймы в начале/конце файла | ISO-8859-1, UTF-16, UTF-8 (v2.4) | `TXXX` | v2.4 — null-separated; v2.3 — чаще `/` |
| **Vorbis comments** | FLAC, Ogg Vorbis, Opus, Speex, Ogg FLAC, Ogg Theora | comment-блок FLAC / comment-пакет Ogg | UTF-8 | да | повторяющиеся ключи |
| **MP4 atoms** | M4A, M4B, MP4, M4V, MOV | `moov/udta/meta/ilst`, `udta`, `meta/keys` | UTF-8 / UTF-16 | freeform `----:mean:name` | несколько `data`-атомов |
| **RIFF INFO** | WAV, AVI | `LIST INFO` chunks | без маркера; обычно ASCII/CP1252 | нет, фиксированные fourCC | нет |
| **ASF objects** | WMA, WMV, ASF | Content Description / Extended Content Description / Metadata objects | UTF-16LE | именованные `WM/*` | ограниченно |
| **APEv2** | APE, WavPack, Musepack, TTA, TAK, OptimFROG; иногда MP3 | header/footer + items | UTF-8 | да | несколько значений на ключ |
| **Matroska tags** | MKV, MKA, WebM | `Tags/Tag/SimpleTag` | UTF-8 | да, с ABNF-именем | повторяющиеся `SimpleTag` |
| **Почти без тегов** | FLV, MPEG-TS, MPEG-PS/VOB | `onMetaData` (AMF0), PSI/SI, timed ID3 | AMF0/бинарный | нет | нет |

### 2.1. Три уровня расширяемости

Это ключ к дизайну универсального класса:

1. **Свободная схема:** Vorbis comments, APEv2, Matroska, MP4 freeform, ID3 `TXXX`. Можно хранить любое поле, но семантика — на совести приложения.
2. **Фиксированная схема + ограниченное расширение:** ID3v2 (стандартные фреймы + `TXXX`), MP4 `ilst` (стандартные атомы + freeform), ASF (`WM/*` + именованные атрибуты). Новое поле возможно, но не все плееры его увидят.
3. **Только фиксированная схема:** ID3v1, RIFF INFO. Здесь универсальный класс обязан либо отказывать, либо делать lossy-маппинг.

---

## 3. Форматы подробно

### 3.1. MP3 и ID3

MP3 — не контейнер в строгом смысле, а поток MPEG audio frames; теги приклеены снаружи.

**ID3v1 / ID3v1.1.** 128 байт в конце файла: `TAG` + title (30) + artist (30) + album (30) + year (4) + comment (30) + genre (1). В v1.1 последний байт comment — номер трека, а comment урезан до 28 байт. Кодировка — ISO-8859-1 (на практике часто CP1251/CP1252), обрезка по 30 байтам, один genre-индекс. Годится только как fallback для старых плееров и Windows Explorer.

**ID3v2.2.** Трёхбуквенные идентификаторы фреймов, вышел из употребления. Поддерживать только чтение.

**ID3v2.3.** Основной «совместимый» вариант: ISO-8859-1 или UTF-16 с BOM, размер фрейма — обычный 32-битный big-endian, даты — `TYER`/`TDAT`/`TIME`, несколько значений в текстовых фреймах часто склеены через `/` без escaping. Его до сих пор лучше понимают старые аппаратные плееры, iTunes и часть автомобильных головных устройств.

**ID3v2.4.** Современный вариант: кодировки 0–3 (ISO-8859-1, UTF-16 с BOM, UTF-16BE, UTF-8), syncsafe-размеры фреймов и всего тега, текст может содержать несколько значений через `\0`, даты — `TDRC`/`TDRL`/`TDOR`/`TDEN`/`TDTG` в ISO 8601, есть footer и per-frame unsynchronisation. Именно его рекомендуют Jellyfin и современные теггеры.

Основные фреймы:

| Поле | ID3v2.3/2.4 | Примечание |
|---|---|---|
| Title | `TIT2` | |
| Artist | `TPE1` | |
| Album artist | `TPE2` | в v2.3/2.4 |
| Album | `TALB` | |
| Track | `TRCK` | текст `N` или `N/T` |
| Disc | `TPOS` | текст `N` или `N/T` |
| Date | `TDRC` (2.4); `TYER`+`TDAT`+`TIME` (2.3) | частичные даты |
| Genre | `TCON` | текст или `(17)` |
| Comment | `COMM` | language + description + text |
| Composer | `TCOM` | |
| Lyricist | `TEXT` | |
| Lyrics | `USLT` | language + description |
| Cover | `APIC` | MIME + picture type + description |
| Sort title | `TSOT` | |
| Sort artist | `TSOP` | |
| Sort album | `TSOA` | |
| Sort albumartist | `TSO2` | iTunes-расширение |
| Compilation | `TCMP` | iTunes-расширение |
| BPM | `TBPM` | |
| ISRC | `TSRC` | |
| Key | `TKEY` | |
| Language | `TLAN` | ISO 639-2 |
| Grouping | `GRP1` | iTunes-расширение |
| Copyright | `TCOP` | |
| Label | `TPUB` | |
| Conductor | `TPE3` | |
| Producer | `TIPL` / `TXXX:PRODUCER` | `TIPL` — 2.4 |
| MusicBrainz IDs | `TXXX:MusicBrainz ...` / `UFID` | вне стандарта ID3 |
| Произвольное | `TXXX:description` | |

**Что писать.** Разумная политика: по умолчанию ID3v2.4 + UTF-8; опция «совместимость» — ID3v2.3 + UTF-16. ID3v1 писать только по флагу, понимая, что это lossy. APEv2 на MP3 — только если он уже есть или включён явно, иначе получится три конкурирующих набора тегов.

**Подводные камни:** несколько ID3v2-тегов в файле; unsynchronisation; «пустые» фреймы; padding; ID3v2.3 с UTF-16 и BOM; ID3v2.4, который некоторые плееры читают как 2.3; жанровые индексы; `TRCK` с ведущими нулями.

### 3.2. FLAC

FLAC — это поток метаданных-блоков перед аудио: `STREAMINFO`, `PADDING`, `APPLICATION`, `SEEKTABLE`, `VORBIS_COMMENT`, `CUESHEET`, `PICTURE`. Текстовые поля — в `VORBIS_COMMENT` (тип 4), обложка — в `PICTURE` (тип 6).

- Кодировка — **UTF-8**, ключи — ASCII, регистронезависимые, значение после `=`.
- Схема не фиксирована: `TITLE`, `ARTIST`, `ALBUM`, `ALBUMARTIST`, `TRACKNUMBER`, `TRACKTOTAL`, `DISCNUMBER`, `DISCTOTAL`, `DATE`, `GENRE`, `COMMENT`, `COMPOSER`, `LYRICS`, `MUSICBRAINZ_*`, `REPLAYGAIN_*` — это конвенции, а не жёсткий стандарт.
- Мультизначения — повторяющиеся ключи.
- `PADDING` позволяет дописывать теги без перезаписи всего файла.
- ID3v2 в FLAC — **нестандартно**; некоторые файлы так размечены, но писать так не надо. Jellyfin отдельно предупреждает, что FLAC с ID3-тегами может плохо играть в браузерах.

### 3.3. Ogg Vorbis и Opus

Ogg — контейнер страниц; у Vorbis comment-заголовок свой, у Opus — `OpusTags`, но формат комментариев общий: vendor string + список `KEY=value` в UTF-8.

- Ключи: ASCII `0x20..0x7D`, кроме `=`, регистронезависимые; значения — UTF-8; дубликаты допустимы и являются мультизначением.
- Обложка: `METADATA_BLOCK_PICTURE` — base64 от FLAC `PICTURE`-блока; исторически также `COVERART`/`COVERARTMIME`.
- Opus: `R128_TRACK_GAIN`, `R128_ALBUM_GAIN` (Q7.8 dB) — не ReplayGain, но близко по смыслу.
- Ogg FLAC, Speex, Theora используют тот же механизм комментариев; у Theora — свой заголовок комментариев.

### 3.4. MP4 / M4A / MOV

ISO Base Media File Format (ISO/IEC 14496-12) — это дерево боксов. Текстовые метаданные могут лежать в трёх разных местах:

1. **iTunes-style `moov/udta/meta/ilst`** — основной вариант для `.m4a`, `.m4b`, `.mp4`. Каждый элемент — атом с именем из четырёх байт (`©nam`, `©ART`, `©alb`, `aART`, `trkn`, `disk`, `covr`, ...), внутри — `data`-атом с флагом типа (UTF-8 = 1, UTF-16 = 2, JPEG = 13, PNG = 14, integer = 21).
2. **QuickTime `moov/udta`** — «пользовательские» атомы (`©nam`, `©dir`, `©prd`, `©wrt`, `©day`, `©cpy`), с языковыми вариантами. Это не то же самое, что `ilst`.
3. **QuickTime `moov/meta/keys` + `ilst`** — `mdta`-ключи вида `com.apple.quicktime.title`, `com.apple.quicktime.artist`, `com.apple.quicktime.creationdate`, `com.apple.quicktime.location.ISO6709`.

Для видео дополнительно используются TV-атомы: `tvsh` (show), `tven` (episode id), `tvsn` (season), `tves` (episode), `tvnn` (network), `stik` (media kind), `desc`/`ldes` (description), `purl`, `egid`, `catg`, `keyw`.

**Ключевые атомы:**

| Поле | Атом |
|---|---|
| Title | `©nam` |
| Artist | `©ART` |
| Album | `©alb` |
| Album artist | `aART` |
| Track | `trkn` (binary: reserved, number, total, reserved) |
| Disc | `disk` (binary) |
| Date | `©day` |
| Genre | `©gen` (текст) / `gnre` (ID3-индекс) |
| Comment | `©cmt` |
| Composer | `©wrt` |
| Lyrics | `©lyr` |
| Cover | `covr` |
| Sort title/artist/album/albumartist | `sonm`, `soar`, `soal`, `soaa` |
| Compilation | `cpil` |
| BPM | `tmpo` |
| Copyright | `cprt` |
| Произвольное | `----:com.apple.iTunes:Field` |

Freeform-атом `----` состоит из `mean` (обычно `com.apple.iTunes`), `name` (имя поля) и `data`; так хранят MusicBrainz IDs, ISRC, LABEL, LANGUAGE, CONDUCTOR, LYRICIST, REPLAYGAIN_*.

**Подводные камни:** `trkn`/`disk` — не текст, а бинарные пары; `©day` — свободная строка, а не ISO-дата; `covr` не хранит тип обложки (front/back); `ilst` и `udta` могут противоречить друг другу; `.mp4` может быть и аудио, и видео; `.m4a` — соглашение об имени, а не отдельный формат.

### 3.5. WAV и AIFF

**WAV** — RIFF-контейнер. Текстовые метаданные:
- `LIST INFO` с fourCC: `INAM` (title), `IART` (artist), `IPRD` (album/product), `ITRK`/`IPRT` (track), `ICRD` (date), `IGNR` (genre), `ICMT` (comment), `ICOP` (copyright), `ISFT` (software), `IENG` (engineer), `ILNG` (language), `IWRI` (writer), `IMUS` (composer), `IPUB` (publisher), `ISRC`.
- `id3 ` chunk с полноценным ID3v2 — так делает Picard и многие теггеры.
- BWF `bext` — broadcast-поля (description, originator, date, time reference), не пользовательские теги.

**AIFF** — IFF-контейнер: текстовые чанки `NAME`, `AUTH`, `ANNO`, `(c) `, а для богатых полей — `ID3 ` chunk.

Ни WAV, ни AIFF не имеют стандартных albumartist/disc/cover в RIFF/IFF-схеме. Picard прямо пишет: у WAV нет стандарта тегирования, поэтому он использует ID3v2, а RIFF INFO — только для совместимости, с ограничениями по полям и кодировкам.

### 3.6. DSF и DFF (DSD)

DSF (Sony) хранит ID3v2-тег в конце файла, на него указывает offset в заголовке. DFF (DSDIFF) хранит ID3-тег отдельным chunk. Набор полей — тот же ID3v2.3/2.4, поэтому DSF/DFF можно обрабатывать тем же ID3-адаптером, но с доступом к файлу целиком (тег в конце).

### 3.7. ASF / WMA / WMV

ASF — объектная модель: Header Object содержит Content Description Object (Title, Author, Copyright, Description, Rating), Extended Content Description Object (имя/значение, обычно `WM/*`), Metadata Object и Metadata Library Object. Строки — UTF-16LE.

Типичные имена: `WM/AlbumTitle`, `WM/AlbumArtist`, `WM/TrackNumber`, `WM/PartOfSet`, `WM/Year`, `WM/Genre`, `WM/Composer`, `WM/Conductor`, `WM/Writer`, `WM/Lyrics`, `WM/Publisher`, `WM/ISRC`, `WM/BeatsPerMinute`, `WM/Language`, `WM/Picture`, `WM/IsCompilation`, `MusicBrainz/*`.

ASF покрывает и аудио (WMA), и видео (WMV). Схема более фиксированная, чем Vorbis/Matroska, но расширяемая именованными атрибутами; не все плееры понимают нестандартные `WM/*`.

### 3.8. APEv2 и семейство APE

APEv2 — key/value-теги в конце (или начале, если есть header) файла. Ключи — ASCII без control-символов, значения — UTF-8; у item есть флаг text/binary/external, поэтому в APEv2 можно хранить обложку как binary item. Схема свободная, но канонические имена (`Title`, `Artist`, `Album`, `Album Artist`, `Track`, `Disc`, `Year`, `Genre`, `Comment`, `Composer`, `Lyrics`, `Cover Art (Front)`) — конвенция.

Основные носители: Monkey's Audio (`.ape`), WavPack (`.wv`), Musepack (`.mpc`), TTA, TAK, OptimFROG. На MP3 APEv2 встречается как альтернатива ID3 (например, старый foobar2000), но это уже «третий набор тегов» и источник конфликтов.

### 3.9. Matroska / WebM

Matroska — EBML-контейнер. Теги — `Tags` → `Tag` → `Targets` + `SimpleTag` (`TagName`, `TagString`, `TagLanguage`, `TagDefault`, `TagBinary`). Всё в UTF-8.

- `TargetTypeValue` задаёт уровень: 70 — collection, 60 — edition/volume/opus (произведение), 50 — album/opera/concert, 40 — part/session, 30 — track/song, 20 — subtrack/movement, 10 — lowest.
- Имена тегов — ABNF `[A-Z0-9][A-Z0-9_]*`; список назначенных имён включает `TITLE`, `SUBTITLE`, `ARTIST`, `LEAD_PERFORMER`, `ACCOMPANIMENT`, `COMPOSER`, `ARRANGER`, `LYRICS`, `LYRICIST`, `CONDUCTOR`, `DIRECTOR`, `PRODUCER`, `WRITTEN_BY`, `SCREENPLAY_BY`, `SUMMARY`, `SYNOPSIS`, `GENRE`, `MOOD`, `KEYWORDS`, `DATE_RELEASED`, `DATE_RECORDED`, `PART_NUMBER`, `TOTAL_PARTS`, `LABEL`, `PUBLISHER`, `COPYRIGHT`, `LAW_RATING` и др.
- Несколько значений — несколько `SimpleTag` с одним именем; вложенные `SimpleTag` описывают атрибуты родителя (например, `ACTOR` → `CHARACTER`).
- Даты — `YYYY-MM-DD hh:mm:ss.mss` или укороченные формы, UTC.
- Обложка — обычно attachment (`cover.jpg`/`cover.png`), а не тег.

Для видео Matroska — самый выразительный из массовых контейнеров: есть `DIRECTOR`, `PRODUCER`, `WRITTEN_BY`, `SUMMARY`, `SYNOPSIS`, `KEYWORDS`, `LAW_RATING`. Но отдельных стандартных `SEASON`/`EPISODE` нет: сезон/эпизод выражают через иерархию `TargetTypeValue` + `PART_NUMBER`/`TOTAL_PARTS` либо нестандартные теги, что разные инструменты делают по-разному.

WebM — подмножество Matroska; `Tags`/`SimpleTag` в спецификации есть, но поддержка в инструментах неоднородна. Для WebM безопаснее считать теги best-effort.

### 3.10. AVI

AVI — RIFF-контейнер. Текстовые метаданные — `LIST INFO` (`INAM`, `IART`, `ICMT`, `ICRD`, `IGNR`, `IPRD`, `ITRK`, `ISFT`, `ICOP`, `IENG`, `IDIT`), плюс иногда `ID3 ` chunk. Нет стандартных albumartist, disc, cover, lyrics, multi-values. Для видео этого мало: director/producer можно положить только в «похожие» поля (`IART`, `ICNM`, `IPRO`) или в ID3 chunk, который прочитает не каждый плеер.

### 3.11. FLV

FLV хранит `onMetaData` — AMF0-объект с полями duration, width, height, videodatarate, framerate, videocodecid, audiodatarate, audiosamplerate, audiosamplesize, stereo, audiocodecid, filesize, encoder. Это техническая шапка, а не теговая система: title/author/description могут быть добавлены как AMF-поля, но rich-метаданных (album, track, cover, lyrics) там нет. FLV — формат доставки/захвата; для библиотеки его лучше ремуксить в MP4/MKV.

### 3.12. MPEG-TS и MPEG-PS/VOB

В MPEG-TS нет стандартных файловых тегов: есть PSI/SI (service information), а описательные метаданные — в лучшем случае timed ID3 (HLS) или sidecar-файлы. MPEG-PS/VOB ещё беднее; DVD-метаданные лежат в IFO. Для библиотеки это acquisition-форматы: после получения файл ремуксится в MKV/MP4, где теги есть.

### 3.13. Ogg Theora / OGV

Theora использует comment-заголовок (аналог Vorbis comments) для title/artist/description, но видео-специфичных полей (show/season/episode) там нет. Формат редкий; поддержка — низкий приоритет.

### 3.14. Что осталось за рамками

MXF (broadcast KLV), CAF, MOD/XM/S3M/IT, MIDI, AIFF-C с компрессией, WavPack correction files, encrypted M4P/AAX. Их можно добавить позже как отдельные адаптеры; в универсальную модель они не обязаны попадать с первого дня.

---

## 4. Поля и матрица поддержки

### 4.1. Нормализованная модель полей

| Поле | Тип | Семантика |
|---|---|---|
| `title` | str | название трека/ролика |
| `title_sort` | str | ключ сортировки |
| `artist` | str | основной исполнитель (display) |
| `artists` | list[str] | все исполнители |
| `artist_sort` | str | ключ сортировки исполнителя |
| `album_artist` | str | исполнитель альбома |
| `album` | str | альбом/релиз |
| `album_sort` | str | ключ сортировки альбома |
| `track_number`, `track_total` | int | номер и всего треков |
| `disc_number`, `disc_total` | int | номер и всего дисков |
| `date` | PartialDate | дата релиза/записи, может быть неполной |
| `original_date` | PartialDate | оригинальная дата релиза |
| `genre` | list[str] | жанры |
| `comment` | str | комментарий |
| `composer` | list[str] | композитор(ы) |
| `lyricist` | list[str] | автор текста |
| `lyrics` | Lyrics | текст + язык + synced/unsynced |
| `conductor` | list[str] | дирижёр |
| `producer` | list[str] | продюсер |
| `label` | str | лейбл/издатель |
| `copyright` | str | copyright |
| `isrc` | str | ISRC |
| `bpm` | int | BPM |
| `key` | str | тональность |
| `language` | str | язык |
| `grouping` | str | группировка |
| `compilation` | bool | сборник |
| `musicbrainz_ids` | dict[str, str] | MBIDs |
| `replaygain` | dict[str, float] | track/album gain/peak |
| `extra` | dict[str, list[str]] | неизвестные/нестандартные теги |

Видео-поля:

| Поле | Тип | Семантика |
|---|---|---|
| `show` | str | название шоу/сериала |
| `season` | int | сезон |
| `episode` | int | номер эпизода |
| `episode_id` | str | id эпизода |
| `network` | str | телесеть/платформа |
| `director` | list[str] | режиссёр |
| `writer` | list[str] | сценарист |
| `synopsis` | str | описание/сюжет |
| `rating` | str | возрастной рейтинг |
| `keywords` | list[str] | ключевые слова |
| `location` | str | место съёмки |

`PartialDate` — отдельный тип, а не `datetime`: `2024`, `2024-05`, `2024-05-17` — валидные значения. Иначе нельзя честно прочитать ID3v2.4 `TDRC`, Vorbis `DATE`, MP4 `©day` и Matroska `DATE_RELEASED`.

### 4.2. Матрица «формат × группы возможностей»

Обозначения: `●` — штатное поле; `◐` — только через generic/extension/ID3-chunk/attachment; `○` — штатного способа нет; `—` — неприменимо. Колонка «Альбом/трек/диск» для видео означает иерархию «шоу/сезон/эпизод».

| Формат | Ядро (title/artist/album) | Альбом/трек/диск | Кредиты | Lyrics | ID/тех. | Произвольные ключи | Обложка | Мультизначения |
|---|---|---|---|---|---|---|---|---|
| MP3 (ID3v2.4) | ● | ● | ● | ● | ● | ● `TXXX` | ● `APIC` | ● |
| MP3 (ID3v1) | ● | ◐ track only | ○ | ○ | ○ | ○ | ○ | ○ |
| FLAC | ● | ● | ● | ● | ● | ● | ● `PICTURE` | ● |
| Ogg Vorbis | ● | ● | ● | ● | ● | ● | ● base64 | ● |
| Ogg Opus | ● | ● | ● | ● | ● | ● | ● base64 | ● |
| M4A / MP4 audio | ● | ● | ● | ● | ● | ● freeform | ● `covr` | ● |
| WAV | ● `INFO`/ID3 | ◐ ID3 | ◐ ID3 | ◐ ID3 | ◐ | ◐ | ◐ ID3 | ○ |
| AIFF | ● `NAME`/ID3 | ◐ ID3 | ◐ ID3 | ◐ ID3 | ◐ | ◐ | ◐ ID3 | ○ |
| DSF / DFF | ● ID3 | ● ID3 | ● ID3 | ● ID3 | ● ID3 | ● ID3 | ● `APIC` | ● |
| WMA / ASF | ● | ● | ● | ● | ● | ● `WM/*` | ● `WM/Picture` | ◐ |
| APE / WV / MPC | ● | ● | ● | ● | ● | ● | ● binary | ● |
| MKA (Matroska) | ● | ● | ● | ● | ● | ● | ● attachment | ● |
| MP4 / MOV video | ● | ● TV atoms | ● | ◐ | ● | ● freeform | ● `covr` | ● |

| MKV / WebM | ● | ● | ● | ● | ● | ● | ● attachment | ● |
| AVI | ● `INFO` | ◐ | ◐ | ○ | ◐ | ◐ | ○ | ○ |
| WMV / ASF | ● | ● | ● | ● | ● | ● | ● | ◐ |
| FLV | ◐ `onMetaData` | ○ | ○ | ○ | ◐ | ◐ | ○ | ○ |
| MPEG-TS / PS | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| OGV (Theora) | ● comments | ○ | ◐ | ○ | ◐ | ◐ | ○ | ● |

### 4.3. Аудио: ключи основных полей

| Формат | Title | Artist | Album | Album artist | Track | Disc | Date | Genre | Lyrics | Cover |
|---|---|---|---|---|---|---|---|---|---|---|
| MP3 | `TIT2` | `TPE1` | `TALB` | `TPE2` | `TRCK` | `TPOS` | `TDRC`/`TYER` | `TCON` | `USLT` | `APIC` |
| FLAC | `TITLE` | `ARTIST` | `ALBUM` | `ALBUMARTIST` | `TRACKNUMBER`+`TRACKTOTAL` | `DISCNUMBER`+`DISCTOTAL` | `DATE` | `GENRE` | `LYRICS` | `PICTURE` block |
| Ogg | `TITLE` | `ARTIST` | `ALBUM` | `ALBUMARTIST` | `TRACKNUMBER`+`TRACKTOTAL` | `DISCNUMBER`+`DISCTOTAL` | `DATE` | `GENRE` | `LYRICS` | `METADATA_BLOCK_PICTURE` |
| M4A | `©nam` | `©ART` | `©alb` | `aART` | `trkn` | `disk` | `©day` | `©gen` | `©lyr` | `covr` |
| WAV | `INAM`/ID3 | `IART`/ID3 | `IPRD`/ID3 | ID3 | `ITRK`/ID3 | ID3 | `ICRD`/ID3 | `IGNR`/ID3 | ID3 | ID3 |
| AIFF | `NAME`/ID3 | `AUTH`/ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 |
| DSF | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | ID3 | `APIC` |
| WMA | `WM/Title` | `WM/Author` | `WM/AlbumTitle` | `WM/AlbumArtist` | `WM/TrackNumber` | `WM/PartOfSet` | `WM/Year` | `WM/Genre` | `WM/Lyrics` | `WM/Picture` |
| APE | `Title` | `Artist` | `Album` | `Album Artist` | `Track` | `Disc` | `Year`/`Date` | `Genre` | `Lyrics` | `Cover Art (Front)` |
| MKA | `TITLE` | `ARTIST` | `ALBUM` | `ARTIST@50` | `PART_NUMBER` | `PART_NUMBER` (50/60) | `DATE_RELEASED` | `GENRE` | `LYRICS` | attachment |

### 4.4. Видео: ключи основных полей

| Формат | Title | Show | Season | Episode | Director | Synopsis | Cover |
|---|---|---|---|---|---|---|---|
| MP4/MOV | `©nam`/`com.apple.quicktime.title` | `tvsh` | `tvsn` | `tven`/`tves` | `©dir`/freeform | `desc`/`ldes` | `covr` |
| MKV/WebM | `TITLE` | `TITLE@50`/custom | custom (нет стандарта) | `PART_NUMBER@30`/custom | `DIRECTOR` | `SUMMARY`/`SYNOPSIS` | attachment |
| AVI | `INAM` | ○ | ○ | ○ | `IART`/`ICNM` | ○ | ○ |
| WMV | `Title` | ○ | ○ | ○ | `WM/Director`* | `Description` | `WM/Picture` |
| FLV | `onMetaData.title` | ○ | ○ | ○ | ○ | `onMetaData.description` | ○ |
| TS/PS | ○ | ○ | ○ | ○ | ○ | ○ | ○ |

\* `WM/*` — расширяемые именованные атрибуты; конкретное имя зависит от писателя и читателя.

---

## 5. Семантика, которая ломает «универсальный класс»

### 5.1. Кодировка

- ID3v1 — ISO-8859-1; на практике встречаются CP1251/CP1252, и восстановить кодировку без эвристики нельзя.
- ID3v2.3 — ISO-8859-1 или UTF-16 с BOM; UTF-8 нет.
- ID3v2.4 — UTF-8, UTF-16, UTF-16BE.
- Vorbis/APEv2/Matroska — UTF-8.
- MP4 — UTF-8 (тип 1) или UTF-16 (тип 2); freeform тоже.
- ASF — UTF-16LE.
- RIFF INFO — кодировка не задана; обычно ASCII/CP1252, иногда UTF-8 «как повезёт».

Вывод: модель хранит `str` (Unicode), а адаптер отвечает за encode/decode и **возвращает warning**, если пришлось угадывать кодировку.

### 5.2. Мультизначения

| Система | Как хранит | Как читать/писать |
|---|---|---|
| ID3v2.4 | несколько строк через `\0` | split по `\0`, писать `\0` |
| ID3v2.3 | часто `/` | split эвристикой; при записи join `/` (lossy) |
| Vorbis | повторяющиеся `KEY=value` | список |
| MP4 | несколько `data`-атомов / несколько freeform | список |
| APEv2 | несколько значений на ключ | список |
| Matroska | несколько `SimpleTag` | список |
| ASF | обычно одно значение | ограниченно |
| RIFF INFO | одно значение | одно |

**Нельзя** хранить список в одном string с разделителем «по умолчанию»: `AC/DC`, `Simon & Garfunkel`, `Earth, Wind & Fire` ломаются. Разделитель — только явная настройка формата/плеера.

### 5.3. Даты

- ID3v2.4 `TDRC` — ISO 8601, допускает `YYYY-MM-DDTHH:MM:SS`.
- ID3v2.3 — отдельные `TYER`, `TDAT` (DDMM), `TIME` (HHMM).
- Vorbis `DATE` — свободная строка; встречаются `YYYY`, `YYYY-MM-DD`, `YYYY-MM-DD HH:MM:SS`.
- MP4 `©day` — свободная строка.
- Matroska — строгий формат `YYYY-MM-DD hh:mm:ss.mss` с усечением справа, UTC.
- ASF `WM/Year` — обычно год.

Вывод: `PartialDate` + сохранение исходной строки в `extra`, если распарсить не удалось. Не превращать всё в `datetime` — потеряется «только год».

### 5.4. Числа и пары

- Track/disc в ID3 — текст `N` или `N/T`.
- В MP4 — бинарная пара `trkn`/`disk` (number + total).
- В Vorbis — отдельные `TRACKNUMBER`/`TRACKTOTAL`.
- В Matroska — отдельные `PART_NUMBER`/`TOTAL_PARTS` (строки!).
- В ASF — `WM/TrackNumber`, причём исторически есть 0-based `WM/Track` и 1-based `WM/TrackNumber`; нужно проверять.

Вывод: модель хранит `number` и `total` раздельно; адаптер сам собирает `N/T` или пару.

### 5.5. Жанры

ID3 `TCON` может быть текстом (`Rock`), числовым индексом (`(17)`), списком. ID3v1 — один байт-индекс. MP4 — `©gen` (текст) или `gnre` (ID3-индекс). Vorbis/Matroska — текст. Вывод: нормализовать в список строк, но сохранять исходный индекс в `extra`, если он был.

### 5.6. Язык и описания

- ID3 `COMM`/`USLT`/`APIC` имеют language и description; нельзя терять при round-trip.
- Matroska `TagLanguage` (RFC 5646) и `TagDefault`.
- MP4 `data` может иметь language/country.
- Vorbis `LANGUAGE`.

Вывод: `Lyrics` и `Comment` — не просто `str`, а структуры с `lang`/`description`; иначе повторная запись испортит теги.

### 5.7. Неизвестные теги

Универсальный класс не должен стирать то, чего не понял. Варианты:
- хранить `extra: dict[str, list[str]]` и писать обратно;
- хранить «сырые» теги per tag system и не трогать их, если поле не менялось;
- при перезаписи файла сохранять все неизвестные фреймы/атомы/items.

Это отдельное требование к адаптеру: **read-modify-write**, а не «собрать новый тег с нуля».

### 5.8. Несколько теговых систем в одном файле

Приоритеты чтения нужно зафиксировать:

| Файл | Порядок чтения (предложение) |
|---|---|
| MP3 | ID3v2.4 → ID3v2.3 → APEv2 → ID3v1 |
| WAV | ID3 chunk → RIFF INFO → BWF |
| MP4 | `ilst` → `udta` → `mdta` |
| Matroska | наиболее специфичный `TargetTypeValue` (30) → 50 → 70 |
| DSF/DFF | ID3v2 (единственный) |

При записи: обновлять «канонический» набор и, по флагу, синхронизировать legacy (ID3v1/RIFF INFO). Иначе плееры будут показывать разные значения.

### 5.9. Стоимость записи

- ID3v2 в MP3 — можно дописать в padding, иначе перезапись файла.
- FLAC — padding-блок, иначе сдвиг аудио.
- MP4 — `moov` может быть в конце; перезапись/перенос `moov` дорогая, особенно для видео.
- Matroska — теги в конце/в кластерах; перезапись может быть дорогой.
- WAV/AVI — chunk sizes; вставка в середину требует сдвига.

Вывод: API записи должен быть асинхронным по смыслу (долгая операция), атомарным (temp + replace) и уметь «не переписывать, если не изменилось».

### 5.10. Обложка

| Формат | Где | Что хранит |
|---|---|---|
| MP3 | `APIC` | MIME, picture type, description, binary |
| FLAC | `PICTURE` block | type, MIME, description, width/height/depth/colors, binary |
| Ogg | `METADATA_BLOCK_PICTURE` | base64 от FLAC PICTURE |
| M4A/MP4 | `covr` | JPEG/PNG, без type/description |
| WMA/ASF | `WM/Picture` | type, MIME, description, binary |
| APEv2 | binary item `Cover Art (Front)` | filename + `\0` + binary |
| Matroska | attachment | filename, MIME, description, binary |
| WAV/AIFF | только ID3 chunk | `APIC` |

Вывод: `Artwork` — отдельная модель (`data`, `mime`, `kind`, `description`, `width`, `height`), отдельные `read_artwork`/`write_artwork`, потому что:
- обложек может быть несколько (front/back/artist/logo);
- в MP4/Matroska нет picture type;
- в Matroska это attachment, а не тег;
- большие обложки влияют на размер и скорость.

---

## 6. Что реально нужно сервисам

### 6.1. Navidrome

Navidrome — музыкальный сервер (Subsonic/OpenSubsonic API). Он читает аудио-файлы, метаданные — через TagLib, воспроизведение/транскодирование — через ffmpeg. Список MIME-типов в его репозитории: `.mp3`, `.ogg`, `.oga`, `.opus`, `.aac`, `.alac`, `.m4a`, `.m4b`, `.flac`, `.wav`, `.wma`, `.ape`, `.mpc`, `.shn`, `.aif`, `.aiff`, `.dsf`, `.wv`, `.tak`, `.mka`, `.webm`. В комментарии к файлу прямо сказано: «будет работать только если формат поддержан taglib и/или ffmpeg».

Navidrome импортирует **ограниченный набор полей** (см. `resources/mappings.yaml`): title, sort title, artist, artists, albumartist, album, track/total, disc/total, date, original date, release date, genre, mood, compilation, bpm, lyrics, comment, MusicBrainz IDs, ReplayGain, ISRC, label, language, copyright, grouping, movement, explicit status и др. Лишние теги игнорируются, если не объявлены в `Tags`.

Обложка: Navidrome ищет `cover.*`, `folder.*`, `front.*`, затем embedded, затем внешний сервис. То есть sidecar-обложки для него — first-class, и это важно для итогового проекта.

### 6.2. Jellyfin

Jellyfin для музыки читает embedded-метаданные; альбом определяется папкой, но теги имеют приоритет. Важные ограничения из документации:
- ID3v1 обрезает поля до 30 байт — нужен ID3v2.4;
- `.mp4` только с аудио не считается музыкой — переименовать в `.m4a`;
- `.mkv`/`.webm` только с аудио — переименовать в `.mka`;
- `.weba` не поддерживается;
- FLAC с embedded WebP или ID3-тегами может не играть в браузерах;
- обложки — external или embedded; external имеет приоритет.

Для видео Jellyfin обычно использует sidecar/NFO и онлайн-провайдеров, а не богатые embedded-теги. Это аргумент за то, что видео-модель должна уметь и embedded, и sidecar.

### 6.3. Plex и Emby

Plex для музыки читает embedded-теги и sidecar-обложки; для видео — агенты/NFO/sidecar. Публичная страница Plex про форматы говорит в основном о воспроизведении (MP4/AAC/AC3/EAC3, M4A и т.д.), а не о тегах. Практический вывод: для видео embedded-теги — «приятный бонус», основной канал — sidecar/NFO; для музыки — наоборот.

### 6.4. yt-dlp

`yt-dlp --embed-metadata` вызывает ffmpeg и передаёт ограниченный набор «common» полей (см. `FFmpegMetadataPP`):

- title ← track/title
- date ← upload_date
- description/synopsis ← description
- purl/comment ← webpage_url
- track ← track_number
- artist ← artist/artists/creator/creators/uploader/uploader_id
- composer ← composer/composers
- genre ← genre/genres/categories/tags
- album ← album/series
- album_artist ← album_artist/album_artists
- disc ← disc_number
- show ← series
- season_number, episode_id, episode_sort

Дополнительно:
- `--embed-thumbnail` для части форматов использует mutagen/AtomicParsley;
- `--parse-metadata FROM:TO` позволяет добавить поля;
- `--embed-info-json` возможен только для MKV/MKA (attachment);
- при `--embed-metadata` yt-dlp добавляет `-write_id3v1 1`, то есть для MP3 пишет и ID3v1.

Это важный ориентир: если наш класс читает то, что пишет yt-dlp, он должен понимать `title`, `artist`, `album`, `date`, `comment`, `genre`, `track`, `disc`, `description`, `synopsis`, `show`, `season_number`, `episode_id`.

### 6.5. MusicBrainz Picard

Picard поддерживает MP3/MP2, M4A/M4B/M4P/M4V/M4R/MP4, WMA/WMV/ASF, WAV, FLAC, AIFF/AIF/AIFC, Musepack, WavPack, Monkey's Audio, Ogg (Vorbis/FLAC/Theora/Opus), DFF/DSF. Он пишет MusicBrainz IDs, sort-поля, album artist, original date, release country/status/type, cover art.

Ключевой урок: Picard для WAV использует ID3v2, потому что «у WAV нет стандарта тегирования», а RIFF INFO пишет только для совместимости и с ограничениями. Это ровно тот случай, где универсальный класс должен уметь «основной + legacy» набор.

### 6.6. OpenSubsonic / Subsonic API

Navidrome отдаёт клиентам поля вроде `title`, `album`, `artist`, `track`, `discNumber`, `year`, `genre`, `coverArt`, `size`, `contentType`, `suffix`, `duration`, `bitRate`, `path`, `musicBrainzId`, `isrc`, `genres`, `artists`, `albumArtists`, `displayArtist`, `releaseDate`, `originalReleaseDate`, `sortName`, `discSubtitle`, `moods`, `replayGain`, `bpm`, `comment`, `lyrics`. Это де-факто «минимальный общий знаменатель» для музыкального сервера.

### 6.7. Коммерческие стриминги

Spotify/Apple Music/YouTube Music для дистрибуции принимают метаданные через DDEX/XML и партнёрские API, а не вычитывают ID3-теги из мастер-файла. Embedded-теги важны для локальных файлов, загрузок пользователя и self-hosted серверов, но не являются каналом доставки в коммерческий стриминг. Это стоит зафиксировать в scope, чтобы не строить универсальный класс под несуществующее требование.

### 6.8. Сводка требований

| Сервис | Основной сценарий | Форматы | Ключевые поля | Обложка |
|---|---|---|---|---|
| Navidrome | аудио-библиотека | mp3, flac, ogg/opus, m4a, wav, wma, ape, dsf, mka | title, artist, albumartist, album, track/disc, date, genre, lyrics, MBID, ReplayGain | embedded + sidecar |
| Jellyfin | аудио/видео | mp3, flac, m4a, ogg, wma, wav, mkv, mp4 | embedded music tags, NFO для видео | embedded + external |
| Plex/Emby | аудио/видео | mp4, mkv, avi, mov, wmv, mp3, flac, m4a | music tags, video sidecar | embedded + external |
| yt-dlp | скачивание | mp4, mkv, webm, mp3, m4a, opus, flac, wav | title, artist, album, date, track, show/season/episode | thumbnail embed |
| Picard | теггинг | mp3, flac, ogg, m4a, wav, aiff, wma, ape, wv, dsf | MBID, sort, albumartist, date, cover | embedded |
| OpenSubsonic | API | любые | title, album, artist, track, date, genre, coverArt, MBID, ISRC | coverArt id |

---

## 7. Рекомендуемая архитектура

### 7.1. Принципы

1. **Контейнер и теговая система — разные сущности.** `ContainerFormat` (MP4, Matroska, RIFF, Ogg, MPEG audio, ASF) и `TagSystem` (ID3v2, Vorbis, MP4 ilst, RIFF INFO, ASF, APEv2, Matroska Tags).
2. **Определять формат по magic bytes, а не по расширению.** `.m4a`/`.mp4`/`.m4b` — один контейнер; `.ogg` может быть Vorbis/Opus/FLAC/Theora; `.mkv`/`.webm` — один EBML-контейнер.
3. **Модель — нормализованная, адаптеры — формат-специфичные.** Никаких `if extension == ".mp3"` в бизнес-логике.
4. **Capability matrix на каждый адаптер.** Поле может быть native/generic/unsupported, с типом, кратностью, лимитом и кодировкой.
5. **Read-modify-write, а не «создать тег с нуля».** Неизвестные фреймы/атомы/items сохраняются.
6. **Запись атомарная и проверяемая.** Temp-файл → fsync → replace; после записи — re-read и сравнение.
7. **Ошибки — данные.** `WriteReport` вместо исключения на каждое неподдерживаемое поле.

### 7.2. Модель

```python
from dataclasses import dataclass, field
from enum import StrEnum

class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"

@dataclass(frozen=True)
class PartialDate:
    year: int
    month: int | None = None
    day: int | None = None

@dataclass(frozen=True)
class Artwork:
    data: bytes
    mime: str
    kind: str = "front"          # front/back/artist/logo/other
    description: str | None = None
    width: int | None = None
    height: int | None = None

@dataclass(frozen=True)
class Lyrics:
    text: str
    language: str | None = None
    synced: bool = False

@dataclass
class CommonMetadata:
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    date: PartialDate | None = None
    genre: list[str] = field(default_factory=list)
    comment: str | None = None
    composer: list[str] = field(default_factory=list)
    lyrics: Lyrics | None = None
    copyright: str | None = None
    language: str | None = None
    extra: dict[str, list[str]] = field(default_factory=dict)

@dataclass
class AudioMetadata(CommonMetadata):
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    compilation: bool | None = None
    lyricist: list[str] = field(default_factory=list)
    conductor: list[str] = field(default_factory=list)
    producer: list[str] = field(default_factory=list)
    label: str | None = None
    isrc: str | None = None
    bpm: int | None = None

@dataclass
class VideoMetadata(CommonMetadata):
    show: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_id: str | None = None
    network: str | None = None
    director: list[str] = field(default_factory=list)
    writer: list[str] = field(default_factory=list)
    synopsis: str | None = None
    rating: str | None = None
    keywords: list[str] = field(default_factory=list)
    location: str | None = None
```

Почему не один класс: видео-поля бессмысленны для трека, а альбомные — для фильма. Но оба класса наследуют `CommonMetadata`, а адаптеры работают с `CommonMetadata` и проверяют `isinstance`.

### 7.3. Адаптеры и capability

```python
from typing import Protocol

class MetadataBackend(Protocol):
    name: str
    containers: frozenset[str]
    def sniff(self, path: str) -> str | None: ...
    def read(self, path: str) -> CommonMetadata: ...
    def capabilities(self, container: str) -> "Capabilities": ...
    def write(self, path: str, meta: CommonMetadata,
              policy: "WritePolicy") -> "WriteReport": ...
    def read_artwork(self, path: str) -> list[Artwork]: ...
    def write_artwork(self, path: str, art: list[Artwork],
                      policy: "WritePolicy") -> "WriteReport": ...

@dataclass(frozen=True)
class FieldCapability:
    supported: bool
    multi: bool
    value_type: type
    max_bytes: int | None
    encoding: str
    native_key: str | None

@dataclass(frozen=True)
class Capabilities:
    tag_system: str
    fields: dict[str, FieldCapability]
    arbitrary_keys: bool
    artwork: bool
```

`WritePolicy`:

```python
class Unsupported(StrEnum):
    ERROR = "error"
    SKIP = "skip"
    COERCE = "coerce"

@dataclass(frozen=True)
class WritePolicy:
    on_unsupported: Unsupported = Unsupported.SKIP
    write_legacy: bool = False
    preserve_unknown: bool = True
    verify: bool = True
```

### 7.4. Конвейер чтения

```
path
  → sniff (magic bytes) → ContainerFormat + TagSystem
  → probe (ffprobe/parser) → MediaKind, codec, duration
  → backend.read_tags() → raw tags + unknown tags
  → normalize (encoding, dates, numbers, lists)
  → merge по приоритету теговых систем
  → CommonMetadata (+ warnings)
```

### 7.5. Конвейер записи

```
CommonMetadata
  → validate (типы, NUL, длины)
  → capabilities(container).plan(fields) → Plan(write/skip/coerce)
  → если policy=ERROR и есть unsupported → fail
  → read raw tags (read-modify-write)
  → patch только изменённых полей
  → atomic write (temp + fsync + replace)
  → re-read + compare → WriteReport
```

`WriteReport`:

```python
@dataclass
class WriteReport:
    backend: str
    container: str
    written: list[str]
    skipped: list[tuple[str, str]]   # field, reason
    coerced: list[tuple[str, str]]   # field, reason
    warnings: list[str]
    bytes_before: int
    bytes_after: int
```

### 7.6. Выбор библиотек

| Библиотека | Плюсы | Минусы | Роль |
|---|---|---|---|
| **mutagen** | чистый Python, ID3v1/v2, FLAC, Ogg, Opus, MP4, ASF, APEv2, AIFF, WAV, DSF/DFF | нет Matroska/AVI/FLV/TS | основной backend для аудио |
| **ffmpeg/ffprobe** | почти все контейнеры, видео, remux | ограниченный набор metadata-ключей, перезапись, subprocess | backend для MKV/AVI/TS и видео, fallback |
| **TagLib (pytaglib)** | C++, быстрый, широчайший список, включая Matroska/WebM | бинарная зависимость, Python-биндинги могут отставать | быстрый backend для сканирования библиотеки |
| **ExifTool** | читает/пишет почти всё, включая MKV/MP4/RIFF | Perl, медленный, внешний процесс | верификация и редкие форматы |
| **mediafile (beets)** | нормализованный API поверх mutagen | меньше форматов, своя модель | референс для маппинга полей |

Практичная стратегия: **mutagen — основа для аудио; ffmpeg — для видео и Matroska; TagLib — опциональный ускоритель; ExifTool — тест/fallback**. Не тащить все четыре в runtime с первого дня.

### 7.7. Тестирование

- **Fixtures:** генерировать файлы ffmpeg/mutagen в тестах, а не хранить бинарники в git (лицензии, размер).
- **Round-trip:** записать → прочитать → сравнить по каждому полю; отдельно — Unicode (эмодзи, RTL, диакритика), длинные строки, NUL, переводы строк.
- **Cross-reader:** после записи проверить ffprobe + mutagen/TagLib; для видео — ffprobe и MKVToolNix.
- **Raw dump:** golden-тесты на байтовые структуры (ID3 frame sizes, MP4 atoms, EBML).
- **Fuzz:** битые размеры ID3/MP4/EBML, отрицательные длины, гигантские теги.
- **Целостность:** хеш аудио/видео-потока до и после записи тегов не должен меняться (кроме случаев remux).
- **Capability:** для каждого формата — тест, что неподдерживаемое поле не теряется молча.

---

## 8. Приоритеты и план внедрения

### 8.1. Уровни

| Уровень | Форматы | Почему |
|---|---|---|
| **P0** | MP3, FLAC, M4A/MP4 audio, OGG/Opus, MP4/MOV video, MKV/WebM | 90% библиотек Navidrome/Jellyfin/yt-dlp |
| **P1** | WAV, AIFF, WMA/ASF, APE/WV/MPC, MKA, AVI | старые библиотеки, lossless-архивы, видео |
| **P2** | DSF/DFF, FLV, MPEG-TS/PS, OGV, TTA/TAK | нишевые и acquisition-форматы |

### 8.2. Этапы

1. **Модель + sniffing + capability.** Без записи. Тесты на определение контейнера по magic bytes.
2. **Чтение P0.** mutagen: MP3/FLAC/OGG/Opus/M4A; ffmpeg/ffprobe: MP4/MKV.
3. **Запись P0 + обложка.** Read-modify-write, `WriteReport`, атомарность.
4. **Видео P0.** MP4/MOV и MKV/WebM: title/show/season/episode/director/synopsis, chapters — отдельно.
5. **P1.** WAV/AIFF (ID3 + INFO), WMA, APE, AVI.
6. **P2 и fuzz.** DSF/DFF, FLV, TS, OGV; тесты на битые файлы.

---

## 9. Открытые вопросы

1. **ID3v2.4 или 2.3 по умолчанию?** 2.4 — современно и UTF-8; 2.3 — совместимее со старыми плеерами. Предложение: 2.4 по умолчанию, 2.3 — опцией.
2. **Писать ли ID3v1 всегда?** Нет: только по флагу, иначе получаем lossy-дубль и рассинхрон.
3. **Что делать с неподдерживаемым полем?** `SKIP` с warning по умолчанию; `ERROR` — для строгого режима.
4. **Сохранять ли неизвестные теги?** Да, иначе универсальный класс будет портить библиотеку.
5. **Обложка: одна front или полный набор?** Модель — список; UI/CLI может ограничивать.
6. **Sidecar-обложки/NFO поддерживать?** Для Navidrome/Jellyfin — да, но это отдельный слой, не теги.
7. **Видео-модель: show/season/episode обязательны?** Для сериалов — да; для фильмов — нет.
8. **Коммерческая дистрибуция (DDEX) в scope?** Если нет — не тащить её в модель.
9. **Нужен ли TagLib как optional dependency?** Если библиотека большая и важна скорость сканирования — да.
10. **Как обрабатывать несколько теговых систем?** Зафиксировать приоритет и синхронизацию legacy.

---

## 10. Источники

### Спецификации

- [ID3v2.4 — structure](https://mutagen-specs.readthedocs.io/en/latest/id3/id3v2.4.0-structure.html)
- [ID3v2.4 — frames](https://mutagen-specs.readthedocs.io/en/latest/id3/id3v2.4.0-frames.html)
- [ID3v2.3](https://mutagen-specs.readthedocs.io/en/latest/id3/id3v2.3.0.html)
- [ID3v1](https://id3.org/ID3v1)
- [Vorbis comment specification](https://www.xiph.org/vorbis/doc/v-comment.html)
- [FLAC format](https://xiph.org/flac/format.html)
- [Matroska tag specification (draft-ietf-cellar-tags-25)](https://www.ietf.org/archive/id/draft-ietf-cellar-tags-25.txt)
- [Matroska tagging overview](https://www.matroska.org/technical/tagging.html)
- [WebM container guidelines](https://www.webmproject.org/docs/container/)
- [Apple QuickTime user data atoms](https://developer.apple.com/documentation/quicktime-file-format/user_data_atoms)
- [Apple QuickTime metadata keys](https://developer.apple.com/documentation/quicktime-file-format/quicktime_metadata_keys)
- [APEv2 tag overview](https://en.wikipedia.org/wiki/APEv2_tag)

### Реализации и маппинги

- [TagLib property mapping](https://taglib.org/api/p_propertymapping.html)
- [TagLib supported formats](https://taglib.org/)
- [Mutagen documentation](https://mutagen.readthedocs.io/en/latest/)
- [Mutagen MP4 API](https://mutagen.readthedocs.io/en/latest/api/mp4.html)
- [Mutagen ASF API](https://mutagen.readthedocs.io/en/latest/api/asf.html)
- [FFmpeg formats documentation](https://ffmpeg.org/ffmpeg-formats.html)
- [ExifTool RIFF tags](https://exiftool.org/TagNames/RIFF.html)
- [ExifTool ASF tags](https://exiftool.org/TagNames/ASF.html)

### Сервисы и инструменты

- [Navidrome mappings.yaml](https://github.com/navidrome/navidrome/blob/master/resources/mappings.yaml)
- [Navidrome mime_types.yaml](https://github.com/navidrome/navidrome/blob/master/resources/mime_types.yaml)
- [Navidrome tagging guidelines](https://www.navidrome.org/docs/usage/library/tagging/)
- [Navidrome artwork resolution](https://www.navidrome.org/docs/usage/library/artwork/)
- [Navidrome overview](https://www.navidrome.org/docs/overview/)
- [Jellyfin music documentation](https://jellyfin.org/docs/general/server/media/music/)
- [Plex supported media formats](https://support.plex.tv/articles/204377253-what-media-formats-are-supported/)
- [MusicBrainz Picard file formats](https://picard-docs.musicbrainz.org/en/v2.13/faq/faq_file_formats.html)
- [yt-dlp README — modifying metadata](https://github.com/yt-dlp/yt-dlp#modifying-metadata)
- [yt-dlp FFmpegMetadataPP source](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/postprocessor/ffmpeg.py)

---

*Дата обращения ко всем источникам: 2026-11-08.*
