# Plan 13: Phase 0 spike — CPU-модели

- **Статус:** conditional
- **Тип:** Spike
- **Зависит от:** Plan 01
- **Разблокирует:** Plan 14, 16, 17
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior engineer, проводящий CPU-бенчмарки. Работаешь в `trackforge`, но **не пишешь production-код** и не меняешь `src/`. Задача — измерить Whisper, Demucs и cross-encoder на целевом CPU, сравнить с оценками `14-phase0-spikes-assessment.md` и дать go/no-go.

## Цель

Заполнен `docs/plan/artifacts/13-spike-cpu-models.md`: железо, версии, модели/лицензии, RTF, peak RSS, latency, качество на 2–3 треках, go/no-go по каждому классу и рекомендация default-моделей для `config.toml` (не в коде).

## Контекст и инварианты

- CPU-only, 16–18 ГБ RAM, Linux; точная модель CPU/AVX неизвестна (`14-phase0-spikes-assessment.md` §1).
- Тяжёлые модели — последовательно, не параллельно; `ModelManager` позже.
- Whisper `small` — безопасный default для `full`; `medium` — опция; `large` не планируется.
- Demucs PyTorch `htdemucs` ≈1.5× длительности; `htdemucs_ft` ×4 — не для bulk; `-j 1`.
- ONNX/OpenVINO Demucs — отдельный spike внутри spike; только если PyTorch слишком медленный.
- Cross-encoder: MiniLM QInt8 — дефолт; BGE — top-N/если качество критично.
- Модели/веса имеют отдельные лицензии; зафиксировать в отчёте.
- Никаких production-зависимостей в `pyproject.toml`; spike — во временном venv/`uvx`.

## Прочитать перед началом

- `docs/design/14-phase0-spikes-assessment.md` — §3.4–3.7, §4, §7–8.
- `docs/design/06-analysis.md` — §4, §6–7, §10, §13.
- `docs/design/10-operations.md` — §5.
- `docs/design/11-quality.md` — §4.
- `docs/plan/artifacts/13-spike-cpu-models.md` (шаблон).
- `docs/plan/01-bootstrap.md` (toolchain).

## Предусловия

```bash
nproc
free -h
lscpu | head -30
python3 --version
uv --version
ffmpeg -version
```

Нужны 2–3 реальных трека (3–4 мин): речь/музыка/nightcore или derivative. Нужно место на диске (модели + stems) и разрешение на скачивание моделей. Если моделей/сети нет — `blocked`; не выдумывай цифры.

## Scope

- Whisper: `faster-whisper` small int8, medium int8; threads 8/12; `vad_filter` on/off; `beam_size=5`; word timestamps.
- Demucs: PyTorch `htdemucs`, `-j 1`; измерить time/RAM; опционально `htdemucs_ft` на одном треке; ONNX/OpenVINO — только если PyTorch >3× duration.
- Cross-encoder: MiniLM QInt8/BGE base, 50–100 пар title/artist/cover; latency/throughput/RAM; top-1/top-3 на 20–30 своих примерах.
- Записать качество: Whisper WER/фрагменты, Demucs vocal/instrumental, cross-encoder top-k.
- Заполнить артефакт и дать go/no-go.

## Non-goals

- Писать адаптеры/ModelManager/CLI.
- ASR/Demucs merge, LRC/ELRC, derivative detector.
- Обучение/дообучение моделей.
- GPU/CUDA.
- Скачивание моделей в CI.
- Local LLM 0.5B — **Deferred**, не этот spike.

## Deliverables

```text
docs/plan/artifacts/13-spike-cpu-models.md   # заполненный отчёт
```

Никаких изменений в `src/`, `tests/`, `pyproject.toml`, `docs/design/`.

## Контракты (формат отчёта)

```text
1. Железо: CPU model, cores/threads, RAM, distro, Python, ffmpeg.
2. Модели: id, размер, лицензия, версия runtime, checksum (если есть).
3. Whisper: таблица model/threads/batch/vad/beam, audio duration, wall time,
   RTF, peak RSS, language, quality notes.
4. Demucs: model/segment/overlap/jobs, wall time, RTF, peak RSS, stem quality notes.
5. Cross-encoder: model/precision/threads, pairs, latency, pairs/s, RAM, top-k.
6. Сравнение с оценками docs; отклонения и причины.
7. Go/no-go по каждому пункту §7 docs.
8. Рекомендация: default ASR model, Demucs mode, ranker model; всё — ID для config,
   не hardcode.
```

## Порядок работ

1. Зафиксируй железо/версии; выбери 2–3 трека.
2. Прогони Whisper small/medium на одном треке; замерь `/usr/bin/time -v`.
3. Повтори на остальных; сравни threads 8/12 и `vad_filter`.
4. Прогони Demucs `htdemucs -j 1`; замерь time/RSS; опционально `htdemucs_ft`.
5. Если PyTorch >3× duration — проверь ONNX/OpenVINO на одном сегменте.
6. Прогони cross-encoder на 50–100 парах; замерь latency/RAM/top-k.
7. Заполни артефакт; сделай вывод.
8. Удали временные stems/venv; модели можно оставить в `~/.cache`, но не в git.

## Тесты

Spike не добавляет unit-тестов. Проверки:

- `/usr/bin/time -v` peak RSS;
- `ffprobe` длительность трека;
- JSON/CSV с результатами;
- отсутствие production-изменений (`git status`).

## Проверка

```bash
/usr/bin/time -v uvx --with faster-whisper python -c "import faster_whisper; print(faster_whisper.__version__)"
/usr/bin/time -v uvx --with demucs python -c "import demucs; print(demucs.__version__)"
git status --short
rg -n "TBD|TODO|<fill" docs/plan/artifacts/13-spike-cpu-models.md || true
```

Ожидается: артефакт заполнен реальными числами; `git status` не содержит `src/`/`tests/`/`pyproject.toml`; нет TBD.

## Acceptance criteria

- [ ] Отчёт заполнен по формату, с железом и версиями.
- [ ] Whisper small и medium измерены на 2–3 треках: RTF, peak RSS, качество.
- [ ] Demucs `htdemucs -j 1` измерен: time, RTF, peak RSS; `htdemucs_ft` — опционально.
- [ ] Cross-encoder MiniLM измерен на 50–100 парах; BGE — по возможности.
- [ ] Зафиксированы лицензии моделей/runtime.
- [ ] Go/no-go по каждому пункту §7 docs; рекомендации model ID для config.
- [ ] `src/`, `tests/`, `pyproject.toml`, `docs/design/` не изменены.
- [ ] Нет TBD/TODO и выдуманных benchmark-чисел.

## Handoff

- Plan 14 получает: ranker model/размер/лицензию, нужен ли cross-encoder в v1.0, latency.
- Plan 16 получает: Whisper default (small/medium), threads, RAM, go/no-go.
- Plan 17 получает: Demucs mode (PyTorch/ONNX), `-j 1`, go/no-go.
- Если no-go: Plan 16/17 `deferred`, релиз без них; Plan 14 — lexical only.
- Модели задаются в `config.toml`, не в коде (Q17/уточнение review).

## Stop conditions / decision gates

- Нет 2–3 треков/места/разрешения на модели — `blocked`; не выдумывай.
- Whisper medium OOM/RTF <1.5× — medium no-go; small может остаться.
- Demucs >3× duration/OOM — PyTorch no-go; ONNX spike или defer.
- Cross-encoder не лучше lexical — lexical only; D8.
- D8 не решён — используй MiniLM QInt8 как рекомендацию, но зафиксируй в отчёте.
- Нужен GPU/CUDA — стоп; CPU-only.
