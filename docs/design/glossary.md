# Glossary

> Единый язык проекта. Если термин используется в документах/коде — он должен значить то, что здесь.

## A–C

**Artifact** — типизированный результат шага: `Observation`, `Claim`-снимок, `DecisionRecord`, `OutputBundle`. Имеет манифест, версию схемы и хэш.

**AnalysisProfile** — набор включённых capability, бюджет и качество (`fast`/`balanced`/`full`/`custom`).

**Candidate** — возможный вариант медиа от источника (формат/качество/язык) до скачивания.

**Capability** — шаг пайплайна с манифестом `requires/provides/cost/version`.

**CAS** — content-addressed storage; байты по SHA-256, неизменяемы.

**Claim** — утверждение о значении поля с subject, source, method, confidence, evidence.

**Collection** — плейлист/альбом/канал/папка; контейнер с контекстом, не трек.

**CollectionItem** — позиция в коллекции; ссылка на candidate/item.

**Confidence** — калиброванная уверенность claim (0..1); не сравнивается между методами без калибровки.

**Correction** — событие, когда пользователь изменил решение системы; хранится с контекстом.

**Content hash** — SHA-256 исходных байтов; идентификатор `MediaObject`.

## D–F

**Decision** — выбранное значение поля + альтернативы + policy + rationale + lock.

**DecisionRecord** — снимок всех decisions по item с версиями policy/модели/промпта.

**Degradation** — видимое ухудшение (optional output недоступен), статус `degraded`/`skipped`.

**Derivative** — производная версия recording: nightcore, remix, cover, sped up, slowed, 8D и т.п.

**Desired output** — желательное поле (title/artist), но не обязательное; можно `unknown`.

**Dry-run** — план/прогноз без side effects.

**FeedbackEvent** — событие обратной связи: correction, approval, rejection, lock, review note.

**FileInstance** — конкретное появление байтов на диске (source/cas/render/library/duplicate).

**FieldPolicy** — правила выбора значения для поля.

## G–L

**Item** — агрегатный корень одной аудиоединицы; ссылается на media/intake/claims/decisions/renders.

**Intake** — факт приобретения: source, candidate, snapshot, downloader, media.

**LibraryEntry** — опубликованный элемент managed library.

**Lock** — пользовательская фиксация поля/item; сильнее policy/LLM.

**LLM autopilot** — режим, где LLM выбирает/синтезирует финальные значения с guardrails.

**License boundary** — правило: ядро не импортирует GPL/AGPL; они живут в адаптерах/extras.

## M–R

**Managed library** — управляемая библиотека output-файлов; оригиналы не трогаем.

**MediaObject** — неизменяемые байты + probe + content hash.

**Observation** — сырой результат измерения/получения; immutable.

**OutputBundle** — фактический набор файлов рендера (audio, cover, lyrics, sidecar).

**PreferenceRule** — явное правило, выведенное из corrections; не автообучение.

**Provenance** — источник/метод/время/стоимость/доказательства claim.

**ProviderChain** — упорядоченные альтернативные провайдеры для типа артефакта.

**Ranker** — не-генеративная модель/эвристика, упорядочивающая готовые кандидаты.

**Recording** — конкретное исполнение/запись; не release и не file.

**Release** — альбом/EP/сингл; содержит recordings.

**ReleaseGroup** — группа релизов.

**Render** — операция сборки output bundle из decisions + media.

**Required output** — обязательный результат; для MVP — успешно созданный выходной файл.

**Resume** — продолжение run с места остановки без повторного выполнения шагов.

## S–Z

**Sidecar** — `trackforge.json` рядом с output; портативный снимок metadata/decisions.

**SourceRef** — входная точка: путь, URL, текстовый запрос, collection ref.

**Step** — шаг выполнения capability в run.

**Subject** — к чему относится claim: item/media/recording/release/artist/derivative/collection.

**Transformation** — тип изменения derivative (nightcore, sped_up, remix, ...).

**Unknown** — легальное решение: доказательств нет, поле не заполняется.

**Work** — композиция (слова+музыка), не recording.

## Сокращения

- **MB** — MusicBrainz.
- **MBID** — MusicBrainz Identifier.
- **CAA** — Cover Art Archive.
- **LRC** — формат синхронизированных lyrics.
- **ELRC** — расширенный LRC с word timestamps.
- **ASR** — automatic speech recognition.
- **LUFS** — единица громкости.
- **CAS** — content-addressed storage.
- **DTO** — data transfer object.
- **ADR** — architecture decision record.
