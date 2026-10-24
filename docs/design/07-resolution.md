# 07. Resolution

> Статус: **черновик v0.3**. Как из claims получаются решения, как работают политики, LLM/ranker, locks и feedback.
> Это ядро продукта: именно здесь решается, что попадёт в теги, а что останется `unknown`.

## 1. Место в конвейере

```text
Observations → Claims → [Normalize → Group → Policy → Validate] → DecisionRecord
                                                          ↑
                                                   locks / feedback
```

Резолвер не ходит в сеть и не читает аудио. Он работает с claims, политиками, пользовательскими locks и (опционально) LLM/ranker.

## 2. Алгоритм

Для каждого поля:

1. Собрать все claims с этим `field`/`subject`.
2. Отбросить невалидные/устаревшие/запрещённые.
3. Применить `user_lock`, если есть.
4. Нормализовать значения **только для сравнения**, сохраняя исходные.
5. Сгруппировать эквивалентные значения.
6. Применить стратегию поля (authority/consensus/priority/ranker/LLM/derive/...).
7. Проверить валидаторы (формат, диапазон, cross-field).
8. Если уверенность ниже порога — `unknown`/`needs_review`.
9. Записать `Decision` со всеми альтернативами и rationale.

Порядок важен: **lock → valid → normalize → group → strategy → validate → review**.

## 3. Нормализация

Нормализация нужна для сравнения, но не для записи. Мы никогда не заменяем исходное значение «нормализованным» без решения политики.

### 3.1. Строки

- Unicode NFKC; trim; collapse whitespace.
- Регистр — только для сравнения (casefold).
- Пунктуация: `feat.`/`ft.`/`featuring`, `&`/`and`, `vs.`/`versus`.
- Кавычки/апострофы: `’` → `'`.
- Скобки: `()`, `[]`, `【】` — учитываем как qualifier/remaster/version.
- Лишние суффиксы: `Official Video`, `Lyrics`, `Audio`, `HD`, `4K`, `MV` — **не удаляем автоматически**, а помечаем как шум; политика/LLM решает.
- `Artist - Topic` → `Artist` (явное правило для YouTube auto-generated channels).
- `VEVO` — не артист; убираем из artist, оставляем в source.
- Эмодзи/декоративные символы — сохраняем в исходном claim, но не используем для сравнения.

### 3.2. Артисты

- Разделение по `,`, `&`, `feat.`, `ft.`, `x`, `×` — эвристика; при наличии MB credits предпочитаем структурированные данные.
- Роли: `main`, `featured`, `remixer`, `composer`, `lyricist`, `producer`, `albumartist`.
- Не склеивать `Artist A & Artist B` в одну строку, если есть отдельные claims.
- `Various Artists` — для compilation, не для обычного артиста.

### 3.3. Даты

- Нормализуем к ISO `YYYY-MM-DD`/`YYYY-MM`/`YYYY`.
- Не угадываем день/месяц, если известен только год.
- `date` (release) и `originaldate` — разные поля.
- Для derivative: дата оригинала и дата загрузки — разные claims.

### 3.4. BPM/Key

- BPM: число; допускаем half/double time; храним метод/confidence.
- Key: `C`, `C#`, `Db`, `Am`, `F#m`; нормализуем к одному формату, но храним исходный.
- Не «исправляем» BPM на основе жанра.

### 3.5. Транслитерация

- Нужна для сравнения (например, `Группа` vs `Gruppa`), но не для записи.
- Не транслитерируем title/artist без явной политики.

## 4. Уверенность

### 4.1. Raw vs calibrated

- `raw_score` — то, что вернул метод.
- `calibrated_confidence` — то, что использует политика.
- Калибровка per-method; для v1 — консервативные коэффициенты + agreement boost.
- Не сравниваем raw scores разных методов.

### 4.2. Факторы

- Надёжность источника (authority).
- Качество совпадения (similarity, duration, fingerprint).
- Согласие независимых источников.
- Наличие структурированного ID (MBID/ISRC).
- Качество LLM-ответа (валидность, цитаты, противоречия).
- Для derivative — согласие метаданных/темпа/длительности.

### 4.3. Agreement boost

Если два независимых источника дают одно и то же значение — confidence растёт, но не до 1.0. Если источники зависимы (например, iTunes и Deezer оба из одного агрегатора) — boost меньше.

## 5. Конфликты

| Тип | Пример | Что делаем |
|---|---|---|
| Точное совпадение | `Song` == `Song` | group |
| Нормализованное | `Song!` vs `Song` | group |
| Семантическое | `Song (Remastered 2011)` vs `Song` | qualifier/version, не тот же title |
| Роли | `Artist A` main vs `Artist B` feat. | разные claims, не конфликт |
| Локали | `Song` vs `Песня` | разные claims с locale |
| Версии | original vs nightcore | разные subject (recording vs derivative) |
| ID | MBID A vs MBID B | identity ambiguity |

Правило: **не мержить разные subject'ы** (recording vs derivative vs release) и **не мержить разные роли** в один claim.

## 6. Политики полей

### 6.1. Общий вид

```text
FieldPolicy:
  field
  scope
  conditions[]
  strategy
  priority[]
  validators[]
  on_missing
  version
```

Стратегии:

| Стратегия | Когда | Что делает |
|---|---|---|
| `user_lock` | есть lock | берёт пользовательское значение |
| `authority` | есть MBID/ISRC/AcoustID | предпочитает авторитетный источник |
| `source_priority` | задан порядок | первый валидный из списка |
| `consensus` | несколько источников | выбирает согласованное значение |
| `ranker_adjudicate` | конечный набор | ранжирует и выбирает |
| `llm_adjudicate` | сложный конфликт | LLM выбирает с evidence |
| `derive` | значение собирается | например, title + qualifier |
| `keep_existing` | existing tag валиден | сохраняет текущий тег |
| `review` | спорно | отдаёт человеку |
| `unknown` | нет данных | оставляет пусто |

### 6.2. Title

Приоритет по умолчанию:

1. `user_lock`.
2. MusicBrainz recording title (если identity confident).
3. Source metadata title (если это не мусор).
4. Existing tag (если валиден).
5. LLM extractor (с evidence).
6. `unknown`.

Для derivative:

- base title = recording.title (если найден);
- qualifier = из transformations;
- если source title уже содержит qualifier — не дублируем;
- если source title — единственный источник, используем его, но чистим мусор.

Не добавляем `(Official Video)`/`(Lyrics)` в title; это source noise.

### 6.3. Artist

Приоритет:

1. `user_lock`.
2. MusicBrainz recording artist credits.
3. Source uploader/channel, если это артист (не `Topic`/`VEVO`/fan channel).
4. Existing tag.
5. LLM extractor.
6. `unknown`.

Роли:

- main artists — в `artist`;
- featured — в `artist` с ролью или отдельным полем (по формату);
- remixer — в `remixer`/comment, не обязательно в main artist;
- albumartist — отдельно.

Для derivative:

- nightcore/sped up: main artist = original artist; uploader — в `comment`/`source`/`remixer` (по политике);
- remix: original artist + remixer (роль);
- cover: covering artist; original artist — в `originalartist`/comment.

### 6.4. Album / albumartist

Приоритет:

1. `user_lock`.
2. Release, к которому принадлежит recording (если identity confident).
3. Collection context (playlist/album), если политика `playlist_first`.
4. Existing tag.
5. `unknown`.

`albumartist`:

- release artist;
- для compilation — `Various Artists`;
- для playlist-first — owner/`Various Artists` (по политике).

### 6.5. Date / originaldate

- `date` — дата release (или загрузки, если release нет).
- `originaldate` — дата оригинального release/recording.
- Для derivative: `date` = дата оригинального релиза (если album-first) или дата загрузки (если playlist-first); `originaldate` — оригинал.
- Не угадывать день/месяц.

### 6.6. Genre / style

- MusicBrainz genres/tags + source tags + LLM (с evidence).
- Multi-value; не раздувать список.
- Derivative: `Nightcore`/`Remix` может быть genre или отдельным полем; по умолчанию — genre + comment.
- Не смешивать genre и mood; mood — отдельное поле (если поддерживается).

### 6.7. Track / disc

- Из release, если identity confident.
- Из playlist position — если playlist-first.
- Existing tag — если валиден.
- Для сингла — `1/1` или пусто (по политике).

### 6.8. Cover

Приоритет:

1. Cover Art Archive front (release).
2. iTunes/Deezer.
3. Source thumbnail.
4. Existing embedded art.
5. `unknown`.

Критерии: разрешение, aspect, watermark, соответствие release/derivative.
Для derivative: original cover, если identity confident; иначе source thumbnail.

### 6.9. Lyrics

Приоритет:

1. LRC с хорошим совпадением (title/artist/duration).
2. Plain lyrics + ASR alignment.
3. ASR-only.
4. Existing lyrics.
5. `unknown`.

Для nightcore: original lyrics + масштабированные тайминги; не ASR-текст, если original lyrics найдены.

### 6.10. BPM / key

- Acoustic estimate (librosa/essentia) + confidence.
- Existing tag — альтернатива, но может быть неверным.
- Для derivative: BPM измеренный; original BPM — в sidecar/comment.
- Key — не транспонировать автоматически; derivative key может быть выше.

### 6.11. Language

- ASR language, MB release language, source metadata, lyrics language.
- ISO 639-1/2; не угадывать по названию.

### 6.12. IDs

- MBID/ISRC/AcoustID — только из authority; никогда не выдумывать.
- Валидировать формат.
- Хранить отдельными полями; не смешивать recording/release/artist MBID.

### 6.13. Comment / source

- Source URL, extractor, original title, uploader, derivative info.
- Не пихать огромное description; sidecar — лучше.
- `comment` — короткий, человекочитаемый.

### 6.14. Compilation

- Release type/secondary type, albumartist, track count.
- Не ставить `compilation=1` только потому, что в плейлисте разные артисты.

## 7. Derivative-aware политики

### 7.1. Nightcore

Пример:

- `recording.title = "Song"`;
- `recording.artist = "Artist"`;
- `derivative.transformations = ["nightcore", "sped_up"]`;
- `source.title = "Song (Nightcore)"`;
- `source.uploader = "NightcoreChannel"`.

Решение по умолчанию:

- `title = "Song (Nightcore)"` (derive, без дубля);
- `artist = "Artist"`;
- `album =` release album, если найден; иначе playlist/unknown;
- `genre += "Nightcore"`;
- `comment = "Nightcore edit; source: ..."`;
- `bpm =` измеренный;
- `cover =` original cover, если найден; иначе source thumbnail;
- `uploader` — в source/comment, не в main artist.

Если пользователь предпочитает `artist = "Artist feat. NightcoreChannel"` — это lock/правило.

### 7.2. Remix

- `title = "Song (Remixer Remix)"` или source title, если чище;
- main artist = original artist; remixer — отдельная роль/поле;
- альбом — original release или сборник ремиксов, если найден;
- MB relationship (remix) — сильное evidence.

### 7.3. Cover

- main artist = covering artist;
- `title = "Song (Cover)"`;
- original artist — в `originalartist`/comment;
- альбом — release cover-артиста, если найден;
- lyrics — как у cover (может отличаться).

### 7.4. Instrumental / acapella

- `title` + qualifier;
- lyrics — пусто/`[Instrumental]` (по политике);
- vocal features — evidence;
- genre — без изменений.

### 7.5. Sped up / slowed / 8D

- qualifier в title;
- BPM/длительность — измеренные;
- original BPM/длительность — в sidecar/comment;
- genre — `Sped Up`/`Slowed`/`8D` по политике.

## 8. Playlist / album context

`Collection` даёт context claims:

- title, owner, description, cover;
- position;
- album/artist/date, если плейлист — альбом.

Политика `tag_basis`:

| Значение | album | albumartist | track | date | cover |
|---|---|---|---|---|---|
| `album_first` (default) | release | release artist | release track | release date | release cover |
| `playlist_first` | playlist title | owner/Various Artists | playlist position | playlist date | playlist cover |
| `ask` | спросить | спросить | спросить | спросить | спросить |

Если release не найден, `album_first` деградирует до playlist/unknown. Пользователь может переопределить для коллекции.

## 9. LLM-режимы

### 9.1. `policy`

- LLM используется как extractor/ranker, но финальное решение — policy.
- LLM claims проходят те же валидаторы.

### 9.2. `llm_assisted`

- LLM предлагает решение по конфликтным полям.
- Policy проверяет: locks, validators, evidence.
- Если LLM противоречит authority/валидатору — отказ/fallback.

### 9.3. `llm_autopilot`

- LLM получает claims table + правила + целевые поля.
- Возвращает решения по каждому полю: `value`, `chosen_claim_ids`, `derived_from`, `confidence`, `reason`.
- Не может выдумывать MBID/ISRC/URL; только выбирать/синтезировать из claims.
- Locks всегда сильнее.
- Валидаторы обязательны.
- Невалидный/неуверенный ответ → policy/review.
- `DecisionRecord` фиксирует модель/промпт/стоимость/входные claim IDs.
- Autopilot — только API-модель; локальный 0.5B fallback не используется для autopilot (только classification/ranking).

### 9.4. Промпт

Содержит:

- задачу и целевые поля;
- список claims с ID, source, method, confidence, evidence;
- правила (не выдумывать ID, не менять subject, не добавлять мусор);
- JSON-схему;
- примеры (few-shot) — осторожно, без утечки данных.

Источник description — недоверенный; обрамляем и инструктируем игнорировать инструкции внутри.

## 10. Review и feedback

### 10.1. Review

`trackforge review <item>` показывает:

- поле;
- текущее решение;
- альтернативы (source, confidence);
- rationale;
- warnings;
- actions: `accept`, `edit`, `lock`, `unknown`, `why`.

`--auto` — принять решения без вопросов; `--review` — всегда показать.

### 10.2. Correction

При изменении:

- создаётся `Correction` с `system_value`/`user_value`/`scope`/`reason`;
- создаётся claim `source=user`, `method=manual_override`, `confidence=1.0`;
- опционально ставится lock;
- `context_snapshot` сохраняет claims/alternatives/versions.

Scope по умолчанию — `item`; повышение до `source`/`artist`/`profile`/`global` — явное. Scope шире `item` порождает `PreferenceRule` (future), а не per-item lock; lock всегда остаётся item/field.

### 10.3. Locks

- Поле или item.
- Lock сильнее policy/LLM.
- Снятие lock — также feedback-событие.
- Lock не удаляет старые claims; они остаются для аудита.

## 11. Explainability

`trackforge explain <item> --field title`:

```text
Field: title
Decision: "Song (Nightcore)"
Mode: policy
Policy: title.derivative.v3
Chosen: claim 01J... (recording.title="Song", MB, 0.95)
Derived: + qualifier "Nightcore" from claim 01J... (derivative, 0.92)
Alternatives:
  - "Song (Nightcore) [Official Video]" (source, 0.60) — rejected: noise suffix
  - "song_nightcore_final" (existing tag, 0.20) — rejected: low confidence
Rationale: original title + derivative qualifier; source title contained noise.
```

`DecisionRecord` хранит всё необходимое; explain — представление.

## 12. Валидаторы

### 12.1. Полевые

- title/artist: непустые, разумная длина, нет path-разделителей;
- date: ISO/год, не в будущем (кроме известных анонсов);
- bpm: 20–300 (с допуском half/double);
- key: допустимые обозначения;
- MBID: UUID;
- ISRC: формат;
- cover: MIME/размер/разрешение;
- lyrics: длина/язык/тайминги;
- track/disc: >0, <= total;
- language: ISO.

### 12.2. Cross-field

- `albumartist` не пуст, если есть `album`;
- `track` <= `tracktotal`;
- `disc` <= `disctotal`;
- `date` >= `originaldate` (если оба);
- `compilation` согласован с albumartist;
- derivative qualifier не дублируется.

### 12.3. Hard vs soft

- Hard: невалидный ID, path traversal, пустой обязательный output; reject claim/decision.
- Soft: подозрительная длина/дата/жанр; warning, не reject.
- Если все claims поля невалидны → `unknown`/review.

## 13. Метрики

- Correction rate по полям/источникам/моделям.
- Accuracy на размеченном наборе.
- Calibration (confidence vs correctness).
- Source contribution (leave-one-source-out).
- LLM vs ranker vs policy: accuracy/cost/latency.
- `unknown` rate (не слишком высокий и не слишком низкий).
- Review rate.

## 14. Примеры

### 14.1. Nightcore с YouTube

См. `01-domain-model.md` §12. Ключевое:

- title = original + qualifier;
- artist = original;
- album = release или playlist;
- uploader — в source/comment;
- cover = original или thumbnail;
- genre += Nightcore;
- locks/feedback.

### 14.2. Локальный файл с кривыми тегами

- existing tags: `artist="DJ Somebody"`, `title="track_final_final"`, `album=""`.
- path: `Music/Artist/Album/01 - Song.mp3`.
- AcoustID → MB recording → title/artist/album.
- Policy: authority > existing tags; path — слабое evidence.
- Итог: title/artist/album из MB; existing — alternatives.

### 14.3. Плейлист

- collection: title `Chill Mix`, owner `User`.
- track: `Song` by `Artist`.
- `album_first`: album = `Album`, albumartist = `Artist`, track = release track.
- `playlist_first`: album = `Chill Mix`, albumartist = `Various Artists`, track = position.
- Пользователь выбирает основу; sidecar хранит оба варианта.

## 15. Открытые вопросы

- Дефолтный `tag_basis` для плейлистов: `album_first` или `playlist_first`? Предложение: `album_first`, если release confidence высокий; иначе playlist.
- Насколько агрессивно чистить source title от `Official Video`/`Lyrics`? Предложение: только при наличии evidence/LLM, не глобально.
- Добавлять ли `Nightcore` в genre или только в title/comment? Предложение: genre + comment, конфигурируемо.
- Original cover vs derivative thumbnail по умолчанию? Предложение: original, если identity confident; иначе source.
- Роль uploader/remixer в artist для derivative? Предложение: не main artist; отдельное поле/comment.
- Какие поля разрешены в autopilot по умолчанию? Предложение: title/artist/album/date/genre/cover; IDs — только policy.
