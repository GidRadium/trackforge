# Artifact 17: Demucs + derivative handoff

- **Статус:** completed
- **План:** [17-demucs-derivatives](../17-demucs-derivatives.md)
- **Дата:** 2026-10-24
- **Режим:** CPU-only, `htdemucs`, `-j 1`, модель и аудио из Plan 13 (`.spike-private/bench`, git-ignored).

## Короткий вывод

- Demucs PyTorch `htdemucs` реализован как opt-in observer `audio.stems.v1`; `full`/`--enable stems` включают его, `--disable stems` выключает с warning.
- Стемы не сохраняются: в artifact попадают только energy ratios, `vocal_energy`, `spectral_centroid`, `rms` и warnings.
- Slow benchmark на реальном треке: **247.5 с** на 245.4 с аудио (**RTF 1.009**), **peak RSS 2217.7 MB**, vocal ratio **26.1%**; локальная модель, сеть не использовалась. Это совпадает с go-оценкой Plan 13 (0.95–0.96×, 1.84–2.08 ГиБ) с точностью до overhead на observer/CLI.
- Derivative-детектор использует metadata + duration/BPM + vocal/spectral + MusicBrainz relationships; при конфликте evidence выдаёт `ambiguous`, и resolver не применяет qualifier.
- Essentia не добавлялась; `docs/design/` не изменялся.

## 1. Реализация

| Компонент | Файл |
|---|---|
| `audio.stems.v1` schema | `src/trackforge/application/dto/stems.py` |
| `DerivativeHypothesis` / hypotheses schema | `src/trackforge/application/dto/derivatives.py` |
| Rules/thresholds/evidence | `src/trackforge/domain/policies/derivative.py` |
| Demucs observer | `src/trackforge/adapters/analysis/demucs.py` |
| Model registry/download | `src/trackforge/application/ports/models.py`, `src/trackforge/adapters/models/local.py` |
| Profile/CLI wiring | `plan_item.py`, `observe_item.py`, `bootstrap/container.py`, CLI commands |
| Claims/resolver integration | `interpret_item.py`, `resolve_item.py` |
| Tests | `tests/adapters/analysis/test_demucs.py`, `tests/domain/policies/test_derivative.py`, `tests/application/test_derivative_claims.py`, `tests/integration/test_demucs_slow.py` |

## 2. Конфигурация

```toml
[models.stems]
name = "htdemucs"
license = "MIT"
# Optional: defaults to the Plan 13 checkpoint metadata for htdemucs.
version = "955717e8-8726e21a"
checksum = "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4"
url = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"
segment = 7.8
overlap = 0.25
jobs = 1
threads = 8
memory_mb = 3072
```

`[models.demucs]` is accepted as an alias. The model remains opt-in: with an empty
`stems_model`, `full` plans the step and reports a visible `model_missing` skip;
derivative analysis still works from metadata/duration evidence.

```bash
uv sync --extra tagging --extra asr --extra stems
uv run trackforge models download demucs
uv run trackforge models list --json
uv run trackforge import track.flac --profile full
uv run trackforge import track.flac --profile full --disable stems
```

`--offline` blocks `models download` with `OfflineModeError`; a local checkpoint
continues to work offline. Analysis never downloads weights implicitly.

## 3. Slow benchmark

Command:

```bash
TRACKFORGE_DEMUCS_AUDIO=.spike-private/bench/audio/music.ogg \
TRACKFORGE_DEMUCS_MODEL=.spike-private/bench/torch-cache/hub/checkpoints/955717e8-8726e21a.th \
TRACKFORGE_DEMUCS_BENCH_OUT=docs/plan/artifacts/17-demucs-benchmark.json \
uv run pytest -m slow tests/integration/test_demucs_slow.py -q -s
```

| Параметр | Значение |
|---|---:|
| Длительность трека | 245.352 с |
| Wall time (observer) | 247.548 с |
| RTF | 1.009 |
| Peak RSS | 2217.7 MB |
| RSS до прогона | 255.0 MB |
| Vocals ratio / vocal_energy | 26.08% |
| Instrumental ratio | 73.92% |
| Spectral centroid | 3130.5 Hz |
| RMS | 0.2978 |
| Статус | succeeded, warnings: none |

Raw JSON: [`17-demucs-benchmark.json`](17-demucs-benchmark.json). The test also
runs the real stem payload through `DerivativeDetector` and asserts that
transformation claims are produced; it never downloads weights.

## 4. Derivative rules

- Metadata markers: title/description/tags/comment/genre/album/path/source
  uploader; confidence by origin (title highest, album lowest).
- Duration ratio vs a MusicBrainz recording: `1.15–1.60` speed-up,
  `0.60–0.90` slowed; BPM ratio: `1.18–1.40` speed-up, `0.70–0.93` slowed.
- Vocals: `<= 0.12` → instrumental; `>= 0.55` with drums `<= 0.12`,
  bass `<= 0.15`, other `<= 0.20` → acapella.
- Spectral centroid vs a reference: `>= 1.12×` supports speed-up,
  `<= 0.89×` supports slowed.
- MusicBrainz remix/cover/live/etc. relationships map to transformations;
  `official=true` is the only evidence that sets `is_official=true`;
  explicit unofficial/bootleg evidence can set `false`, otherwise it stays
  `null`.
- Nightcore adds an explicit implied `sped_up` evidence item and estimates
  `pitch_semitones` from the speed factor when one is available.
- Accel + decel evidence (or instrumental + acapella) → `ambiguous`, score
  capped at 0.5. `DerivativeDetector` writes the status claim; `ResolveItem`
  filters transformation claims when status is `ambiguous`, so no title
  qualifier is guessed.
- LLM claims are weak evidence (max 0.55) and never set official status.

## 5. Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/analysis/test_demucs.py tests/domain/policies/test_derivative.py tests/application/test_derivative_claims.py -q
uv run pytest -m slow tests/integration/test_demucs_slow.py -q
uv run lint-imports
uv run trackforge models list --json
```

The slow test is excluded from the default pytest addopts (`-m 'not network and not slow'`).
