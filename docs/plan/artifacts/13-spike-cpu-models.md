# Artifact 13: CPU models spike

- **Статус:** completed
- **План:** [13-spike-cpu-models](../13-spike-cpu-models.md)
- **Дата:** 2026-10-23
- **Исполнитель:** spike executor (automated coding agent)
- **Режим:** CPU-only, последовательные прогоны, временные venv, production-код не менялся.

## Короткий вывод

- Железо — виртуальный QEMU CPU **без AVX/AVX2/F16C/AVX-512**. Это главная причина, почему Whisper заметно медленнее оценок из `14-phase0-spikes-assessment.md`; цифры нельзя без оговорки переносить на «настоящий» 18-ядерный CPU с AVX2.
- **Whisper small — conditional go**: 3.74× на речи, 4.05–4.61× на музыке/nightcore, peak RSS ≈1.0 ГБ. Это выше жёсткого no-go порога 3×, но ниже целевых 5×. Для `full` остаётся default, medium — только opt-in.
- **Whisper medium — no-go как default**: 1.46× на речи (два прогона), 1.58× на музыке, 1.67× на nightcore, peak RSS ≈2.1 ГБ. Качество на речи лучше small лишь на 0.4 п.п. WER, на музыке — на 8.3 п.п., но цена в 2.5–3× времени.
- **Demucs PyTorch `htdemucs` — go (opt-in)**: 0.96× длительности на музыке и 0.95× на nightcore, peak RSS 2.08/1.84 ГиБ, `-j 1`. ONNX/OpenVINO не проверялся, потому что триггер «PyTorch >3× длительности» не наступил.
- **Cross-encoder — conditional go как optional**. Lexical baseline: top-1 88%, top-3 100%, 1.5 мс/100 пар. MiniLM QInt8: top-1 96%, top-3 100%, 0.33 с/100, 129 МиБ. BGE base INT8: top-1/top-3 100%, 1.55 с/100, 820 МиБ. Top-3 упёрся в потолок, поэтому формальный критерий «top-3 > lexical» не доказан; top-1-прирост есть, но статистически слабый (25 запросов).
- **Рекомендация для config:** ASR — `Systran/faster-whisper-small`, int8, 8 threads; Demucs — `htdemucs`, `-j 1`, 4 стема, opt-in; ranker — lexical по умолчанию, optional `BAAI/bge-reranker-base` INT8 для RU/translit, MiniLM QInt8 — облегчённая альтернатива. Cross-encoder не должен быть обязательной зависимостью v1.0.
- В `src/`, `tests/`, `pyproject.toml`, `docs/design/` изменений нет.

## 1. Железо и окружение

| Параметр | Значение |
|---|---|
| CPU model | `QEMU Virtual CPU version 2.5+` (KVM, x86_64) |
| Cores/threads | 18 vCPU / 18 threads, 1 thread/core, 1 socket |
| SIMD | SSE4.2, AES, POPCNT; **AVX, AVX2, F16C, AVX-512 отсутствуют** |
| L3 cache | 16 MiB |
| RAM | 21 GiB total, 19 GiB available на старте, swap 4 GiB |
| Storage | 95 GiB volume, 77 GiB свободно до моделей; 72 GiB после установки моделей, 74 GiB после очистки |
| Linux distro | Ubuntu 24.04.5 LTS, kernel 6.8.0-147-generic |
| Python | 3.12.3 |
| `uv` | 0.13.5 |
| `ffmpeg` / `ffprobe` | 6.1.1-3ubuntu5 |
| PyTorch CPU capability | `DEFAULT` (`torch.backends.cpu.get_cpu_capability()`), не AVX2 |

RAM в этом окружении больше целевых 16–18 ГБ, но все тяжёлые модели запускались **по одной**, поэтому результаты по памяти можно считать верхней оценкой для 16 ГБ.

## 2. Модели, версии, лицензии

### 2.1. Модели

| Компонент | Model ID / revision | Runtime | Size | License | SHA-256 |
|---|---|---|---:|---|---|
| Whisper small | `Systran/faster-whisper-small` @ `536b0662742c02347bc0e980a01041f333bce120` | faster-whisper 1.2.1, CTranslate2 4.6.0 | 483 546 902 B (`model.bin`), каталог 464 MiB | MIT (model card; upstream OpenAI Whisper MIT) | `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671` |
| Whisper medium | `Systran/faster-whisper-medium` @ `08e178d48790749d25932bbc082711ddcfdfbc4f` | faster-whisper 1.2.1, CTranslate2 4.6.0 | 1 527 906 378 B (`model.bin`), каталог 1.5 GiB | MIT (model card; upstream OpenAI Whisper MIT) | `9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae` |
| Demucs htdemucs | `955717e8-8726e21a.th` из Demucs v4 | demucs 4.0.1, PyTorch 2.14.1+cpu, torchaudio 2.11.0+cpu | 84 141 911 B | MIT (Demucs repo; отдельной model card у весов нет) | `8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4` |
| Demucs ONNX/OpenVINO | не запускался | — | — | — | — |
| Cross-encoder MiniLM | `temsa/ms-marco-MiniLM-L-6-v2-onnx-cpu-qint8` @ `a91184fb9ccba1b862518a5eedc5b8b68ee116f2` | onnxruntime 1.23.2, transformers 4.57.1 | 23 180 880 B | Apache-2.0 | `5741182c012282d6fe025705a0a4b43884c9f778da01f7ac7b20ff82097829a5` |
| Cross-encoder BGE base | `BAAI/bge-reranker-base` @ `2cfc18c9415c912f9d8155881c133215df768a70`, ONNX FP32 + dynamic INT8 (сделано локально) | onnxruntime 1.23.2, transformers 4.57.1 | 278 882 955 B INT8 (исходный FP32 — 1 112 459 588 B) | MIT | `bb395f84afbcf05b769942ae14358da25a1e6d77a07db671bd098fe36bd23c2e` |

### 2.2. Runtime и лицензии

| Компонент | Версия | Лицензия |
|---|---|---|
| Python | 3.12.3 | PSF |
| faster-whisper | 1.2.1 | MIT |
| CTranslate2 | 4.6.0 | MIT |
| PyAV | 15.1.0 | BSD-3-Clause |
| NumPy | 2.5.4 | BSD-3-Clause |
| PyTorch | 2.14.1+cpu | BSD-3-Clause |
| torchaudio | 2.11.0+cpu | BSD-2-Clause |
| Demucs | 4.0.1 | MIT |
| onnxruntime | 1.23.2 | MIT |
| transformers | 4.57.1 | Apache-2.0 |
| tokenizers | 0.22.2 | Apache-2.0 |
| rapidfuzz | 3.14.3 | MIT |

Модели и веса имеют собственные лицензии, перечисленные выше. BGE INT8 — производная от MIT-весов, квантование выполнено локально через `onnxruntime.quantization.quantize_dynamic` (QInt8, без калибровки). Лицензия на ONNX-конверсию BGE остаётся MIT.

### 2.3. Треки

| ID | Роль | Источник и лицензия | Длительность | Формат | SHA-256 |
|---|---|---|---:|---|---|
| `speech` | речь | Robert Frost, «Birches», LibriVox Short Poetry Collection 102; public domain (LibriVox + текст Wikisource) | 214.883 с (3:35) | MP3 128 kbps, mono 44.1 kHz | `98047fdbe41a2db272bed6033b80e51a7a10307bac6a44f3fb300aecbba54f9b` |
| `music` | музыка с вокалом | CG5, «Fish Out Of Water», Wikimedia Commons, **CC BY 3.0**; требуется attribution | 245.352 с (4:05) | OGG Vorbis 112 kbps, stereo 48 kHz | `00294d20478a2bdf4931f8bddca09d2e3d1bda71e715c7fb5778301d2ca1309b` |
| `nightcore` | derivative | `music`, ускорение и повышение питча на 25% (`asetrate=48000*1.25,aresample=48000`), **CC BY 3.0** | 196.260 с (3:16) | OGG Vorbis 153 kbps, stereo 48 kHz | `e6e30e04094f0feb630677e8c40d4199cf76e22db36b55bd3acd871186ebc885` |

Nightcore — не отдельная запись, а честно помеченный derivative того же трека: так проверялись pitch/time robustness и Demucs на ускоренном материале. Речь — стихотворение, а не спонтанная речь; WER на нём нельзя считать оценкой для подкастов/интервью.

## 3. Whisper

### 3.1. Методика

- Один процесс на конфигурацию, `device=cpu`, `compute_type=int8`, `num_workers=1`, `beam_size=5`, `best_of=5`, `word_timestamps=true`, `condition_on_previous_text=true`, язык — auto-detect.
- `batch=1`: в `faster-whisper 1.2.1` у `WhisperModel.transcribe` нет параметра batch; `BatchedInferencePipeline` не использовался, чтобы не менять семантику word timestamps.
- В таблице `Wall` — время `model.transcribe` (включая decode и word alignment), без загрузки модели. Загрузка small ≈1.1 с, medium ≈3.0–3.5 с. `/usr/bin/time -v` использовался для внешнего peak RSS; в JSON — `ru_maxrss`.
- RTF = длительность аудио / `Wall`. Для VAD-on на музыке RTF несопоставим: VAD вырезал почти всё, см. отдельную таблицу.
- WER: нормализация (lowercase, пунктуация, дефисы, апострофы), Levenshtein по словам. Для речи — полный текст стихотворения, 510 слов после нормализации. Для музыки — первый уникальный блок verse 1 + pre-chorus + chorus (97 слов), чтобы повторяющиеся припевы не искажали WER.

### 3.2. Основные прогоны

| Track | Model | Threads | Batch | VAD | Audio, s | Wall, s | RTF | Peak RSS, MiB | Lang | WER | Quality notes |
|---|---|---:|---:|---|---:|---:|---:|---:|---|---:|---|
| speech | small | 8 | 1 | on | 214.9 | 57.43 | 3.74 | 991 | en 1.00 | 3.14% | 510 слов, ошибки в «boy's been» → «boys when», «right» → «write»; пунктуация и строки хорошие |
| speech | small | 8 | 1 | off | 214.9 | 57.31 | 3.75 | 950 | en 1.00 | 2.75% | Чуть лучше WER, но добавлены intro/outro; VAD не обязателен для clean speech |
| speech | small | 12 | 1 | on | 214.9 | 69.87 | 3.08 | 1011 | en 1.00 | 3.14% | 12 threads **медленнее** 8 на этой VM |
| speech | small | 8 | 1 | on | 214.9 | 55.10 | 3.90 | 987 | en 1.00 | 3.14% | Без word timestamps: 55.10 с, RTF 3.90; alignment добавляет ~4% времени и не является причиной отставания |
| speech | medium | 8 | 1 | on | 214.9 | 146.73 / 147.09 | 1.46 | 2124 / 2063 | en 1.00 | 2.75% | Два прогона, стабильно; 5 substitutions, 1 deletion, 8 insertions |
| speech | medium | 12 | 1 | on | 214.9 | 176.42 | 1.22 | 1955 | en 1.00 | 2.75% | 12 threads снова медленнее 8 |
| music | small | 8 | 1 | off | 245.4 | 53.23 | 4.61 | 884 | en 0.57 | 22.68% | Основной смысл и припев читаются; ошибки в «fledged», «Carry on» и отдельных строках |
| music | medium | 8 | 1 | off | 245.4 | 155.26 | 1.58 | 1968 | en 0.82 | 14.43% | Заметно лучше small на уникальном блоке; language probability выше |
| nightcore | small | 8 | 1 | off | 196.3 | 48.47 | 4.05 | 950 | en 0.53 | 35.05% | Pitch/time ломают слова: «fully blessed friend», «you can get what you want» |
| nightcore | medium | 8 | 1 | off | 196.3 | 117.60 | 1.67 | 2009 | en 0.82 | 25.77% | Лучше small, но всё ещё много замен; language probability 0.82 |

Thread scaling для small на речи (8 threads — оптимум):

| Threads | Wall, s | RTF | Peak RSS, MiB |
|---:|---:|---:|---:|
| 4 | 89.28 | 2.41 | 992 |
| 6 | 64.77 | 3.32 | 987 |
| 8 | 57.43 | 3.74 | 991 |
| 12 | 69.87 | 3.08 | 1011 |

### 3.3. VAD on/off на музыке

| Track | VAD | Audio, s | Обработано после VAD, s | Wall, s | RTF по полному аудио | Peak RSS, MiB | Words | Результат |
|---|---|---:|---:|---:|---:|---:|---:|---|
| music | on | 245.4 | 15.4 | 10.22 | 24.0 (не сопоставим) | 784 | 25 | VAD отбросил почти весь вокал; фрагменты «This little woo…» |
| music | off | 245.4 | 245.4 | 53.23 | 4.61 | 884 | 291 | Полный, связный транскрипт |
| nightcore | on | 196.3 | 9.9 | 9.26 | 21.2 (не сопоставим) | 768 | 17 | VAD оставил 9.9 с, дальше мусор |
| nightcore | off | 196.3 | 196.3 | 48.47 | 4.05 | 950 | 298 | Полный транскрипт, но с ошибками pitch |

**Вывод:** для sung audio `vad_filter` в текущем виде непригоден: он режет вокал и создаёт ложные «быстрые» RTF. Нужен content-aware режим: VAD on для речи/подкастов, VAD off для музыки; решение — Plan 16.

### 3.4. Качество ASR

- Речь: small 3.14%, medium 2.75% WER. Оба варианта пригодны для lyrics/timing; medium даёт минимальный прирост, не оправдывающий 2.5× времени.
- Музыка: small 22.68%, medium 14.43% WER на уникальном блоке. Для plain lyrics этого мало; для языка/идентичности и чернового LRC — приемлемо с warning.
- Nightcore: small 35.05%, medium 25.77%. Для текста использовать оригинальные lyrics, ASR — только для таймингов/языка.
- Полный WER по всем 353 словам lyrics (с повторами) даёт 38.0% / 59.5% / 51.0% / 41.9% для music mix / music vocals / nightcore mix / nightcore vocals. Эти числа сильно зависят от пропущенных повторов и не должны быть основным quality gate; они приведены для полноты.

## 4. Demucs

### 4.1. Методика

- `htdemucs`, PyTorch CPU, 8 threads (`OMP_NUM_THREADS=8`, `torch.set_num_threads(8)`), `-j 1`, `segment=7.8 с` (модель `Fraction(39,5)`), `overlap=0.25`, 4 стема.
- Wall включает загрузку модели и сохранение WAV; чистое разделение по progress bar — ≈4:06 для music и ≈3:19 для nightcore. RTF в таблице посчитан по wall, поэтому он чуть оптимистичнее «чистого» RTF.
- Пик RSS — `/usr/bin/time -v`. Качество — количественные прокси: RMS/peak стемов, vocal energy ratio, SI-SDR суммы стемов к микс-дауну, ASR на vocals/instrumental. Субъективного прослушивания не было.

### 4.2. Результаты

| Track | Model | Segment | Overlap | Jobs | Wall, s | RTF | Peak RSS, MiB | Quality notes |
|---|---|---|---:|---:|---:|---:|---:|---|
| music | htdemucs | 7.8 с | 0.25 | 1 | 254.79 | 0.96 | 2126 | Vocal energy 28.9%, vocals RMS −17.6 dBFS, peak −0.09 dBFS; сумма стемов SI-SDR 20.9 дБ |
| nightcore | htdemucs | 7.8 с | 0.25 | 1 | 207.75 | 0.95 | 1883 | Vocal energy 25.2%, vocals RMS −18.0 dBFS, peak −0.09 dBFS; SI-SDR 24.3 дБ |
| music | htdemucs_ft | — | — | — | не запускался | — | — | Optional; по README ≈4× htdemucs, т.е. ~17 мин на 4-мин трек. Для bulk не рассматривается |
| music | ONNX/OpenVINO | — | — | — | не запускался | — | — | Триггер «PyTorch >3× длительности» не наступил; ONNX/OpenVINO deferred |

### 4.3. Качество стемов

| Проверка | music | nightcore |
|---|---:|---:|
| Vocal energy ratio | 28.9% | 25.2% |
| Vocals RMS / peak | −17.6 / −0.09 dBFS | −18.0 / −0.09 dBFS |
| Drums RMS | −17.9 dBFS | −17.3 dBFS |
| Bass RMS | −18.4 dBFS | −19.0 dBFS |
| Other RMS | −19.0 dBFS | −18.0 dBFS |
| SI-SDR суммы 4 стемов к исходнику | 20.9 дБ | 24.3 дБ |
| ASR instrumental (small, VAD off) | 45 токенов, только точки; текста нет | не запускался |
| ASR vocals WER (unique block) | 31.96% | 28.87% |

- В instrumental-стеме Whisper не нашёл слов — хороший признак отсутствия утечки вокала (хотя нулевой ASR не доказывает идеальную чистоту).
- Demucs **не универсально улучшает ASR**: на music vocals WER 31.96% против 22.68% на миксе; на nightcore, наоборот, 28.87% против 35.05%. Причина, вероятно, в потере backing vocals/артефактах и в том, что Whisper лучше использует гармонический контекст микса. Маршрутизацию «ASR только по vocals» нельзя включать без A/B.
- Сумма стемов не равна исходнику bit-exact (SI-SDR 20.9/24.3 дБ); для lossless-реконструкции стемы не годятся.
- Субъективная оценка «на слух» не выполнялась; выводы ограничены метриками и ASR-прокси.

## 5. Cross-encoder

### 5.1. Набор

- 25 запросов × 4 кандидата = **100 пар** title/artist.
- Состав: 17 exact (12 EN + 5 RU), 3 version (instrumental/nightcore/remix), 2 cover, 3 translit/script-mismatch.
- Языки: 17 EN, 5 RU, 3 mixed. Запросы содержат реальный uploader-шум: `(Official Video)`, `(Remastered)`, `feat.`, `[Radio Edit]`, русские `(Официальный клип)`.
- В каждой группе ровно один gold; кандидаты перемешаны фиксированным seed. Метрики: top-1, top-3, MRR.
- Cover-изображения не ранжировались: text cross-encoder не оценивает качество/watermark картинки. Для cover ranking нужен отдельный provider-priority/vision-подход; это ограничение, а не результат.

### 5.2. Результаты

| Model | Precision | Threads | Batch | Pairs | Latency, ms/100 | Pairs/s | Peak RSS, MiB | Top-1 | Top-3 | MRR |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| lexical `token_set_ratio` (0.7 title / 0.3 artist) | float | 1 | — | 100 | 1.49 | 67 102 | 76 | 88% | 100% | 0.933 |
| MiniLM L-6 | QInt8 | 12 | 8 | 100 | 329.3 | 303.7 | 129 | 96% | 100% | 0.973 |
| BGE base (XLM-R) | INT8 dynamic | 12 | 8 | 100 | 1 547.2 | 64.6 | 820 | 100% | 100% | 1.000 |

- Латентность включает tokenization + ONNX inference, без загрузки модели. Загрузка: MiniLM 0.17 с, BGE 1.74 с.
- При 8 threads: MiniLM 432.7 мс/100, BGE 2 154.6 мс/100. Для BGE 12 threads обязательны, чтобы уложиться в ≤2 с; 16 threads дают 1 915.3 мс/100, 12 — оптимум.
- MiniLM — английский BERT (`bert-base-uncased`-подобный tokenizer); BGE base — XLM-R (`xlm-roberta-base`), поэтому BGE лучше держит кириллицу/translit.
- По языкам: lexical — 100% EN, 100% RU, 0% mixed; MiniLM — 100% EN, 100% RU, 66.7% mixed; BGE — 100% во всех трёх.
- По типам: lexical и MiniLM дают 100% top-1 на exact/version/cover; на translit lexical 0%, MiniLM 66.7%, BGE 100%.

### 5.3. Статистическая оговорка

- Top-3 = 100% у всех трёх, поэтому формальный критерий «top-3 > lexical» **не выполнен**: потолок на 4 кандидатах, top-3 слишком легко.
- Top-1: MiniLM +8 п.п. к lexical (2 запроса), BGE +12 п.п. (3 запроса). Paired bootstrap 95% CI: MiniLM [0, +20] п.п., BGE [0, +24] п.п.; McNemar exact p=0.50 и p=0.25 соответственно. На 25 запросах это **не доказанный** прирост.
- MiniLM ошибся на `tr01` (`Nas Ne Dogonyat` → gold на 3-м месте); lexical ошибся на всех трёх translit-запросах. BGE не ошибся ни разу, но это может быть эффект маленького набора и известных названий.
- Для решения D8 нужен больший набор (50–100 запросов, 200–400 пар) и RU/EN holdout; текущий результат — сигнал, не окончательная калибровка.

## 6. Сравнение с оценками docs

| Пункт | Оценка docs | Факт | Отклонение и причина |
|---|---|---|---|
| Whisper small RTF | 6–12× | 3.74× речь, 4.05–4.61× музыка/nightcore | В 1.6–3.2× медленнее; CPU без AVX/AVX2, QEMU, виртуализация. Word timestamps добавляют ~4% времени, не являются причиной отставания |
| Whisper medium RTF | 2–3× | 1.46–1.67× | В 1.2–2× медленнее; та же причина. Речь на границе no-go 1.5× |
| Whisper small RSS | 1.5–2 ГБ | 0.95–1.01 ГиБ | Ниже оценки; CTranslate2 int8 + короткие сегменты |
| Whisper medium RSS | 3–4 ГБ | 1.96–2.12 ГиБ | Ниже оценки |
| Demucs RTF | ~1.5× | 0.95–0.96× (wall) | Лучше оценки; PyTorch 2.14 CPU + 18 vCPU, но только 2 трека. Не экстраполировать на 500 треков без проверки |
| Demucs RSS | 3–7 ГБ | 1.84–2.08 ГиБ | Ниже оценки |
| MiniLM 100 pairs | 0.6–1.2 с | 0.33 с (12 threads), 0.43 с (8) | Быстрее; CPU/ONNX QInt8 |
| MiniLM RSS | <0.5 ГБ | 129 МиБ | Ниже оценки |
| BGE base 100 pairs | 3–6 с | 1.55 с (12 threads), 2.15 с (8) | Быстрее; INT8 dynamic, XLM-R |
| BGE base RSS | ~1 ГБ | 820 МиБ | В пределах оценки |

Главное отклонение — Whisper: оценки docs получены на i7-12700K с AVX2, а здесь CPU вообще без AVX. Поэтому нельзя делать вывод «faster-whisper плох»; корректный вывод — «на этом конкретном CPU target 5× не достигается».

## 7. Go/no-go

| Spike | Критерий | Факт | Решение |
|---|---|---|---|
| Whisper small | RTF ≥5× | 3.74× речь, 4.05–4.61× музыка/nightcore; RSS ~1.0 ГиБ | **Conditional go**: выше жёсткого no-go 3×, ниже цели 5×. Оставить default для `full`, заложить ~4× и warning о CPU |
| Whisper medium | RTF ≥2×, RAM ≤6 ГБ | 1.46–1.67×, RSS 2.1 ГиБ; речь 1.46× < 1.5× | **No-go как default**. Opt-in для избранных треков; качество выше, но 2.5–3× дороже. Если продукту нужен medium по умолчанию — только на CPU с AVX2 и после повторного замера |
| Demucs PyTorch | ≤2× duration, RAM ≤8 ГБ | 0.95–0.96×, RSS 2.08 ГиБ | **Go (opt-in)**: только `-j 1`, не для плейлистов; 4 стема; `htdemucs_ft` не включать |
| Demucs ONNX/OpenVINO | ≥3× ускорение, RAM ≤6 ГБ | Не проверялся: PyTorch <3× duration | **Deferred / not triggered**. Следующий spike имеет смысл только на слабом CPU или для batch-обработки |
| Cross-encoder | top-3 > lexical, ≤2 с/100 | Top-3 100% = 100% = 100%; top-1 88/96/100%; BGE 1.55 с/100, MiniLM 0.33 с/100 | **Conditional go как optional**: lexical остаётся default; BGE — лучший кандидат для RU/translit, MiniLM — для EN/низкой памяти. Формальный top-3-критерий не доказан из-за потолка |
| ASR quality | Приемлемо для lyrics/языка | Речь 3.1% / 2.75% WER; музыка 22.7% / 14.4%; nightcore 35.1% / 25.8% | Для речи — да; для музыки/nightcore — только язык/тайминг с warning и оригинальными lyrics |
| Demucs quality | Пригодно для vocal/instrumental | Instrumental без текста; vocal WER не всегда лучше микса | **Go только как opt-in фича**, не как безусловный улучшитель ASR |

## 8. Рекомендации для config

Все ID — значения конфигурации, не хардкод в коде. Имена ключей ниже ориентировочные: точную схему `config.toml` определяют Plan 14/16/17.

| Секция | Рекомендация | Обоснование |
|---|---|---|
| `[models.asr]` | `Systran/faster-whisper-small`, `compute_type=int8`, `threads=8`, `beam_size=5`, `word_timestamps=true` | Лучший компромисс качество/время/память; 8 threads быстрее 12 на этой VM |
| `[models.asr].vad` | `on` для речи/подкастов, `off` для sung music; в идеале `auto` по `ContentKind` | VAD на музыке вырезал 94% аудио и дал мусор |
| `[models.asr].medium` | Не default; разрешить как opt-in для избранных треков | 2.5–3× медленнее, +8 п.п. WER на музыке, +0.4 п.п. на речи |
| `[analysis].demucs` | `htdemucs`, `jobs=1`, `segment=7.8`, `overlap=0.25`, 4 стема, `threads=8` | 0.95–0.96× duration, 2.1 ГиБ; ONNX не нужен |
| `[analysis].demucs_ft` | `false` / не предлагать в bulk | ≈4× htdemucs, не для плейлистов |
| `[models.ranker]` | lexical default; optional `BAAI/bge-reranker-base` INT8 ONNX, `threads=12`, `batch=8`, `max_length=256` | 1.55 с/100, 820 МиБ, 100% top-1/top-3 на наборе; XLM-R держит RU/translit |
| `[models.ranker].alternative` | `temsa/ms-marco-MiniLM-L-6-v2-onnx-cpu-qint8`, QInt8, `threads=12`, `batch=8` | 0.33 с/100, 129 МиБ; 96% top-1; хуже на translit |
| `[ranker].strategy` | `lexical`, пока optional-модель не скачана; `hybrid` — только если cross-encoder реально включён | Не делать cross-encoder обязательной зависимостью v1.0 |
| `[models].max_heavy_models` | `1` | Даже с 21 ГиБ: medium + Demucs + ranker одновременно не нужны и повышают риск OOM на 16 ГБ |
| `[models].offline` | `true` по умолчанию; скачивание — только явной командой | Воспроизводимость, checksum, лицензии |

Практическая рекомендация для D8: **lexical baseline + optional BGE base INT8** для multilingual/RU; MiniLM QInt8 — если важны минимальный размер и скорость на EN. Это не делает cross-encoder обязательным для v1.0. Формулировка docs «MiniLM multilingual QInt8» требует отдельной multilingual MiniLM-модели; протестированный `temsa/ms-marco-MiniLM-L-6-v2` — английский BERT, поэтому для RU/translit он слабее BGE.

## 9. Ограничения

- Один CPU-класс (QEMU без AVX) и один запуск на конфигурацию, кроме medium/speech (два прогона). Разброс RTF между повторами ≤1%; межтрековый разброс больше.
- Один speech-трек (поэзия), один music-трек и его derivative. Нет подкастов, интервью, рэпа, металла, классики, других языков.
- WER на поэзии и sung lyrics ограничен: повторы, backing vocals, артикли и пунктуация. Music WER — только уникальный блок, не весь трек.
- Demucs quality — без human listening; выводы по RMS/SI-SDR/ASR. Субъективные артефакты (phasiness, bleed) могли остаться незамеченными.
- Cross-encoder — 25 запросов/100 пар, потолок top-3, широкие доверительные интервалы; cover-image ranking не проверялся.
- `htdemucs_ft`, ONNX/OpenVINO Demucs, GPU/CUDA, local LLM не запускались (non-goals / deferred).
- Модели, скрипты и JSON лежат в `.spike-private/bench` (git-ignored), не в `~/.cache`, потому что sandbox не разрешал запись в `~/.cache`; временные venv и stems удалены, в git они не попадают.

## 10. Воспроизводимость

Скрипты и сырые JSON лежат в `.spike-private/bench/` (не коммитятся):

```text
bench_whisper.py                 # один прогон faster-whisper + JSON
run_demucs.py                    # Demucs CLI + WAV save через soundfile
run_lexical.py                   # lexical baseline
run_ranker.py                    # ONNX cross-encoder
build_ranker_dataset.py          # 25×4 датасет
score_wer.py                     # WER речи
score_lyrics_unique.py           # WER уникального блока музыки
analyze_stems.py                 # RMS/SI-SDR стемов
compare_rankers.py               # top-k, McNemar, bootstrap
results/                         # JSON/CSV-подобные результаты и time -v
```

Примеры команд:

```bash
# Whisper
python bench_whisper.py --model models/faster-whisper-small \
  --audio audio/birches.mp3 --track speech --model-id Systran/faster-whisper-small \
  --threads 8 --vad on --beam 5 --output results/small_birches_t8_vadon.json

# Demucs
OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 python run_demucs.py -n htdemucs -j 1 \
  --overlap 0.25 --out results/stems_htdemucs audio/music.ogg

# Ranker
python run_ranker.py --name 'BAAI/bge-reranker-base (ONNX int8 dynamic)' \
  --model models/bge-reranker-base/onnx/model-int8.onnx \
  --tokenizer models/bge-reranker-base --precision INT8 --threads 12 \
  --batch-size 8 --output results/ranker_bge_t12.json
```

Финальная проверка: `git status --short` не показывает новых изменений в production-путях; отчёт не содержит плейсхолдеров и шаблонных маркеров. Pre-existing dirty worktree (изменения, бывшие до spike) не трогался и не откатывался.

## 11. Plan 17 follow-up

Implementation handoff: [`17-demucs-handoff.md`](17-demucs-handoff.md). The
production Demucs observer reproduced the go result on the `music` track:
245.4 s audio in 247.5 s wall (RTF 1.009), peak RSS 2217.7 MB, vocals ratio
26.1%, `-j 1`. Slow benchmark JSON:
[`17-demucs-benchmark.json`](17-demucs-benchmark.json).
