# 14. Phase 0 spikes — оценка по интернету

> Дата: 2026-10-22. Оценка только по опубликованным бенчмаркам/документации; локально ничего не запускалось.
> Допущения по железу: **18 ядер, 16–18 ГБ RAM, CPU-only, Linux**. Точная модель CPU, AVX-512 и пропускная способность памяти неизвестны — это главный источник погрешности.

## 1. Короткий вывод

| Spike | Вердикт | Ожидаемая стоимость | Уверенность |
|---|---|---|---|
| yt-dlp + Deno + cookies | **High risk, но feasible** | сеть; 1–2 дня интеграции | Средняя |
| ffprobe + mutagen round-trip | **Low risk** | <1 с/файл; 1–2 дня тестов | Высокая |
| MusicBrainz + AcoustID | **Feasible, rate-limited** | 100 треков: ~3–7 мин; 500: ~17–40 мин | Высокая |
| faster-whisper small/medium | **Feasible** | 3.5-мин трек: ~20–110 с; 1.5–4 ГБ | Высокая |
| Demucs (PyTorch) | **Тяжело, но возможно** | 3.5-мин трек: ~4–6 мин; 3–7 ГБ | Средняя |
| Demucs ONNX/OpenVINO | **Перспективно, но spike** | 3.5-мин трек: ~30–60 с; 1–4 ГБ | Средняя |
| Cross-encoder | **Feasible** | 100 кандидатов: ~0.5–2 с (MiniLM); 5–15 с (BGE) | Высокая |
| LLM API | **Feasible, cost-dependent** | ~$0.005–0.25/трек; 2–10 с/вызов | Средняя |
| Local LLM 0.5B (optional fallback) | **Не планируется как основной; API — primary** | ~1–2 ГБ; скорость замерить | Средняя |
| SQLite/migrations/file lock | **Low risk** | секунды; 1 день тестов | Высокая |

Главный вывод: **16–18 ГБ хватает, если тяжёлые модели не работают одновременно**. Whisper medium + Demucs + локальный fallback 0.5B в одном процессе — всё ещё риск OOM. Нужен `ModelManager` с выгрузкой и последовательным запуском.

## 2. Что именно считается «spike»

Spike — это не «написать модуль», а проверить:

1. Работает ли технология на целевом железе.
2. Сколько времени/RAM/денег она реально стоит.
3. Достаточно ли качества на наших данных.
4. Есть ли go/no-go для архитектуры.

## 3. Оценки по spike'ам

### 3.1. yt-dlp + Deno + cookies

**Что говорит интернет**

- Для YouTube `yt-dlp` теперь требует внешний JS runtime: Deno (рекомендован, включён по умолчанию), Node 22+, QuickJS/QuickJS-NG. Deno минимум **2.3.0**; Node минимум **22.0.0**; старые QuickJS могут исполнять скрипты минутами. ([yt-dlp EJS wiki](https://github.com/yt-dlp/yt-Dlp/wiki/EJS))
- PyPI-пакету нужен `yt-dlp-ejs`; проще ставить `yt-dlp[default]`, иначе EJS-скриптов не будет. Версия `yt-dlp-ejs` должна соответствовать версии `yt-dlp`. ([yt-dlp EJS wiki](https://github.com/yt-dlp/yt-Dlp/wiki/EJS))
- Deno запускает EJS в restricted permissions (без FS/network), это хороший security-дефолт. ([yt-dlp EJS wiki](https://github.com/yt-dlp/yt-Dlp/wiki/EJS))
- Cookies/PO tokens и bot-check остаются операционным риском; успешность зависит от сайта, региона, возраста аккаунта и текущей политики YouTube. Это **не оценивается по интернету** для твоих конкретных ссылок.

**Оценка**

- CPU/RAM: пренебрежимо по сравнению с сетью; Deno-процесс — десятки-сотни МБ.
- Время: определяется сетью и площадкой; 3–5-мин аудио — обычно секунды-десятки секунд при нормальном канале.
- Интеграция: 1–2 дня на subprocess-адаптер, cookies, error mapping, progress.
- Риск: **высокий** — внешняя зависимость ломается чаще всех остальных.

**Go/no-go**

- 4 из 5 тестовых URL (YouTube, YouTube Music, SoundCloud, Bandcamp, обычный файл) дают metadata + audio.
- Ошибки login/rate-limit/DRM/geo понятны и не текут cookies.
- `doctor` проверяет Deno/yt-dlp/ejs/cookies.

**Что мерить локально**

- Success rate на 5–10 своих ссылках.
- Нужны ли cookies/PO token для твоих видео.
- Скорость скачивания и размер файлов.
- Поведение при 429/403/«Sign in to confirm you're not a bot».

### 3.2. ffprobe + mutagen round-trip

**Что говорит интернет**

- `mutagen` поддерживает ASF, FLAC, MP4, Monkey's Audio, MP3, Musepack, Ogg Opus, Ogg FLAC, Ogg Speex, Ogg Theora, Ogg Vorbis, True Audio, WavPack, OptimFROG, AIFF; все версии ID3v2, все стандартные ID3v2.4 frames. ([mutagen docs](https://mutagen.readthedocs.io/en/latest/index.html))
- Freeform keys, multi-value, Unicode поддерживаются; это важно для MBID/кастомных полей. ([mutagen docs](https://mutagen.readthedocs.io/en/latest/index.html))
- Лицензия mutagen — GPL-2.0-or-later; это уже учтено в license boundary. ([mutagen docs](https://mutagen.readthedocs.io/en/latest/index.html))

**Оценка**

- Время: ffprobe/запись тегов — обычно <1 с на файл; обложка/lyrics — зависит от размера.
- RAM: десятки МБ.
- Основная стоимость — **корректность**, не производительность: ID3v2.3 vs 2.4, MP4 multi-value, Ogg picture base64, lyrics embedding, read-back.
- Тесты: 1–2 дня на матрицу форматов и полей.

**Go/no-go**

- MP3/M4A/FLAC/OGG/Opus: title/artist/album/date/track/genre/BPM/key/cover читаются обратно.
- Оригинал не изменён; sidecar/read-back работают.
- ID3v2.4 и 2.3 проходят; MP4 freeform MBID читается.

**Что мерить локально**

- Поведение в твоих плеерах (Navidrome/foobar/VLC/Apple Music).
- Реальные файлы с «грязными» тегами.
- Размер обложки/lyrics и лимиты.

### 3.3. MusicBrainz + AcoustID

**Что говорит интернет**

- MusicBrainz: в среднем **1 запрос/сек на IP**, иначе 503; нужен осмысленный User-Agent с контактом; кэшировать ответы; при высоком объёме — локальная репликация. ([MusicBrainz rate limiting](https://wiki.musicbrainz.org/MusicBrainz_API/Rate_Limiting))
- AcoustID: **не более 3 запросов/сек**, бесплатно только для некоммерческого использования; нужен client API key. ([AcoustID web service](https://acoustid.org/webservice))
- AcoustID lookup по fingerprint; fingerprint считается локально (`fpcalc`/Chromaprint), это быстро.

**Оценка**

- 100 треков, 2 запроса MB/трек: ~200 с (3.3 мин) минимум, без учёта retry.
- 500 треков, 2 запроса MB/трек: ~1000 с (16.7 мин).
- 500 треков, 5 запросов MB/трек: ~2500 с (41.7 мин).
- AcoustID: 500 треков по 1 запросу при 3 req/s — ~167 с (2.8 мин).
- Кэш/батчинг/локальная репликация сокращают; но 1 req/s — жёсткий потолок.
- CPU/RAM: низкие; `fpcalc` — секунды.

**Go/no-go**

- Match rate на 20–30 своих треках достаточен (например, >50–60% с учётом derivative).
- Нет 503-шторма; Retry-After соблюдается.
- Кэш реально спасает повторные запросы.
- AcoustID используется только в личном/некоммерческом режиме.

**Что мерить локально**

- Match rate на твоей библиотеке (особенно nightcore/remix).
- Среднее число запросов на трек.
- Нужна ли локальная репликация MB при 10k+.

### 3.4. faster-whisper (CPU)

**Что говорит интернет**

- Официальный бенчмарк faster-whisper: **small, int8, 8 threads, i7-12700K**, 13 минут аудио — **1m42s**, **1477 MB RAM**; RTF ≈ **7.6x**. С `batch_size=8`: **51s**, **3608 MB**, RTF ≈ **15.3x**. ([faster-whisper README](https://github.com/SYSTRAN/faster-whisper))
- Масштабирование по размерам моделей (оценка, не измерение): medium ≈ в 3.15 раза медленнее small; large ≈ в 6.35 раза, то есть примерно real-time на 8 threads. ([dev.to derivation](https://dev.to/multigrid/how-slow-whisper-is-on-cpu-only-derived-iac))
- Больше потоков не всегда лучше: decoder упирается в memory bandwidth; 8–12 потоков часто оптимум, 18 — не гарантия. ([dev.to derivation](https://dev.to/multigrid/how-slow-whisper-is-on-cpu-only-derived-iac))

**Оценка для 3.5-мин трека (210 с)**

| Модель | Оценка RTF | Время | RAM |
|---|---:|---:|---:|
| small int8, 8 threads | 7.6x | ~28 с | ~1.5 ГБ |
| small int8, 18 cores (оценка) | 10–15x | ~15–25 с | ~1.5–2 ГБ |
| medium int8 (оценка) | 2.4x | ~85 с | ~3–4 ГБ |
| large (оценка) | ~1x | ~210 с | ~6–8 ГБ |
| small batch 8 | 15.3x | ~14 с | ~3.6 ГБ |

Это **оценки**, а не гарантия: многое зависит от CPU, аудио (музыка/речь/шум) и beam size.

**Go/no-go**

- small: RTF ≥5x на целевом CPU.
- medium: RTF ≥2x, RAM ≤6 ГБ.
- WER/качество на 2–3 треках (речь, музыка, nightcore) приемлемо для lyrics/языка.
- 18 потоков не обязательны; сравнить 8/12/16.

**Что мерить локально**

- RTF на твоём CPU для small/medium.
- RAM peak (RSS) при `batch_size=1/8`.
- WER/качество на твоих жанрах/языках.
- Влияние `vad_filter`, `beam_size`, `condition_on_previous_text`.

### 3.5. Demucs (PyTorch)

**Что говорит интернет**

- Официальный README: на CPU обработка Demucs **примерно равна 1.5× длительности трека**. ([Demucs README](https://github.com/adefossez/demucs))
- `htdemucs_ft` (fine-tuned) — **в 4 раза больше времени**, чем `htdemucs`; для CPU это почти наверняка нежизнеспособно. ([Demucs README](https://github.com/adefossez/demucs))
- `-j` параллелит и умножает RAM; для 16–18 ГБ это опасно. ([Demucs README](https://github.com/adefossez/demucs))

**Оценка для 3.5-мин трека**

- `htdemucs` PyTorch: ~5.25 мин (1.5×), RAM ориентировочно 3–7 ГБ.
- `htdemucs_ft`: ~21 мин, RAM выше; не для bulk.
- 500 треков `htdemucs`: ~44 часа; `htdemucs_ft` — ~175 часов. Это ключевая цифра: **Demucs нельзя включать по умолчанию для плейлистов**.

**Go/no-go**

- PyTorch htdemucs на 3.5-мин треке ≤2× длительности.
- RAM ≤8 ГБ при одном процессе.
- Качество stem ratios достаточно для instrumental/acapella/derivative.
- Иначе — только opt-in для избранных треков.

**Что мерить локально**

- Время/RAM `htdemucs` на 3.5-мин треке.
- Влияние `--segment`, `--overlap`, `-j 1`.
- Качество на nightcore/remix (артефакты, вокал).

### 3.6. Demucs ONNX/OpenVINO (альтернатива)

**Что говорит интернет**

- ONNX INT8 HTDemucs: 7.8-секундный сегмент на **i7-12800H, 4 threads** — **1.67–1.89 с** (ORT) или **1.22 с** (OpenVINO); peak RAM **1.1–3.9 ГБ** в зависимости от варианта. ([htdemucs-int8-memory](https://huggingface.co/itamiArika/htdemucs-int8-memory))
- Для 3.5-мин трека это ~27 сегментов: ~32–51 с + overhead; на 18 ядрах может быть быстрее, но зависит от threading/сегментации.
- Это сторонний experimental-проект; качество INT8 заявлено SDR −0.03 дБ, но требует собственной проверки. ([htdemucs-int8-memory](https://huggingface.co/itamiArika/htdemucs-int8-memory))

**Оценка**

- 3.5-мин трек: ~30–60 с, 1–4 ГБ RAM (при удачной интеграции).
- Интеграция сложнее: сегментация, ONNX runtime, версии моделей, проверка качества.
- Это **spike внутри spike**: сначала PyTorch baseline, потом ONNX, если PyTorch слишком медленный.

**Go/no-go**

- ONNX-вариант даёт ≥3× ускорение без заметной деградации на 5–10 треках.
- RAM ≤6 ГБ, стабильность на длинных аудио.
- Иначе оставляем PyTorch + opt-in.

### 3.7. Cross-encoder / ranker

**Что говорит интернет**

- `ms-marco-MiniLM-L-6-v2` QInt8 ONNX на **i7-9750H (6 cores, AVX2)**: 20 документов — **210 мс** avg, 50 документов — **578 мс**; throughput 86–95 pairs/s. ([HF model card](https://huggingface.co/temsa/ms-marco-MiniLM-L-6-v2-onnx-cpu-qint8))
- `bge-reranker-base` ONNX: ~1.0 с на 3 набора по 5–6 пассажей (≈16 пар), то есть ~60 мс/пара; примерно в 2× быстрее PyTorch. ([HF model card](https://huggingface.co/sampoorna42/bge-reranker-base-onnx))
- Multilingual `bge-reranker-v2-m3` заметно крупнее; на CPU будет медленнее, чем MiniLM. ([HF model card](https://huggingface.co/a-ivanovitch/bge-reranker-v2-m3-onnx))

**Оценка**

- Lexical baseline: <10 мс на 100 кандидатов.
- MiniLM QInt8: 100 кандидатов ~0.6–1.2 с на 6–18 ядрах; RAM <0.5 ГБ.
- BGE base: 100 кандидатов ~3–6 с; RAM ~1 ГБ.
- BGE v2-m3: 100 кандидатов ~10–30 с; для каждого поля — слишком дорого, только top-N.

**Go/no-go**

- MiniLM top-3 на 50–100 парах твоих title/artist вариантов лучше lexical.
- Multilingual качество (ru/en) приемлемо; иначе cloud LLM tie-break.
- 100 кандидатов ≤2 с.

**Что мерить локально**

- Top-1/top-3 на твоих примерах (nightcore, remix, русские названия).
- Латентность на 18 ядрах, RAM.
- Стоимость ONNX vs PyTorch.

### 3.8. LLM API

**Что говорит интернет**

- Провайдеры поддерживают structured outputs/JSON schema, но не все OpenAI-compatible endpoints одинаковы; нужен strict schema + pydantic fallback.
- Актуальные цены на страницы OpenAI/Anthropic/OpenRouter из этой среды получить надёжно не удалось (403/Cloudflare); **точные цены надо проверить вручную**.
- Токенный объём можно оценить: 2–4 вызова на трек, 4–10k input + 0.5–2k output на вызов.

**Оценка стоимости**

Формула:

```text
cost = input_tokens/1e6 * price_in + output_tokens/1e6 * price_out
```

Диапазон для 2–4 вызовов на трек:

| Сценарий | Input | Output | При $0.25/$2 за 1M | При $1/$5 | При $3/$15 |
|---|---:|---:|---:|---:|---:|
| Лёгкий | 8k | 2k | $0.006 | $0.018 | $0.054 |
| Средний | 20k | 4k | $0.013 | $0.040 | $0.120 |
| Тяжёлый | 40k | 8k | $0.026 | $0.080 | $0.240 |

То есть **~$0.006–0.24 за трек** в зависимости от модели/числа вызовов; 1000 треков — **$6–240**. Кэш и сокращение вызовов важнее выбора «самой дешёвой» модели.

**Латентность**

- 2–10 с на вызов; 4 параллельных вызова — 5–20 с на трек; retries/429 увеличивают.
- Для 500 треков даже 10 с/трек = ~1.4 часа чистого времени LLM.

**Go/no-go**

- Field accuracy на 20–30 треках ≥ текущего policy baseline.
- Cost ≤ $0.05/трек (иначе — батчинг/кэш/меньше вызовов).
- Schema failure <5%; fallback работает.
- Prompt injection не ломает output.

**Что мерить локально**

- Токены/стоимость на своих промптах.
- Accuracy по полям (title/artist/album/kind/derivative).
- Латентность/429 у выбранного провайдера.
- JSON-schema support.

### 3.9. Local LLM 0.5B (optional fallback)

**Решение:** локальные 7B/13B **не планируются**. Основной LLM — API; локальный fallback — модель класса **0.5B** (например, Qwen2.5-0.5B-Instruct или SmolLM2-360M-Instruct; конкретная — конфиг), только для classification/ranking/короткого JSON, **не для autopilot и не для генерации lyrics**.

**Что говорит интернет**

- 0.5B-класс — это ~0.3–0.5 ГБ весов в 4-bit; runtime/контекст добавят ещё ~0.5–1 ГБ. Точная скорость на 18 ядрах по интернету не оценивается.
- Качество 0.5B на русском/музыкальных описаниях будет заметно ниже cloud; это fallback «выбрать из готовых вариантов», а не «придумать метаданные».
- 7B/13B не рассматриваем: они занимают 7–14 ГБ и конфликтуют с Whisper/Demucs по памяти.

**Оценка**

- RAM: ~1–2 ГБ peak.
- Скорость: замерить локально; для коротких промптов (классификация/выбор из 5–20 вариантов) должна быть приемлемой.
- Phase 0 spike **не обязателен**: fallback можно добавить после MVP, если API недоступен.

**Go/no-go (если включать)**

- На 20 примерах accuracy не хуже lexical/ranker fallback.
- RAM ≤2 ГБ, не мешает Whisper/Demucs.
- Модель лицензионно совместима (Apache/MIT) и конфигурируема.

### 3.10. SQLite / миграции / file lock

**Что говорит интернет**

- SQLite WAL + busy_timeout + один writer — стандартная схема; для локального приложения достаточно. Это низкорисковый spike, интернет-бенчмарки не нужны.

**Оценка**

- Время: секунды; тесты 1 день.
- RAM/CPU: пренебрежимо.
- Риски: миграции, конкурентные процессы, crash recovery.

**Go/no-go**

- Два процесса не портят каталог (file lock).
- Падение между шагами → resume.
- Миграция с пустой/старой схемы.

## 4. Ресурсный бюджет на 16–18 ГБ

| Режим | Что в памяти | Оценка peak |
|---|---|---:|
| balanced | Python + ffmpeg + cross-encoder MiniLM | ~3–6 ГБ |
| full + Whisper small | + small int8 | ~5–8 ГБ |
| full + Whisper medium | + medium | ~8–12 ГБ |
| full + Demucs PyTorch | + htdemucs | ~10–16 ГБ |
| full + Whisper medium + Demucs | одновременно | **риск OOM** |
| local LLM 0.5B (optional) | + ~1–2 ГБ | ~8–12 ГБ |
| 0.5B + Demucs | одновременно | лучше не запускать; ModelManager всё равно serial |

Правила:

- Тяжёлые модели — **последовательно**, не параллельно.
- `ModelManager` выгружает предыдущую модель перед загрузкой следующей.
- Whisper threads: 8–12, не 18; память важнее.
- Demucs `-j 1`; `-j 2` удваивает RAM.
- 0.5B fallback лёгкий, но всё равно проходит через `ModelManager`.
- При 16 ГБ оставить 2–3 ГБ запаса на OS/ffmpeg/кэши.

## 5. Что интернет не может оценить

Это надо мерить локально (Phase 0 execution):

1. **Точная модель CPU / AVX-512 / memory bandwidth** — главный фактор RTF.
2. **Реальный success rate yt-dlp** на твоих ссылках, cookies, PO tokens, регионе.
3. **Реальный match rate MusicBrainz/AcoustID** на твоей библиотеке (особенно nightcore/remix).
4. **WER/качество Whisper** на твоих жанрах, языках, nightcore.
5. **Время/RAM Demucs** на твоём CPU и версии PyTorch.
6. **Качество ONNX/OpenVINO Demucs** на твоих треках.
7. **Качество cross-encoder** на русских/смешанных названиях.
8. **Точные цены LLM** у выбранного провайдера (страницы блокируются; проверить вручную).
9. **Token usage/accuracy** твоих промптов; скорость/качество 0.5B fallback, если включишь.
10. **Tag round-trip** в твоих плеерах/устройствах.
11. **Диск/IO** и поведение на 10k библиотеке.
12. **Cookies/Deno установка** на твоём дистрибутиве.
13. **Юридические/ToS** нюансы конкретных сайтов.
14. **Поведение при 429/403/бот-чеке** и восстановление.

## 6. Рекомендуемый порядок Phase 0

1. **Tag round-trip** (1–2 дня) — фундамент, низкий риск, быстро.
2. **SQLite/resume/file lock** (1 день) — фундамент, низкий риск.
3. **Cross-encoder** (1 день) — локально, быстро, влияет на resolver.
4. **faster-whisper small/medium** (1–2 дня) — главный CPU-бенчмарк.
5. **Demucs PyTorch vs ONNX** (2–3 дня) — главный риск по времени.
6. **MusicBrainz/AcoustID** (1–2 дня) — rate limit/match rate.
7. **yt-dlp/Deno/cookies** (2–3 дня) — главный операционный риск.
8. **LLM API** (1–2 дня) — стоимость/качество/схема.
9. **Local LLM 0.5B** — **не входит в Phase 0**; опциональный fallback после MVP.

Почему не начинать с yt-dlp: он самый хрупкий, но его результат не меняет архитектуру; tag/storage/ranker — фундамент. Если yt-dlp не заведётся, MVP всё равно возможен на локальных файлах.

## 7. Go/no-go для Phase 0

| Spike | Go | No-go |
|---|---|---|
| yt-dlp | 4/5 URL, понятные ошибки, cookies не текут | <3/5, постоянные bot-check/403 |
| tag round-trip | 5 форматов, core fields + cover | хотя бы один формат ломает read-back |
| MB/AcoustID | match rate достаточен, нет 503-шторма | match rate слишком низкий/лимиты не держатся |
| Whisper small | RTF ≥5x | RTF <3x или RAM >6 ГБ |
| Whisper medium | RTF ≥2x, RAM ≤6 ГБ | RTF <1.5x |
| Demucs | ≤2x duration, RAM ≤8 ГБ | >3x duration или OOM |
| Cross-encoder | top-3 лучше lexical, ≤2 с/100 | нет выигрыша или >5 с |
| LLM API | accuracy ↑, cost ≤$0.05/трек | cost >$0.10 или schema failures |
| Local LLM 0.5B | опционально: accuracy не хуже lexical/ranker, RAM ≤2 ГБ | не лучше fallback или RAM >3 ГБ |
| SQLite | resume/lock без corruption | corruption/lock fail |

## 8. Итоговая рекомендация

- **16–18 ГБ достаточно** для `balanced` и `full` с одной тяжёлой моделью за раз.
- **Whisper small** — безопасный default для `full`; medium — опция для избранных треков.
- **Demucs PyTorch** — только opt-in, не для плейлистов; ONNX/OpenVINO — отдельный spike, потенциально 3–5× быстрее.
- **Cross-encoder MiniLM QInt8** — хороший дефолт; BGE — только top-N/если качество критично.
- **LLM API** — основной инструмент качества; local LLM 0.5B — только опциональный fallback.
- **yt-dlp** — самый хрупкий; нужен `doctor`, cookies, Deno, понятные ошибки.
- **MusicBrainz** — 1 req/s; для 500 треков планировать 17–40 минут и кэш.

## 9. Источники

- [faster-whisper README (CPU benchmark)](https://github.com/SYSTRAN/faster-whisper)
- [How Slow Whisper Is on CPU Only, Derived](https://dev.to/multigrid/how-slow-whisper-is-on-cpu-only-derived-iac)
- [Demucs README](https://github.com/adefossez/demucs)
- [HTDemucs ONNX INT8 memory/latency](https://huggingface.co/itamiArika/htdemucs-int8-memory)
- [MS MARCO MiniLM-L6-v2 ONNX QInt8 benchmark](https://huggingface.co/temsa/ms-marco-MiniLM-L-6-v2-onnx-cpu-qint8)
- [BGE Reranker Base ONNX](https://huggingface.co/sampoorna42/bge-reranker-base-onnx)
- [yt-dlp EJS setup guide](https://github.com/yt-dlp/yt-Dlp/wiki/EJS)
- [MusicBrainz API rate limiting](https://wiki.musicbrainz.org/MusicBrainz_API/Rate_Limiting)
- [AcoustID web service](https://acoustid.org/webservice)
- [mutagen documentation](https://mutagen.readthedocs.io/en/latest/index.html)
