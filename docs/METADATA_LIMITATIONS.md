# Ограничения metadata-слоя

Краткий список известных ограничений, которые сознательно оставлены за рамками
текущей реализации.

## Matroska native tags

Сейчас MKV/WebM/MKA пишутся через `ffmpeg -metadata`. Это даёт рабочие, но
«плоские» теги:

- `track` / `DISC` вместо target-aware `PART_NUMBER` / `TOTAL_PARTS`;
- `ALBUM_ARTIST` вместо `ARTIST` на уровне альбома (`TargetTypeValue=50`);
- `SHOW` / `SEASON_NUMBER` / `EPISODE_SORT` как обычные имена, без
  Matroska-иерархии.

Корректные target-level теги требуют `mkvpropedit`/`mkvextract` из
**MKVToolNix**. Это нативный C++-инструмент, а не Python-пакет:

- `uv add` его не установит;
- `pymkv` на PyPI — только обёртка, которой всё равно нужны бинарники
  MKVToolNix;
- установка: системный пакет `mkvtoolnix` (apt/dnf/pacman/brew) или
  установщик с официального сайта.

До установки MKVToolNix используется ffmpeg-вариант. После установки можно
добавить отдельный backend, который читает существующие теги через
`mkvextract tags`, сливает их с изменениями и пишет через
`mkvpropedit --tags all:...`.

## Обложки AVI/FLV

`ffmpeg` отказывается записывать attachment-потоки в AVI и FLV:

```text
Invalid or not supported codec type 'attachment'
```

Стандартного переносимого способа хранить обложку в этих контейнерах нет.
Рекомендуемый вариант — sidecar-файл (`cover.jpg`/`folder.jpg`) рядом с медиа
или внешний artwork в медиасервере.

## ID3v2.3 и дата

Режим `WritePolicy(id3_version=3)` пишет версию 3 и переводит текст в UTF-16,
но `mutagen` даже при `v2_version=3` нормализует дату в `TDRC`, а не в
`TYER`/`TDAT`. Заголовок, исполнитель, альбом и остальные текстовые поля
работают, но старые плееры, понимающие только `TYER`/`TDAT`, могут не увидеть
дату.

## MPEG-TS/PS

MPEG-TS, M2TS и MPEG-PS/VOB читаются, но не пишутся: у них нет стандартного
файлового набора тегов. Для библиотеки их лучше ремуксить в MKV/MP4.

## Внешние инструменты

`ffmpeg`/`ffprobe` — опциональные внешние зависимости:

- без `ffprobe` не работают probe потоков для MP4/ASF и чтение MKV/AVI/FLV/TS;
- без `ffmpeg` нельзя писать MKV/AVI/FLV и читать/писать обложки MKV.

Они не входят в Python-зависимости и не устанавливаются через `uv`.
