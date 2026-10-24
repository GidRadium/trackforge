# 11. Quality

> Статус: **черновик v0.3**. Тесты, evaluation, безопасность, лицензии и критерии готовности.
> Цель — чтобы «правильно» было проверяемо, а не только описано.

## 1. Принципы

1. **Контракты важнее покрытия.** Тестируем границы портов, схемы артефактов и инварианты.
2. **Детерминизм где возможно.** Сеть/LLM/модели — через кассеты/фейки; реальные вызовы — отдельно.
3. **Fixture-first.** Маленькие медиа-файлы, JSON-примеры, golden-артефакты.
4. **Ошибки — часть контракта.** Проверяем не только success path.
5. **Evaluation — не unit-тест.** Качество LLM/ranker/ASR измеряем на наборе, а не одним примером.
6. **Безопасность и лицензии — gate, а не «потом».**

## 2. Пирамида тестов

```text
        e2e (optional, network)
      integration (ffmpeg, sqlite)
    pipeline (fake adapters, DAG/cache)
  adapter/contract (cassettes, fixtures)
unit (domain, policies, naming, validators)
```

### 2.1. Unit

- Домен: claims/decisions/identity/feedback.
- Политики: стратегии, конфликты, locks, unknown.
- Нормализация: строки/артисты/даты/BPM/key.
- Нейминг: sanitization, collisions, Unicode, length.
- Валидаторы: поля/cross-field.
- Кэш-ключи: канонический JSON, релевантный конфиг.

### 2.2. Contract

Для каждого порта:

- фейк-реализация;
- тест, что адаптер соответствует протоколу;
- тест ошибок/таймаутов/пустых ответов;
- тест, что адаптер не протекает наружу.

### 2.3. Adapter

- `ffprobe` — фикстуры разных контейнеров.
- `mutagen` — round-trip для MP3/M4A/FLAC/OGG/Opus.
- `yt-dlp` — JSON-фикстуры (без live network).
- MusicBrainz/AcoustID/CAA/LRCLIB — HTTP-кассеты.
- LLM — fake provider + JSON-схемы; live — optional.
- SQLite — миграции/репозитории на temp DB.

### 2.4. Pipeline

- DAG: циклы, missing providers, fallback chains, budget.
- Cache: hit/miss, version invalidation, TTL, `--force`.
- Resume: падение между шагами, невалидный артефакт, повторный publish.
- Degradation: нет сети/модели/ffmpeg, невалидный LLM.
- Concurrency: лимиты, отмена, file lock.
- Idempotency: acquire/publish.

### 2.5. Integration

- Локальный файл → полный путь → output → read-back.
- Playlist (позже) с fake source.
- SQLite + filesystem + sidecar.
- `doctor` на чистой среде.

### 2.6. E2E

- Один реальный URL (YouTube/SoundCloud) — optional, network, не в CI.
- Проверка cookies/JS runtime/форматов.
- Помечать `@pytest.mark.network`.

## 3. Fixtures

### 3.1. Медиа

Маленькие файлы (1–5 секунд), сгенерированные ffmpeg:

- sine 440 Hz;
- silence;
- stereo/mono;
- разные sample rates;
- с тегами/без;
- с обложкой/без;
- MP3/M4A/FLAC/OGG/Opus.

Хранить в `tests/fixtures/media/` (или генерировать в fixture-фабрике, чтобы не тащить бинарники).

### 3.2. JSON

- `yt-dlp` info.json для single/playlist.
- MusicBrainz/AcoustID/CAA/LRCLIB responses.
- claims/decisions/artifacts.
- sidecar.

### 3.3. HTTP-кассеты

- `respx` для httpx.
- Кассеты не должны содержать cookies/токены.
- Записывать вручную/через отдельный скрипт, не в CI.

### 3.4. Golden-файлы

- `claims.v1.json`, `decisions.v1.json`, sidecar.
- Стабильная сериализация (sorted keys).
- Обновление — осознанно, с diff.

## 4. Evaluation

### 4.1. Набор

- 20–50 треков: local/YouTube/SoundCloud, разные жанры/языки.
- Ground truth: title, artist, album, date, genre, cover, lyrics, kind, derivative.
- Пары original/derivative (nightcore/sped up/slowed/remix/cover).
- Ошибочные теги/шумные описания.
- Holdout для feedback (не обучаемся на нём втихую).

### 4.2. Метрики

- field accuracy / correction rate;
- derivative precision/recall;
- cover selection accuracy;
- ASR WER (где есть эталон);
- lyrics timing accuracy (offset/line);
- calibration (confidence vs correctness);
- latency/CPU/memory/cost;
- degraded/unknown rate;
- review rate.

### 4.3. Прогон

- `trackforge eval --dataset ...` (позже) — воспроизводимый.
- Фиксировать версии моделей/промптов/политик.
- Сравнивать с baseline.
- Не менять промпт/политику без прогона.

## 5. CI

### 5.1. Обязательно

- `ruff check` + `ruff format --check`.
- `mypy`/`pyright` (strict-ish).
- `import-linter` — слои и license boundary.
- `pytest` unit/contract (без сети).
- `pytest` integration (ffmpeg/sqlite) — если окружение готово.
- Проверка схем артефактов.

### 5.2. Отдельно

- Network/live — manual/nightly.
- Slow (ASR/Demucs) — manual.
- Evaluation — manual/periodic.

### 5.3. Матрица

- Python 3.12/3.13.
- Linux primary; Windows/macOS — позже.
- ffmpeg версия — фиксировать в CI.

## 6. Безопасность

Чеклист:

- [ ] Секреты не в git/логах/артефактах.
- [ ] Cookies `0600`, config `0700`.
- [ ] SSRF guard (private IP/redirects).
- [ ] Path traversal/symlink escape.
- [ ] Плагины — trusted, документировано.
- [ ] Нет DRM/обхода платного доступа.
- [ ] Rate limits/ToS.
- [ ] Feedback не уходит в облако без opt-in.
- [ ] SQL — параметризованный.
- [ ] Ошибки не содержат токенов/путей с секретами.

## 7. Лицензии

### 7.1. Проект

- GPL-3.0-or-later (`LICENSE`).
- Copyright (C) 2026 GidRadium.
- Source files — SPDX headers (`GPL-3.0-or-later`) позже.

### 7.2. Зависимости

| Зависимость | Лицензия | Граница |
|---|---|---|
| mutagen | GPL-2.0-or-later | tagging adapter |
| essentia | AGPL-3.0-only | optional plugin, warning |
| ffmpeg | LGPL/GPL (build) | external binary |
| Chromaprint/fpcalc | LGPL | external binary |
| yt-dlp | Unlicense | subprocess |
| librosa | ISC | core optional? |
| faster-whisper | MIT | asr extra |
| demucs | MIT | stems extra |
| cross-encoder models | Apache/MIT (выбрать) | ranker extra |
| Whisper weights | MIT (проверить) | asr extra |

### 7.3. Правила

- License boundary: ядро не импортирует GPL/AGPL.
- Optional extras — явные, с предупреждением.
- Dependency license audit в CI (позже).
- Модели — отдельные лицензии; хранить в `model_registry`.
- Attribution для внешних данных/обложек — в sidecar/README.

## 8. Документация

- Документы — контракты; изменения через PR/diff.
- ADR на существенные решения.
- Glossary — единый язык.
- Changelog — при релизах.
- Примеры — рабочие, не псевдокод.
- Схемы — Mermaid.

## 9. Release checklist

- [ ] `doctor` проходит на чистой среде.
- [ ] Миграции БД применяются.
- [ ] Unit/contract/integration зелёные.
- [ ] License audit.
- [ ] Changelog/version.
- [ ] Smoke: локальный файл → output → read-back.
- [ ] Smoke: URL (если доступен).
- [ ] Проверка, что секреты не попали в артефакты.
- [ ] Проверка sidecar/feedback.
- [ ] Тег/пакет.

## 10. Открытые вопросы

- Кто и как размечает evaluation-набор.
- Нужен ли CI с live network (скорее нет).
- Какой инструмент license audit.
- Нужен ли coverage gate (скорее нет; важнее контракты).
- Как хранить медиа-фикстуры (git LFS vs генерация).
- Политика обновления моделей/промптов.
