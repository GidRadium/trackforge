# Plan 16: ASR + lyrics

- **Статус:** conditional
- **Тип:** Post-MVP
- **Зависит от:** Plan 12, Plan 13 (go), Plan 14
- **Разблокирует:** — (vocal path для Plan 17)
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за транскрипцию и синхронизированный текст. Работаешь в `trackforge` после metadata/LLM. Задача — faster-whisper на CPU, `asr.transcript.v1`, LRC/ELRC sidecar и merge с lyrics-провайдерами; без Demucs.

## Цель

Для профиля `full` (или `--enable asr`) работает ASR: аудио → `asr.transcript.v1` с сегментами/словами/языком; `lyrics.merge` выбирает/сливает LRC, plain lyrics и ASR; рядом с output появляется `.lrc` (и опционально `.elrc`), LRC не встраивается в тег. Без модели/CPU-бюджета шаг видимо `skipped/degraded`.

## Контекст и инварианты

- faster-whisper (CTranslate2), CPU int8; default `small`, `medium` — опция; `large` не планируется (`13-spike-cpu-models.md`).
- Модель загружается через `ModelManager`, не внутри функции; max 1 тяжёлая модель.
- LRC/ELRC — только sidecar; plain lyrics — в тег, если формат поддерживает (Q30).
- ASR на музыке шумный; не «улучшать» текст LLM без доказательств; можно править пунктуацию/регистр.
- Nightcore: original lyrics + масштабированные тайминги; ASR-текст не заменяет найденный original.
- Язык: ISO 639; не угадывать по названию; language detection — evidence.
- Модели/веса — отдельные лицензии; checksum в `model_registry`; `--offline` запрещает скачивание.
- `full` включает ASR/Demucs; `--disable asr` отключает; `skipped` с причиной.

## Прочитать перед началом

- `docs/design/06-analysis.md` — §6, §11.2, §12–13.
- `docs/design/07-resolution.md` — §6.9, §7.1.
- `docs/design/08-metadata.md` — §5.
- `docs/design/03-pipeline.md` — §7.
- `docs/design/14-phase0-spikes-assessment.md` — §3.4, §7.
- `docs/plan/13-spike-cpu-models.md`, `docs/plan/12-metadata-providers.md`, `docs/plan/14-llm-ranker.md`.
- `docs/plan/artifacts/13-spike-cpu-models.md` (go/no-go).

## Предусловия

```bash
test -f docs/plan/artifacts/13-spike-cpu-models.md
rg -n "Whisper|Go|No-go" docs/plan/artifacts/13-spike-cpu-models.md
uv sync --extra tagging --extra asr
uv run ruff check .
uv run mypy src
uv run pytest tests/integration/test_metadata_resolve.py -q
uv run lint-imports
```

Plan 13 должен дать go для Whisper small (или medium). Если no-go — Plan 16 `deferred`, релиз без ASR. Модель может быть скачана вручную; `--offline` не должен ломать базовый install.

## Scope

- `adapters/analysis/whisper.py`: lazy load, int8, VAD, word timestamps, language.
- `application/use_cases/lyrics_merge.py`: LRC/plain/ASR merge.
- `application/services/lrc.py`: parse/validate/write LRC; ELRC writer.
- `application/dto/lyrics.py`.
- `application/use_cases/resolve_item.py` — lyrics claims.
- `application/use_cases/render_item.py` — копировать `.lrc`/`.elrc` в bundle.
- `[asr]` extra: `faster-whisper` (MIT); `ModelManager` download/checksum/license.
- CLI `models download`, `--enable asr`/`--disable asr`.
- Tests fake ASR + slow integration.

## Non-goals

- Demucs/vocal separation — Plan 17.
- LRC embed/SYLT — Q30.
- LLM-переписывание текста; только merge/пунктуация.
- Whisper large, GPU, real-time.
- Автоматический перевод lyrics.
- Evaluation harness — Deferred (quality backlog после v1.0).

## Deliverables

```text
src/trackforge/adapters/analysis/whisper.py
src/trackforge/application/use_cases/lyrics_merge.py
src/trackforge/application/services/lrc.py
src/trackforge/application/dto/lyrics.py
src/trackforge/adapters/models/local.py            # download/checksum
src/trackforge/interfaces/cli/commands/models.py   # download
tests/adapters/analysis/test_lrc.py
tests/application/test_lyrics_merge.py
tests/integration/test_asr_slow.py                 # marker slow
docs/plan/artifacts/16-asr-benchmark.md            # фактические замеры
```

## Контракты

### ASR

```text
asr.transcript.v1:
  language: str | null
  segments[]:
    start: float
    end: float
    text: str
    words[]: {word, start, end, probability}
  avg_logprob: float | null
  no_speech_prob: float | null
  model: {id, version, compute_type, threads}
  duration_seconds: float
```

- `WhisperObserver` capability: requires media; provides `asr.transcript.v1`; cost high; deterministic/seeded? GPU/CPU может быть недетерминирован; `cacheable=true` при фиксированной модели/версии.
- Config `[models.asr]`: `backend`, `name`, `compute_type`, `threads`, `vad_filter`, `beam_size`; без hardcode ID.
- `ModelManager.get("asr", id)`: lazy load, max 1 heavy, unload LRU; `--offline` не качает; checksum/license в registry.
- Missing model: `model_missing` warning, step `skipped`; не OOM.
- Threads: 8–12; не 18 (memory bandwidth).
- Output text нормализуется, но не переписывается; language — ISO.

### LRC / ELRC

```text
LrcDocument:
  lines[]: {timestamp_ms, text}
  metadata: {title?, artist?, album?, by?, offset_ms?}
  parse(text) -> LrcDocument
  render(doc) -> str
```

- LRC timestamp `[mm:ss.xx]`; минуты 2+ цифры; миллисекунды 2–3; monotonic timestamps; пустые строки допустимы.
- ELRC — word timestamps; формат согласован в `08-metadata.md`; sidecar `<basename>.elrc`.
- Invalid LRC → warning `invalid_lrc`, candidate не используется как authoritative.
- Plain lyrics — без таймкодов; max 256 KB; UTF-8.

### Merge

`LyricsMerger.merge(lyrics_candidates, asr_transcript | None, derivative_claims, duration) -> LyricsResult`:

1. Валидный LRC с хорошим совпадением title/artist/duration → основной текст/тайминг.
2. Plain lyrics + ASR → alignment/пропорциональные тайминги; пометить низкую точность.
3. Нет lyrics → ASR text + LRC по сегментам.
4. ASR плохой, lyrics хорошие → текст lyrics, тайминги ASR; warning.
5. Nightcore → original lyrics + scale timings by `speed_factor` (из derivative claims); если factor нет — warning, не масштабировать наугад.
- Не «улучшать» смысл LLM; допускается пунктуация/регистр.
- `LyricsResult`: `plain`, `lrc`, `elrc?`, `source`, `confidence`, `warnings`, `language`.
- Resolve: lyrics priority LRC > plain+ASR > ASR > existing; для nightcore — original lyrics; `synced_lyrics` sidecar.

### Render / CLI

- `RenderItem` пишет `track.<ext>.lrc`/`.elrc` рядом с audio (same basename); plain — в тег.
- `trackforge models download asr` — явная команда, checksum/license; `--offline` запрещает.
- `--enable asr`/`--disable asr`; `full` включает; `--auto` не спрашивает.
- `doctor` проверяет модель/память; не скачивает.

## Порядок работ

1. Реализуй LRC parser/writer + тесты.
2. Реализуй fake ASR и merge cases.
3. Реализуй WhisperObserver/ModelManager download.
4. Расширь resolve/render для synced lyrics.
5. Добавь CLI `models download`/flags.
6. Добавь slow integration на 1 трек; artifact с замерами.
7. Прогони quality gates; закоммить.

## Тесты

- **unit:** LRC parse/render/round-trip; invalid timing; ELRC; merge 5 сценариев; nightcore scale; language; plain max size.
- **contract:** `asr.transcript.v1` schema; `WhisperObserver` fake; LRC sidecar naming; render copies sidecar.
- **integration (slow, marker):** 1 трек 3–4 мин; RTF/RAM; transcript segments; LRC file; no network if model local.
- **fixtures:** fake transcript JSON, LRC/plain, derivative claims; no large binaries.
- **negative:** missing model; OOM simulated; invalid LRC; empty transcript; nightcore without speed factor; lyrics >256 KB.

## Проверка

```bash
uv sync --extra tagging --extra asr
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/analysis/test_lrc.py tests/application/test_lyrics_merge.py -q
uv run pytest -m slow tests/integration/test_asr_slow.py -q
uv run lint-imports
uv run trackforge models list --json
```

Ожидается: unit/merge зелёные; slow-тест проходит на целевом CPU или помечен skip с причиной; `.lrc` sidecar создаётся; LRC не в теге.

## Acceptance criteria

- [ ] `uv run pytest tests/adapters/analysis/test_lrc.py tests/application/test_lyrics_merge.py -q` зелёный.
- [ ] `asr.transcript.v1` валиден; language/segments/words присутствуют.
- [ ] LRC/ELRC sidecar создаётся рядом с output; LRC не встроен в plain lyrics.
- [ ] Merge покрывает 5 сценариев; nightcore использует original lyrics + scaling.
- [ ] Модель задаётся в `config.toml`, не в коде; `--offline` не скачивает.
- [ ] Без модели/памяти шаг `skipped/degraded` с warning, output не ломается.
- [ ] Slow-тест даёт RTF/RAM в artifact и не входит в CI.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.

## Handoff

Следующие планы могут считать готовыми:

- `WhisperObserver`/`asr.transcript.v1`.
- LRC/ELRC parser/writer/merge.
- `synced_lyrics` sidecar в render.
- `models download`/offline checks.
- Slow benchmark artifact.

## Stop conditions / decision gates

- Plan 13 no-go для Whisper — стоп; Plan 16 `deferred`, релиз без ASR.
- Модель не влезает в RAM — `skipped`, не OOM; зафиксируй.
- Нужен Demucs для качества — Plan 17; здесь микс.
- Нужно встроить LRC — стоп, Q30.
- Нужен LLM-перевод/переписывание — Deferred, не в scope.
- Нет лицензии/checksum модели — не используй; `model_missing`.
