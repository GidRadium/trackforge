# Artifact 12: source covers and derivative naming

- **Статус:** completed
- **Дата:** 2026-10-24
- **Связано:** Plan 11 (URL ingest), Plan 17 (derivatives)

## 1. Обложка из источника

Добавлен `SourceCoverObserver` (`adapters/metadata/source_cover.py`):

- работает от `source.metadata.v1`, без MusicBrainz/AcoustID;
- для YouTube добавляет стандартные
  `i.ytimg.com/vi/<id>/maxresdefault.jpg`, `sddefault`, `hqdefault`,
  `mqdefault` и пробует их по порядку;
- для остальных источников (включая Yandex Music) использует thumbnails из
  `yt-dlp` JSON;
- скачивает первый валидный JPEG/PNG/WebP/GIF, кладёт байты в CAS, а в
  `cover.candidates.v1` сохраняет `artifact_ref` (SHA-256), размеры, MIME и score;
- использует proxy-маршрут источника (`[network.ytdlp]`), а не
  `[network.metadata]`: YouTube-обложка идёт через YouTube-прокси, Yandex —
  напрямую;
- `source_cover_enabled = false` (env `TRACKFORGE_SOURCE_COVER_ENABLED`) полностью
  отключает observer.

`RenderItem` теперь читает выбранный `artwork` из CAS и передаёт его в
renderer/tag-writer, поэтому обложка реально попадает в выходной файл. Старый
`CoverArtObserver` больше не добавляет URL-only source thumbnails: если
CAA/iTunes/Deezer не дали обложку, executor откатывается на
`SourceCoverObserver`.

## 2. Naming derivative-треков

Для YouTube-подобных названий:

```text
title:    Nightcore | Queen of Shadow
uploader: Purely's Metal Nightcore
description: Queen of Shadow by Delain from Dark Waters.
```

получается:

```text
artist:  Delain
album:   Dark Waters
title:   Queen of Shadow (Nightcore)
output:  Delain - Queen of Shadow (Nightcore).m4a
```

Что изменено:

- `strip_qualifier()` удаляет уже присутствующий маркер из title
  (`Nightcore | ...`, `(... Nightcore)`, `... - Nightcore`) перед нормализацией
  trailing qualifier;
- `SourceMetadataInterpreter` не использует uploader как main artist, если и
  title, и uploader выглядят derivative-каналами;
- из description извлекается простая атрибуция `"<title> by <artist> from <album>"`;
- `naming_values` уже умеет не дублировать `(Nightcore)` в
  `{artist} - {title} ({qualifier})`, поэтому итоговое имя содержит qualifier
  ровно один раз.

## 3. Ограничения

- Description-эвристика намеренно узкая (`Title by Artist from Album`); сложные
  описания остаются на LLM/MusicBrainz.
- Если source не даёт ни thumbnail, ни description-атрибуции, artist остаётся
  unknown — детектор не угадывает.
- Yandex Music может отдавать несколько артистов в одном поле; это не меняется
  этим артефактом и остаётся задачей review/LLM.
- Cover observer не скачивает обложки из произвольных ссылок в description;
  используются только thumbnails из `yt-dlp` и стандартные YouTube URL.
