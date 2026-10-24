# ADR-0005: GPL-3.0-or-later + license boundary

- **Статус:** accepted
- **Дата:** 2026-10-22

## Контекст

Проект начинался как MIT, но ключевые инструменты имеют GPL/AGPL:

- `mutagen` — GPL-2.0-or-later;
- `essentia` — AGPL-3.0-only;
- `pytaglib`/`eyeD3` — GPL;
- `ffmpeg`/Chromaprint — LGPL/GPL в зависимости от сборки.

Проект личный; при этом хочется сохранить возможность будущей смены лицензии и переиспользования.

## Решение

- `LICENSE` — **GPL-3.0-or-later**.
- **License boundary:** ядро (`domain`, `application`, `pipeline`) не импортирует GPL/AGPL-библиотеки.
- GPL/AGPL-зависимости живут в адаптерах/optional extras:
  - `mutagen` — tagging adapter;
  - `essentia` — отдельный optional plugin с AGPL-предупреждением.
- `ffmpeg`/`fpcalc` — внешние бинарники, не линкуются.
- Если изоляция станет слишком дорогой — принимаем GPL как есть.

## Альтернативы

1. MIT + GPL extras — сложнее с распространением, но гибче.
2. MIT + ffmpeg/TagLib — меньше покрытие тегов, больше работы.
3. GPL без boundary — проще, но хуже для будущей смены лицензии.

## Последствия

- Нужен dependency/license audit.
- Optional extras и предупреждения.
- Ядро можно перелицензировать, заменив адаптеры (при сохранении авторства).
- Документация должна явно говорить о лицензиях.
