# 13. Consistency review

> Дата: 2026-10-22. Проверка документации v0.3 на противоречия, gaps и устаревшие решения.
> Код не писался; изменения только в проектных документах.

## 1. Что проверялось

- Продукт, MVP, платформы, масштаб, лицензия.
- Pipeline: стадии, use cases, состояния, кэш, resume, деградация.
- Домен ↔ storage: сущности, claims, decisions, locks, feedback, renders.
- Metadata/sources/analysis/interfaces/config.
- Терминология, ссылки, устаревшие open questions.

## 2. Итог

- Найдено **12 расхождений/пробелов**.
- **11 исправлено**, **1 принято как осознанное упрощение v1** (identity-сущности пока artifact-only).
- Битых внутренних ссылок: **0**.
- Устаревших `MIT`/`целевая GPL`: **0** (кроме исторических пояснений).
- Код не писался.

## 3. Находки

| # | Severity | Что было | Где | Решение |
|---|---|---|---|---|
| 1 | High | `Probe` был отдельной стадией в pipeline, но отсутствовал в vision/domain/architecture и в состояниях Item | `03-pipeline`, `00-vision`, `01-domain`, `02-architecture` | Probe объявлен **первым observer внутри Observe**; top-level стадии снова совпадают: Plan → Acquire → Observe → Interpret → Resolve → Render → Publish |
| 2 | High | Claims объявлены append-only, но в storage у `claims` было мутабельное `status` без истории | `01-domain`, `04-storage` | `claims.status` — текущая проекция; добавлена `claim_events` (accepted/rejected/superseded) |
| 3 | High | Locks описаны как отдельная сущность, но в SQLite не было таблицы; scope смешивал locks и правила | `01-domain`, `04-storage`, `07-resolution` | Добавлена `locks` (item/field); широкие scope (`source/artist/profile/global`) отнесены к future `PreferenceRule` |
| 4 | High | `Render` был доменной сущностью, но не имел таблицы; `output_bundles.render_id` висел без FK | `01-domain`, `04-storage` | Добавлена `renders`; `output_bundles.render_id` → FK |
| 5 | High | `library_dir` по умолчанию конфликтовал: `data_dir/library` vs `~/Music/TrackForge` | `00-vision`, `04-storage`, `10-operations` | Единый default: `~/Music/TrackForge`; `data_dir` — только внутренние CAS/артефакты/каталог |
| 6 | Medium | `full` включал Demucs, но open question предлагал «opt-in even in full» | `06-analysis`, `12-roadmap` | `full` включает Demucs; его можно отключить `--disable stems` |
| 7 | Medium | `Correction.scope` содержал `field`, которого не было в resolution/interface; смысл пересекался с `item` | `01-domain`, `07-resolution`, `09-interfaces` | `field` убран; `item` = этот item/поле; широкие scope = `PreferenceRule` |
| 8 | High | Доменные `Recording/Release/Artist/Work/Derivative/IdentityHypothesis` не имели storage-представления | `01-domain`, `04-storage` | В v1 — **artifact-only** (`identity.hypotheses.v1` + claims); нормализованные таблицы позже. Добавлен `items.identity_artifact_id` |
| 9 | Medium | Артефакт хранил `params_hash`, но не сами params; provenance/отладка страдали | `03-pipeline`, `04-storage` | Добавлены `params` в manifest и `params_json` в `artifacts` |
| 10 | Medium | `Collection(kind=album)` и `Release` смешивались | `01-domain` | Явно: Collection — входной контейнер; Release — идентифицированный релиз; они могут ссылаться, но не заменяют друг друга |
| 11 | Low | Пример pipeline публиковал в `library/<artist>/<album>`, хотя layout — flat + collection subfolder | `03-pipeline` | Пример приведён к `library_dir/...` и текущему layout |
| 12 | Low | Q8/Q10 оставались «открытыми», хотя Q27/Q32 уже ответили | `open-questions` | Помечены как решённые, остались только реальные остатки (allowlist, язык, плейлисты и т.п.) |

## 4. Принятое упрощение v1

**Identity-сущности не нормализуются в таблицы.** Это не забытый gap, а осознанное решение:

- MVP — один трек; join'ы по recording/release пока не нужны;
- identity живёт в `identity.hypotheses.v1` и claims;
- нормализованные таблицы (`recordings`, `releases`, `artists`, ...) добавляются, когда появятся duplicate/library UX и запросы по ним;
- публичный API/формат артефактов от этого не меняются.

## 5. Что осталось открытым (не противоречия)

- Q11 — язык docs/UI.
- Q13 — `tag_basis` для плейлистов.
- Q14 — duplicate/library reconciliation.
- Q15 — стабильность публичного API.
- Q16 — sandbox для плагинов.
- Q17 — финальные модели после бенчмарков (но ID уже конфигурируемы).
- Q18 — детали теговых полей.
- ReplayGain, allowlist доменов, точный формат `--json`.

## 6. Проверки после правок

- Внутренние markdown-ссылки: 0 битых.
- `LICENSE` — GPL-3.0-or-later; root README содержит стандартное GPL-уведомление.
- Нет `items/<content_hash>` / `library/<artist>/<album>` как default.
- Нет `Probe` как top-level stage.
- Нет `LRC embed` как default.
- Нет `MIT` как текущей лицензии.

## 7. Следующий шаг

Документы можно считать согласованными на уровне v0.3. Дальше — Phase 0 spikes:

1. yt-dlp/Deno/cookies на 2–3 сайтах.
2. Round-trip тегов MP3/M4A/FLAC/OGG/Opus.
3. CPU-бенчмарки Whisper/Demucs/cross-encoder.
4. Проверка MusicBrainz/AcoustID rate limits.
5. Фиксация MVP scope Phase 1.
