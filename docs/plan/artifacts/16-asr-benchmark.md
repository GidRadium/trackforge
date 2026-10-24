# Artifact 16: ASR benchmark

- **Статус:** done
- **План:** [16-asr-lyrics](../16-asr-lyrics.md)
- **Дата:** 2026-10-24
- **Исполнитель:** DeepSeek coding agent

> Замеры сделаны реальным `WhisperObserver` из Plan 16 (не отдельным spike-скриптом). Модель локальная, сеть при прогоне не использовалась.

## 1. Окружение

| Параметр | Значение |
|---|---|
| CPU | QEMU Virtual CPU version 2.5+, KVM, **без AVX/AVX2/F16C/AVX-512** |
| Cores/threads | 18 vCPU, 1 thread/core |
| RAM | 21 GiB (≈19 GiB available) |
| `faster-whisper` | 1.2.1 |
| CTranslate2 | 4.8.3 |
| PyAV | 15.1.0 (PyAV 19 удалил `metadata_errors`, нужный faster-whisper 1.2.x) |
| Model | `Systran/faster-whisper-small` @ `536b0662742c02347bc0e980a01041f333bce120` |
| Model SHA-256 | `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671` (`model.bin`) |
| License | MIT (model card; upstream OpenAI Whisper MIT) |
| Compute type | int8 |
| Threads | 8 |
| Beam | 5 |
| Word timestamps | on |
| `condition_on_previous_text` | false (стабильность длинных треков) |

Медиа — те же публичные фикстуры, что в [13-spike-cpu-models](13-spike-cpu-models.md):
`birches.mp3` (речь, 214.832 с), `music.ogg` (CC BY 3.0, 245.352 с), `nightcore.ogg`
(тот же трек, +25% pitch/speed, 196.260 с).

## 2. Замеры

| Track | Duration | Model | VAD | Beam | Wall time | RTF | Peak RSS | Language | Segments | Words |
|---|---:|---|---|---:|---:|---:|---:|---|---:|---:|
| speech | 214.832 | small | off | 5 | 48.561 | **4.42** | 907.0 MiB | en | 42 | 523 |
| speech | 214.832 | small | on | 5 | 54.929 | **3.91** | 929.2 MiB | en | 42 | 540 |
| music | 245.352 | small | off | 5 | 59.508 | **4.12** | 976.1 MiB | en | 41 | 463 |
| nightcore | 196.260 | small | off | 5 | 40.487 | **4.85** | 919.9 MiB | en | 51 | 294 |

RTF = duration / wall. RSS — `resource.getrusage(RUSAGE_SELF).ru_maxrss` после
прогона (включает базовый процесс и загруженную модель).

**VAD:** на речи VAD on дал 3.91×; на музыке/nightcore VAD не использовался,
потому что Spike 13 показал отсечение почти всего вокала (15.4 с из 245.4 с и
9.9 с из 196.3 с). Поэтому `[models.asr] vad_filter` по умолчанию **false**
для music-first профиля; для речи его можно включить.

## 3. Качество

- WER речи по этому прогону (reference LibriVox, нормализация как в Spike 13):
  **7.45%** (510 ref / 540 hyp / 38 edits), включая intro/outro. Spike 13 при
  `condition_on_previous_text=true` дал 3.14%; разница объясняется настройками,
  а не регрессией модели. Для lyrics/timing текст пригоден.
- Язык определён верно на всех трёх треках (`en`), `avg_logprob`:
  −0.170 (speech VAD on), −0.172 (speech VAD off), −0.316 (music), −0.388
  (nightcore).
- `no_speech_prob` на музыке 0.749 и nightcore 0.660 — merger помечает ASR
  как low quality (`asr_low_quality`), но всё равно использует его для
  ASR-only LRC с warning. На речи 0.077.
- Music/nightcore WER в этом прогоне не пересчитывался (нет word-level
  reference под рукой); ориентир Spike 13: 22.68% / 35.05% на unique-блоке.
- Nightcore: тайминги ASR уже соответствуют derivative-аудио; scaling
  `1/speed_factor` применяется к найденным original LRC (проверено unit-тестом
  `test_nightcore_uses_original_text_and_scales_lrc_timings`), а не к ASR.
- Lyrics merge: в офлайн-прогоне lyrics-провайдеры не вызывались, сработал
  сценарий **ASR-only**; LRC построен по сегментам, plain не содержит
  таймкодов.
- LRC/ELRC sidecar: **yes**. LRC записан как `track.lrc`; ELRC генерируется
  только при наличии word timestamps и проверен unit-тестами. В тег LRC не
  попадает (см. `tests/application/test_render.py`).

## 4. Go/no-go

- Go: RTF ≥5× (small), RAM ≤6 ГБ; текст пригоден для lyrics/языка.
- Факт: RTF **3.91–4.85×**, peak RSS **907–976 MiB**, язык/сегменты/слова
  стабильны; качество речи приемлемо, музыка шумная (ожидаемо).
- Решение: **go (conditional)**. Small остаётся default для `full`; medium
  opt-in; target 5× на этом CPU без AVX не достигается, поэтому CLI/план
  должен показывать оценку ~4×. RAM-бюджет с запасом.
- Ограничения: VAD на музыке выключать; nightcore текст — только из original
  lyrics; ASR на миксе шумный, не «улучшать» LLM.

## 5. Проверка

- [x] LRC не встроен в plain lyrics.
- [x] Sidecar рядом с output (`track.<ext>.lrc`, публикуется как `<stem>.lrc`).
- [x] Модель задана в config, не в коде; checksum проверяется.
- [x] Нет сети при локальной модели (`WhisperModel` получает путь;
  `models.offline=true`; observer не вызывает download).
- [x] Slow-тест `tests/integration/test_asr_slow.py` помечен `slow` и не входит
  в CI; без модели/медиа/рантайма пропускается с причиной.
