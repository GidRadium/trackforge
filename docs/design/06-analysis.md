# 06. Analysis

> Статус: **черновик v0.3**. Каталог анализаторов, их контракты, стоимость, деградация и оценка качества.
> Ключевая граница: анализаторы создают **наблюдения** и **claims**, но не пишут финальные теги.

## 1. Профили

| Профиль | Что включено | Для кого |
|---|---|---|
| `fast` | probe, existing tags, source metadata, лёгкая акустика, lexical ranker | Быстрый прогон, слабый CPU |
| `balanced` (default) | всё из `fast` + fingerprint/AcoustID/MusicBrainz, обложки, lyrics-провайдеры, LLM по тексту, derivative-детектор | Обычная работа |
| `full` | всё из `balanced` + ASR (faster-whisper) + Demucs + cross-encoder; 0.5B fallback — опционально | Максимальное качество, есть время/CPU |
| `custom` | явные enabled/disabled | Тонкая настройка |

Правила:

- Профиль — это **набор capability + бюджет**, а не жёсткий код.
- Тяжёлые шаги (`full`) можно включать точечно: `--enable asr`, `--enable stems`.
- Отключение шага — явное, с записью `skipped` и причины.
- Дефолт — `balanced` (Q29).

## 2. Каталог анализаторов

| ID | Тип | Вход | Выход | Стоимость | Профиль |
|---|---|---|---|---|---|
| `ffprobe` | observer | media | `audio.probe.v1` | низкая | все |
| `initial_tags` | observer | media | `initial.tags.v1` | низкая | все |
| `source_metadata` | observer | source snapshot | `source.metadata.v1` | низкая | все |
| `chromaprint` | observer | media | `audio.fingerprint.v1` | низкая | balanced+ |
| `acoustid_lookup` | observer | fingerprint | `acoustid.lookup.v1` | сеть | balanced+ |
| `musicbrainz_search` | observer | metadata/fingerprint | `musicbrainz.search.v1` | сеть | balanced+ |
| `cover_search` | observer | metadata/identity | `cover.candidates.v1` | сеть | balanced+ |
| `lyrics_search` | observer | metadata | `lyrics.candidates.v1` | сеть | balanced+ |
| `librosa_features` | observer | media | `audio.features.v1` | средняя (CPU) | все |
| `essentia_features` | observer | media | `audio.features.v1` (optional fields) | средняя (CPU) | full/optional |
| `asr_transcribe` | observer | media | `asr.transcript.v1` | высокая (CPU) | full/opt-in |
| `demucs_stems` | observer | media | `audio.stems.v1` | очень высокая (CPU) | full/opt-in |
| `kind_classifier` | interpreter | observations | `kind` claims | низкая | все |
| `derivative_detector` | interpreter | observations + claims | `derivative` claims | низкая/средняя | balanced+ |
| `identity_resolver` | interpreter | observations + claims | identity hypotheses | низкая | все |
| `metadata_extractor` | interpreter | source metadata/LLM | title/artist/... claims | средняя | balanced+ |
| `lyrics_merger` | interpreter | lyrics + ASR | merged lyrics/timing claims | средняя | balanced+ |
| `cover_ranker` | ranker | candidates | scored covers | низкая | balanced+ |
| `claim_ranker` | ranker | claims | scored claims | низкая/средняя | balanced+ |
| `llm_interpreter` | interpreter/ranker | claims | structured claims/decisions | сеть/деньги | balanced+ |
| `llm_autopilot` | resolver helper | claims | decisions | сеть/деньги | opt-in |

## 3. Базовые наблюдения

### 3.1. `ffprobe`

- Контейнер, потоки, кодек, bitrate, sample rate, channels, duration, chapters, attached pictures.
- Не доверяем тегам из probe; теги — отдельно.
- Результат: `audio.probe.v1`.

### 3.2. `initial_tags`

- Читаем существующие теги: title, artist, album, albumartist, date, genre, track/disc, comment, lyrics, MBIDs, artwork.
- Не стираем и не «исправляем» исходник.
- Это **слабое доказательство**: пользователь мог ошибиться, а файл мог прийти из другого источника.
- Результат: `initial.tags.v1`.

### 3.3. `source_metadata`

- Нормализует `info.json`/source snapshot: title, uploader, description, tags, date, duration, thumbnails, chapters.
- Сохраняет raw snapshot отдельно.
- Результат: `source.metadata.v1`.

## 4. Акустика

### 4.1. `librosa_features` (balanced)

Дешёвые локальные признаки:

- duration, sample rate, channels;
- tempo/BPM (beat tracking; confidence; half/double ambiguity);
- key/mode (chroma + профиль; confidence);
- RMS/energy, dynamic range;
- spectral centroid/rolloff/bandwidth, ZCR;
- harmonic/percussive ratio;
- MFCC (для similarity/классификации, не для тегов);
- stereo width;
- silence/clipping (по возможности).

Результат: `audio.features.v1`.

Ограничения:

- BPM и key — оценки, не факты; храним method/confidence.
- Для nightcore/sped up BPM может быть удвоен/сдвинут; derivative-детектор это учитывает.
- Для классической/живой музыки key/BPM менее надёжны.

### 4.2. `essentia_features` (optional, AGPL)

- Готовые модели: key, mood, danceability, genre (если есть).
- Лицензия **AGPL-3.0-only** → отдельный plugin, не в default.
- Если недоступно — `librosa` + эвристики; качество ниже, но работает.
- Не смешивать scores Essentia и librosa без калибровки.

### 4.3. Loudness / ReplayGain

- Интегральная громкость (LUFS), true peak, LRA.
- Источники: `ffmpeg ebur128` или `pyloudnorm`.
- ReplayGain-теги — отдельное решение рендера; не путать с «громкостью трека».
- Для nightcore/sped up громкость может быть клиппирована; фиксируем warnings.

## 5. Fingerprint и идентичность

### 5.1. Chromaprint

- `fpcalc` → fingerprint + duration.
- Быстро, локально; используется для AcoustID.
- Не является «идентификатором трека» сам по себе; это вход для lookup.

### 5.2. AcoustID

- Lookup по fingerprint + duration.
- Возвращает candidates с score и recording MBID.
- Score — не вероятность «это тот самый трек»; калибруем/трактуем консервативно.
- Если несколько кандидатов — `IdentityHypothesis` с несколькими вариантами.

### 5.3. MusicBrainz

- Search по title/artist/duration; lookup по MBID; relationships.
- Даёт recording/release/release-group/artist, ISRC, genres/tags, relationships (remix/cover).
- Rate limit 1 req/s; кэш.
- MBID — сильный якорь, но не обязательный.

### 5.4. Merge identity

- AcoustID + MB + source metadata + duration + title similarity.
- Если evidence противоречивы — `ambiguous`, не выбираем «наугад».
- Результат: `identity.hypotheses.v1`.

## 6. ASR / транскрипция

### 6.1. Модель

- `faster-whisper` (CTranslate2), CPU int8.
- Модели: tiny/base/small/medium/large; default для `full` — `small` (компромисс), `medium` — опция.
- VAD для отсечения тишины; word timestamps; language detection.
- Модель загружается через `ModelManager`, не внутри функции.

### 6.2. Выход

```text
asr.transcript.v1:
  language
  segments[]:
    start, end, text, words[] (word, start, end, probability)
  avg_logprob, no_speech_prob
  model, version
```

- Plain text, LRC, ELRC — производные представления.
- LRC: строки по сегментам; ELRC: слова с таймингами.
- Для музыки ASR на миксе шумный; если есть Demucs vocals — лучше.
- Для nightcore/sped up качество падает; используем оригинальные lyrics, если найдены.

### 6.3. Слияние с lyrics

Сценарии:

1. **Есть LRC** — используем как основной текст/тайминг; ASR — для проверки/уточнения.
2. **Есть plain lyrics, нет таймингов** — ASR даёт тайминги; alignment переносит слова на известный текст (forced alignment / DP / LLM-слияние).
3. **Нет lyrics** — ASR как текст; LRC по сегментам.
4. **ASR плохой, lyrics хорошие** — текст из lyrics, тайминги из ASR; низкое качество таймингов помечаем.
5. **Nightcore** — тайминги ASR можно масштабировать на speed factor; текст — из оригинала.

Важно: не «улучшать» текст LLM без доказательств; можно исправлять пунктуацию/регистр, но не переписывать смысл.

### 6.4. Ограничения

- CPU-only: `medium`/`large` могут быть слишком медленными.
- Музыка + эффекты + бэкинг-вокал → ошибки.
- Языки/код-свитчинг → ошибки; language detection не всегда верен.
- Инструментальные треки → пустой/мусорный текст; VAD/энергия помогают.

## 7. Demucs / стемы

- `htdemucs` (или актуальная модель) на CPU — **очень медленно**.
- Использование:
  - vocal energy ratio;
  - instrumental/acapella;
  - drums/bass/other ratios;
  - улучшение ASR (vocals);
  - derivative-детектор (вокал/форманты).
- По умолчанию **выключено**; `full`/opt-in.
- Стемы можно не сохранять (только признаки), чтобы не есть диск.
- Fallback без Demucs: метаданные/LLM + спектральные эвристики с низкой уверенностью.

## 8. Определение типа и derivative

### 8.1. `ContentKind`

`music | audiobook | podcast | talk | sound_effect | other`.

Признаки:

- длительность, главы, количество треков;
- source category/description;
- наличие музыки/вокала;
- ASR (речь vs пение);
- обложка/название.

Не смешивать с derivative: подкаст тоже может быть sped up.

### 8.2. Derivative taxonomy

`nightcore`, `sped_up`, `slowed`, `reverb`, `8d`, `remix`, `bootleg`, `mashup`, `cover`, `instrumental`, `acapella`, `karaoke`, `radio_edit`, `extended`, `live`, `acoustic`, `demo`, `other`.

### 8.3. Признаки

| Признак | Что даёт |
|---|---|
| Title/description/tags/uploader | прямые подсказки (`nightcore`, `slowed + reverb`, `8D`) |
| Duration ratio к оригиналу | nightcore/sped up короче; slowed длиннее |
| BPM ratio | ~1.2–1.35 → sped up/nightcore; ~0.8–0.95 → slowed |
| Pitch/formants | nightcore часто повышает pitch; slowed — понижает |
| Spectral centroid | brighter/darker |
| Vocal energy | instrumental/acapella |
| MB relationships | remix/cover официально |
| AcoustID | совпадение с original recording |
| LLM | интерпретация описания/названия |

### 8.4. Правила

- Один derivative может иметь несколько transformations (`nightcore` + `sped_up`).
- Не утверждать derivative без evidence; при конфликте — `ambiguous`.
- `is_official` (official remix vs bootleg) — отдельное поле, если есть данные.
- Параметры (`speed_factor`, `pitch_semitones`, `bpm_ratio`) храним, если оценены.
- Nightcore-треки: title/artist policy — в `07-resolution.md`; анализ только даёт claims.

## 9. LLM

### 9.1. Роли

1. **Extractor** — вытащить title/artist/type/derivative из шумного описания.
2. **Query generator** — предложить поисковые запросы.
3. **Adjudicator** — выбрать из claims/кандидатов.
4. **Merger** — слить правильный текст с таймингами.
5. **Autopilot** — принять решения по всем полям (явный режим).

LLM **не** является источником фактов без evidence. Он не создаёт MBID/ISRC/URL.

### 9.2. Вход

- Нормализованные claims + source snippets.
- Не сырое аудио по умолчанию (audio — opt-in).
- Не весь `info.json` целиком: отбираем релевантные поля, чистим мусор.
- Prompt injection: description — недоверенный текст; обрамляем, инструктируем игнорировать инструкции, валидируем вывод.

### 9.3. Выход

Строгая JSON-схема:

```json
{
  "claims": [
    {
      "field": "recording.title",
      "value": "...",
      "confidence": 0.82,
      "evidence_claim_ids": ["..."],
      "quote": "...",
      "reason": "..."
    }
  ],
  "unknown": ["label"]
}
```

- Никаких свободных тегов вне схемы.
- Каждое значение — со ссылкой на claim/цитату.
- Если доказательств нет — `unknown`.
- Невалидный JSON → retry 1 → fallback ranker/policy.

### 9.4. Провайдеры

- OpenAI-compatible API (OpenAI, OpenRouter, локальный Ollama, др.).
- Anthropic/другие — через адаптер, если нужно.
- Модель/температура/max_tokens/бюджет — в конфиге.
- Кэш по входу + модели + промпту; стоимость в `DecisionRecord`.
- **API — основной LLM.** Локальный fallback — модель класса **0.5B** (classification/ranking/короткий JSON), не autopilot и не генерация длинных текстов; 7B/13B локально не планируются.

### 9.5. Промпты

- Версионируются (`prompts/<task>/vN.md`).
- В промпте — схема, правила, примеры, запрет выдумывать ID.
- Evaluation перед сменой версии.
- Не менять промпт «на лету» без версии.

## 10. Ranker

### 10.1. Зачем

Выбор из конечного набора — не генерация. Дешевле, стабильнее, работает offline.

### 10.2. Слои

1. **Lexical baseline** — нормализация + `rapidfuzz`/token set ratio. Всегда доступен.
2. **Cross-encoder** — маленькая multilingual модель на CPU/ONNX; rerank top-N.
3. **Local LLM** — последний резерв для сложных случаев; constrained choice.

### 10.3. Применение

- ranking title/artist candidates;
- ranking search results;
- ranking covers;
- ranking identity hypotheses;
- tie-break в policy.

### 10.4. Калибровка

- Scores разных ranker'ов не сравниваем напрямую.
- Score — evidence для policy, не финальная уверенность.
- Для cross-encoder — калибровка на размеченном наборе (позже).

## 11. Обложки и lyrics

### 11.1. Обложки

Провайдеры: Cover Art Archive → iTunes → Deezer → thumbnail источника.

Критерии:

- разрешение/качество;
- front cover;
- соответствие release/recording;
- отсутствие watermarks/текста;
- лицензия/источник.

Результат: `cover.candidates.v1` + `cover_ranker` claims.

### 11.2. Lyrics

Провайдеры: LRCLIB → Genius (осторожно) → ASR.

- Plain/LRC; язык; наличие таймингов.
- LRC парсим/валидируем; битые тайминги — warning.
- Слияние с ASR — см. §6.3.
- Embedding/sidecar — в `08-metadata.md`.

## 12. Деградация

| Ситуация | Что делаем | Статус |
|---|---|---|
| Нет сети | локальные анализаторы; title/artist могут быть `unknown` | `degraded` |
| Нет AcoustID key | MB search + ranker/LLM | `degraded` |
| Нет Demucs | без vocal features; derivative по метаданным/темпу | `skipped` |
| Нет ASR | lyrics из провайдеров; иначе `unknown` | `skipped`/`degraded` |
| Нет облачной LLM | cross-encoder → lexical → policy | `degraded` |
| Нет cross-encoder | lexical baseline | `degraded` |
| Модель не влезает в память | skip + warning | `skipped` |
| LLM вернул мусор | retry → fallback | `degraded` |

Все деградации — с `warning.code`/`message`; не молчим.

## 13. Модели и ресурсы

### 13.1. ModelManager

- Lazy load; кэш в `cache/models`.
- Checksum, license, size в `model_registry`.
- Не более N тяжёлых моделей одновременно (default 1).
- `--offline` запрещает скачивание.
- `doctor` проверяет наличие/версии.

### 13.2. Оценки (замерить на целевом CPU)

| Шаг | Ожидаемая стоимость (3.5-мин трек) | Память |
|---|---|---|
| ffprobe | <1 с | низкая |
| chromaprint | секунды | низкая |
| librosa features | секунды | средняя |
| AcoustID/MB | сеть; MB 1 req/s, AcoustID 3 req/s | низкая |
| faster-whisper small int8 | ~15–35 с (RTF 6–12x) | ~1.5–2 ГБ |
| faster-whisper medium | ~60–110 с (RTF 2–3x) | ~3–4 ГБ |
| Demucs htdemucs (PyTorch) | ~1.5× длительности, т.е. ~5 мин | ~3–7 ГБ |
| Demucs ONNX/OpenVINO (spike) | ~30–60 с | ~1–4 ГБ |
| cross-encoder MiniLM QInt8 | ~0.6–1.2 с на 100 кандидатов | <0.5 ГБ |
| cross-encoder BGE base | ~3–6 с на 100 кандидатов | ~1 ГБ |

Точные цифры — после Phase 0 spike; оценка и источники — в [`14-phase0-spikes-assessment.md`](14-phase0-spikes-assessment.md).

## 14. Оценка качества

### 14.1. Данные

- Небольшой размеченный набор (20–50 треков) с ground truth: title/artist/album/date/genre/cover/lyrics/kind/derivative.
- Пары «оригинал ↔ derivative» для детектора.
- Пользовательские corrections как holdout (не обучаемся на них втихую).
- Разные языки/источники/качество.

### 14.2. Метрики

- field accuracy / correction rate;
- derivative precision/recall;
- ASR WER (если есть эталон);
- cover selection accuracy;
- calibration (confidence vs correctness);
- latency/CPU/cost;
- degraded rate.

### 14.3. Правила

- Менять промпт/политику только после прогона на наборе.
- Не сравнивать сырые scores разных анализаторов.
- Хранить версии моделей/промптов в `DecisionRecord`.

## 15. Открытые вопросы

- Какая cross-encoder модель: multilingual, Apache/MIT, ONNX, размер? После бенчмарка; ID — в `[models.ranker]`.
- Какой Whisper default для `full`: `small` или `medium`? После бенчмарка; ID — в `[models.asr]`.
- Включать ли Essentia в `full` (AGPL) или оставить отдельным plugin? Предложение: plugin.
- Demucs входит в `full`, но отключается через `--disable stems`; отдельный «full без Demucs» не нужен — это и есть `custom`.
- LLM-провайдер: в коде нет default; `[llm] base_url/model` — в `config.toml`, API key — в `.env`.
- Локальный fallback 0.5B: нужен ли в v1? Предложение: опционально, только classification/ranking; 7B/13B не планируем.
- Поддержка языков: ru/en в приоритете; остальные — как получится.
- Минимальный evaluation-набор и кто его размечает.
