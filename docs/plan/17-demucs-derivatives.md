# Plan 17: Demucs + derivatives

- **Статус:** conditional
- **Тип:** Post-MVP
- **Зависит от:** Plan 13 (go), Plan 14; Plan 16 — optional для vocal/lyrics path
- **Разблокирует:** —
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за стемы и derivative-анализ. Работаешь в `trackforge` после ASR. Задача — Demucs как opt-in observer, vocal/spectral features и уточнение derivative-детектора; без финальных решений и без Essentia.

## Цель

Для `full`/`--enable stems` работает Demucs: аудио → `audio.stems.v1` (vocal/instrumental ratios, drums/bass/other, optional features), без сохранения стемов по умолчанию. Derivative-детектор использует metadata + tempo/duration + vocal/spectral evidence и создаёт claims/hypotheses для nightcore/sped up/slowed/remix/cover/instrumental/acapella; при нехватке evidence — `ambiguous`, не угадывание.

## Контекст и инварианты

- Demucs PyTorch `htdemucs` на CPU ≈1.5× длительности, 3–7 ГБ; `-j 1`; `htdemucs_ft` не для bulk (`14-phase0-spikes-assessment.md` §3.5).
- ONNX/OpenVINO — только если Plan 13 показал ускорение и качество; иначе PyTorch opt-in.
- Стемы можно не сохранять; по умолчанию только признаки (`06-analysis.md` §7).
- `full` включает Demucs; `--disable stems` отключает; `skipped` с причиной.
- Essentia (AGPL) — **Deferred**, отдельный plugin после v1.0.
- Derivative — связь + transformations, не enum; `ContentKind` — отдельная ось.
- Не утверждать derivative без evidence; при конфликте — `ambiguous`.
- `is_official` — отдельное поле; MB relationship — evidence, не истина.
- Модели — отдельные лицензии/checksum; `--offline` запрещает скачивание.

## Прочитать перед началом

- `docs/design/06-analysis.md` — §7–8, §12–13.
- `docs/design/01-domain-model.md` — §5.4–5.5, §12.
- `docs/design/07-resolution.md` — §7.
- `docs/design/14-phase0-spikes-assessment.md` — §3.5–3.6, §4, §7.
- `docs/plan/artifacts/13-spike-cpu-models.md` (Demucs go/no-go).
- `docs/plan/16-asr-lyrics.md` (handoff).

## Предусловия

```bash
test -f docs/plan/artifacts/13-spike-cpu-models.md
rg -n "Demucs|Go|No-go" docs/plan/artifacts/13-spike-cpu-models.md
uv sync --extra tagging --extra asr --extra stems
uv run ruff check .
uv run mypy src
uv run pytest tests/application/test_llm_modes.py -q
uv run lint-imports
```

Plan 13 должен дать go для Demucs (PyTorch или ONNX). Если no-go — Plan 17 `deferred`, релиз без Demucs; derivative остаётся metadata/tempo-only. `torch`/`demucs` — extra, не базовый install.

## Scope

- `adapters/analysis/demucs.py`: lazy load, `htdemucs`, `-j 1`, segment/overlap, признаки.
- `[stems]` extra: `demucs`, `torch`; лицензии MIT/BSD; проверка.
- `application/dto/stems.py`.
- `application/use_cases/interpret_item.py` — derivative refinement.
- `domain/policies/derivative.py` — правила/пороги/параметры.
- `audio.stems.v1` schema.
- `derivative.hypotheses.v1`/claims (если ещё нет).
- CLI `--enable stems`/`--disable stems`; `models download demucs`.
- Tests fake + slow.

## Non-goals

- Сохранение стемов как output по умолчанию.
- Essentia/AGPL.
- GPU, real-time, batch playlists.
- Обучение derivative-классификатора.
- Точная pitch-оценка без модели; параметры — оценки с confidence.
- Автоматическое удаление/слияние derivative.

## Deliverables

```text
src/trackforge/adapters/analysis/demucs.py
src/trackforge/application/dto/stems.py
src/trackforge/domain/policies/derivative.py
src/trackforge/application/use_cases/interpret_item.py   # расширение
src/trackforge/application/use_cases/resolve_item.py     # derivative claims/evidence
tests/adapters/analysis/test_demucs.py
tests/domain/policies/test_derivative.py
tests/application/test_derivative_claims.py
tests/integration/test_demucs_slow.py                    # marker slow
```

## Контракты

### audio.stems.v1

```text
audio.stems.v1:
  model: {id, version, backend, segment, overlap, jobs}
  duration_seconds
  ratios:
    vocals: float        # 0..1 energy ratio
    drums: float
    bass: float
    other: float
    instrumental: float
  features:
    vocal_energy: float | null
    spectral_centroid: float | null
    rms: float | null
  warnings[]
```

- Стемы не сохраняются; если нужны для debug — `--debug`/temp, с TTL; не в library.
- Demucs capability: requires media; provides `audio.stems.v1`; cost high; cacheable; `-j 1`.
- Missing model/память → `skipped/degraded`; output не ломается.
- Модель/версия в payload; checksum/license в registry.

### Derivative detector

```text
DerivativeEvidence:
  source: metadata | tempo | duration | vocal | spectral | musicbrainz | llm
  value: str | float
  confidence: float
  artifact_ref?

DerivativeHypothesis:
  transformations[]  # nightcore, sped_up, slowed, reverb, 8d, remix,
                     # bootleg, mashup, cover, instrumental, acapella,
                     # karaoke, radio_edit, extended, live, acoustic, demo, other
  parameters: {speed_factor?, pitch_semitones?, bpm_ratio?, reverb_amount?}
  derivative_title?, derivative_artist?, is_official?
  score, status: proposed | accepted | rejected | ambiguous
  evidence[]
```

- Rules:
  - title/description/tags: `nightcore`, `sped up`, `slowed + reverb`, `8D`, `remix`, `cover`, `instrumental`, `acapella`;
  - duration ratio vs MB original (если есть);
  - BPM ratio ~1.2–1.35 → sped up/nightcore; ~0.8–0.95 → slowed;
  - vocal energy low → instrumental; high + no drums → acapella;
  - spectral centroid brighter/darker;
  - MB relationships remix/cover — official evidence;
  - LLM claims — evidence, не истина.
- Несколько transformations допустимы (`nightcore` + `sped_up`).
- `is_official`: только при MB relationship/официальном источнике; иначе `null`/`false` с evidence.
- При конфликте → `ambiguous`; не выбирать «наугад».
- Детектор создаёт claims/hypotheses; resolver (Plan 07) выбирает.

### Profile / CLI

- `full` включает `asr` + `stems`; `--disable stems`/`--enable stems`.
- `--offline` запрещает download; локальная модель работает.
- `trackforge models download demucs` — explicit, checksum/license.
- `doctor` проверяет наличие модели/памяти; не скачивает.
- Output tags: derivative qualifier/genre/comment — через существующую policy; не дублировать qualifier.

## Порядок работ

1. Реализуй `audio.stems.v1` DTO и fake Demucs.
2. Реализуй `DemucsObserver`/ModelManager integration.
3. Реализуй derivative rules и evidence/hypotheses.
4. Расширь resolve claims/derivative policy.
5. Добавь CLI flags/models download.
6. Добавь slow-тест 1 трек; записать результаты в `13-spike-cpu-models.md`/handoff.
7. Прогони quality gates; закоммить.

## Тесты

- **unit:** ratios validation; derivative rules (nightcore/sped up/slowed/remix/cover/instrumental); transformation dedupe; qualifier no duplicate; ambiguous; official flag.
- **contract:** `audio.stems.v1` schema; Demucs fake; `DerivativeHypothesis` schema; claims provenance.
- **integration (slow):** 1 трек 3–4 мин; time/RAM; vocal energy; derivative claims; no network if model local.
- **fixtures:** fake stem ratios, metadata/derivative pairs, MB relationship fixture, noisy title.
- **negative:** missing model; OOM simulated; conflicting evidence → ambiguous; unknown transformation; `--offline`; no evidence → no claim.

## Проверка

```bash
uv sync --extra tagging --extra asr --extra stems
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/analysis/test_demucs.py tests/domain/policies/test_derivative.py tests/application/test_derivative_claims.py -q
uv run pytest -m slow tests/integration/test_demucs_slow.py -q
uv run lint-imports
uv run trackforge models list --json
```

Ожидается: unit/derivative зелёные; slow-тест даёт время/RAM; без модели — `skipped`; claims не утверждают derivative без evidence.

## Acceptance criteria

- [ ] `audio.stems.v1` валиден; стемы не сохраняются по умолчанию.
- [ ] Demucs `-j 1`/`htdemucs`; missing model → `skipped`, не OOM.
- [ ] Derivative rules покрывают nightcore/sped up/slowed/remix/cover/instrumental.
- [ ] `is_official` только при evidence; иначе null/false.
- [ ] Конфликт evidence → `ambiguous`, не выбор наугад.
- [ ] `full`/`--disable stems` работают; `--offline` не скачивает.
- [ ] Slow-тест вне CI; time/RAM зафиксированы.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён; Essentia не добавлена.

## Handoff

Следующие планы могут считать готовыми:

- `audio.stems.v1`/`DemucsObserver`.
- Derivative evidence/hypotheses/claims/rules.
- `--enable/--disable stems`, `models download demucs`.
- Slow benchmark.
- Essentia явно Deferred.

## Stop conditions / decision gates

- Plan 13 no-go Demucs — стоп; Plan 17 `deferred`, derivative metadata/tempo-only.
- Нет `torch`/модели — extra optional; не ломай базовый install.
- Нужна Essentia — Deferred; AGPL plugin после v1.0.
- Нужен GPU/real-time — стоп, CPU-only.
- Конфликт evidence нельзя разрешить — `ambiguous`; не выбирай derivative без доказательств.
- Нужно сохранять стемы в library — вне scope; только debug/temp.
