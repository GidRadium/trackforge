# 12. Roadmap

> Статус: **черновик v0.3**. Этапы, критерии готовности, риски.
> Код ещё не пишется; roadmap фиксирует порядок, чтобы не строить всё сразу.

## 1. Принцип

Вертикальные срезы. Каждый этап заканчивается работающим end-to-end сценарием, а не набором модулей «на будущее».

```text
Phase 0: design + spikes
Phase 1: local file MVP
Phase 2: single URL (yt-dlp)
Phase 3: LLM/ranker + review/feedback
Phase 4: full analysis (ASR/Demucs/lyrics)
Phase 5: playlists/collections
Phase 6: GUI/HTTP + library management
```

## 2. Phase 0 — Design & spikes

### Цель

Снять технические неизвестные до кода. Предварительная оценка по интернет-бенчмаркам — в [`14-phase0-spikes-assessment.md`](14-phase0-spikes-assessment.md).

### Spikes

1. **yt-dlp**: JSON single/playlist, cookies, JS runtime (Deno), форматы, ошибки. Проверить на 5–10 своих ссылках.
2. **ffprobe/mutagen**: round-trip тегов для MP3/M4A/FLAC/OGG/Opus; обложка/lyrics; ID3v2.3/2.4.
3. **MusicBrainz/AcoustID**: rate limits, User-Agent, качество match, кэш.
4. **faster-whisper**: CPU benchmark (small/medium, int8), память, word timestamps.
5. **Demucs**: CPU benchmark, память, реальное время; сравнить PyTorch и ONNX/OpenVINO.
6. **Cross-encoder**: CPU/ONNX benchmark, размер, качество на паре примеров.
7. **LLM**: structured output, стоимость, fallback, prompt injection.
8. **SQLite/миграции**: схема, WAL, file lock.

### Выход

- Обновлённые docs/ADR.
- Benchmark-таблица (CPU/память/время).
- Решение по моделям/зависимостям.
- Список fixtures.

### Критерий готовности

- Понятно, какие библиотеки/модели входят в MVP.
- Нет блокирующих неизвестных по yt-dlp/ffmpeg/тегам.
- Оценка стоимости LLM/CPU.

## 3. Phase 1 — Local file MVP

### Цель

Локальный файл → корректные теги → managed library. Базовая версия работает offline; AcoustID/MB — opt-in.

### Scope

- `pyproject.toml`, `uv`, src layout.
- Домен: Item/Media/Claims/Decisions/Feedback (минимум).
- Порты/адаптеры: LocalFileSource, ffprobe, initial tags, mutagen writer.
- SQLite + CAS + artifact store.
- Политика `policy` для title/artist/album/date/genre/cover.
- CLI `import`, `resolve`, `render`, `review`, `explain`, `doctor`.
- Sidecar.
- Тесты: round-trip, policy, naming, pipeline.
- Опционально: AcoustID/MB (если есть ключ) — но не блокирует.

### Не входит

- URL/yt-dlp.
- ASR/Demucs.
- LLM/autopilot.
- Плейлисты.
- GUI.

### Критерий готовности

- MP3/M4A/FLAC/OGG/Opus: теги записываются и читаются обратно.
- Оригинал не изменён.
- Повторный run не пересчитывает уже сделанное.
- `explain` показывает rationale.
- `review` сохраняет correction/lock.
- `doctor` ловит отсутствие ffmpeg/прав.

## 4. Phase 2 — Single URL (yt-dlp)

### Цель

Одна ссылка → скачанный файл → теги → библиотека.

### Scope

- `YtDlpSource`/`YtDlpAcquirer` (subprocess).
- Candidate list/format selection.
- Cookies per site; JS runtime; error mapping.
- `SourceSnapshot`/`Intake`.
- `add`/`plan` CLI.
- Rate limits/retries.
- Один трек; без плейлистов.

### Не входит

- Плейлисты.
- ASR/Demucs.
- GUI.

### Критерий готовности

- YouTube/SoundCloud single track end-to-end.
- Ошибки login/rate limit/DRM понятны.
- Cookies не попадают в логи/артефакты.
- `doctor` проверяет yt-dlp/Deno/cookies.

## 5. Phase 3 — LLM/ranker + review/feedback

### Цель

Качество на шумных описаниях и управляемое доверие LLM.

### Scope

- `LLMProvider` (OpenAI-compatible), prompts, structured output.
- Режимы `policy`, `llm_assisted`, `llm_autopilot`.
- `ClaimRanker`: lexical + cross-encoder; 0.5B local fallback — опционально.
- Feedback: corrections, locks, export/stats.
- Review UX (CLI).
- Cost/token tracking; budget.
- Prompt versioning/evaluation.

### Не входит

- ASR/Demucs.
- Плейлисты.
- GUI.

### Критерий готовности

- LLM не может выдумать MBID/URL.
- Locks всегда побеждают.
- Correction сохраняется с контекстом.
- Autopilot падает в fallback при невалидном ответе.
- Feedback export пригоден для анализа.

## 6. Phase 4 — Full analysis

### Цель

Максимальное качество: текст, тайминги, вокал, derivative.

### Scope

- faster-whisper (small/medium).
- Demucs (в `full`; можно отключить).
- Lyrics merge/alignment (LRC/ELRC).
- Derivative detector refinement (tempo/pitch/vocal/metadata).
- Essentia optional (AGPL).
- ModelManager/profiles fast/balanced/full.
- CPU benchmarks/eval.

### Не входит

- Плейлисты.
- GUI.

### Критерий готовности

- `full` работает на целевом CPU без OOM.
- LRC/ELRC генерируются; merge с lyrics.
- Nightcore/sped up детектится с приемлемой точностью на наборе.
- Деградация без Demucs/ASR видима и не ломает output.

## 7. Phase 5 — Playlists/collections

### Цель

Плейлист → выбор → поштучная обработка → подпапка коллекции.

### Scope

- `Collection`/`CollectionItem`.
- `--flat-playlist` enumeration.
- Выбор треков/диапазона.
- Per-item queue, rate limits, pause/resume.
- Целевой размер раннего релиза — до 500 треков.
- `tag_basis`: album_first/playlist_first/ask.
- Collection subfolder.
- Partial failures.

### Не входит

- Многопользовательский режим.
- GUI.

### Критерий готовности

- Плейлист до 500 треков (тест 100+): pause/resume, без повторного скачивания.
- Один упавший трек не ломает остальные.
- Подпапка коллекции; sidecar.
- Ban avoidance: лимиты/паузы.

## 8. Phase 6 — GUI/HTTP + library management

### Цель

Удобный локальный интерфейс и управление библиотекой.

### Scope

- FastAPI + SSE.
- Web UI (Svelte/React) или PySide6 — решить.
- Review diff, progress, settings, feedback stats.
- Library scan/duplicates/missing.
- Backup/GC.
- Cross-platform packaging (позже).

### Критерий готовности

- Локальный UI делает те же сценарии, что CLI.
- Нет бизнес-логики в UI.
- Library scan находит расхождения.
- Backup/restore проверены.

## 9. Риски и mitigation

| Риск | Где | Mitigation |
|---|---|---|
| yt-dlp ломается/банит | Phase 2/5 | subprocess, обновления, cookies, лимиты, fallback |
| CPU слишком медленный | Phase 4 | профили, small-модели, opt-in, бенчмарки |
| LLM дорого/нестабильно | Phase 3 | бюджет, кэш, strict schema, ranker/0.5B fallback |
| Теги портятся | Phase 1 | CAS, copy, atomic, read-back, round-trip |
| Идентичность ambiguous | все | hypotheses, review, не угадывать |
| Scope creep | все | вертикальные срезы, non-goals |
| Лицензии | все | license boundary, audit |
| Feedback bias | Phase 3 | item-level, holdout, eval |

## 10. Non-goals (напоминание)

- Многопользовательский сервер/auth.
- Стриминг/рекомендации.
- DRM/платный обход.
- Мобильное приложение.
- Поддержка «любого сайта» в ядре.
- Идеальное автотегирование без review.
- Real-time/DJ-анализ.

## 11. Критерии «проект готов к коду»

- [ ] Видение/принципы согласованы.
- [ ] Домен/инварианты согласованы.
- [ ] Архитектура/порты согласованы.
- [ ] Pipeline/кэш/resume описаны.
- [ ] Storage/схемы описаны.
- [ ] Источники/анализ/резолв/метаданные описаны.
- [ ] Интерфейсы/конфиг/качество описаны.
- [ ] Лицензия и границы зависимостей зафиксированы.
- [ ] MVP scope и критерии готовности приняты.
- [ ] Spikes Phase 0 закрыты или имеют план.

## 12. Открытые вопросы

- Включать ли AcoustID/MB уже в Phase 1 или Phase 2?
- Включать ли lyrics/cover в MVP или после?
- LLM-провайдер: в коде нет default; для Phase 3 нужен `config.toml` с `base_url`/`model` и ключом в `.env`.
- Нужен ли TUI до GUI?
- Q32: Linux-only v1; cross-platform — после v1, отдельным этапом.
- Кто размечает evaluation-набор.
