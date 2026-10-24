# Plan 12: Metadata providers

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 09
- **Разблокирует:** Plan 14, 16
- **Оценка:** L
- **Риск:** средний

## Роль

Ты — senior Python engineer, отвечающий за внешние metadata observations. Работаешь в `trackforge` после local MVP. Задача — MusicBrainz, AcoustID, Cover Art Archive, LRCLIB, fingerprint и нормализация их ответов в observations/claims; без LLM и без live-тестов в CI.

## Цель

Для локального файла (и позже URL) работают opt-in providers: fingerprint → AcoustID → MusicBrainz → claims title/artist/album/date/genre/MBIDs; CAA/iTunes/Deezer — cover candidates; LRCLIB — lyrics candidates. Сеть деградирует видимо; `--offline` не делает запросов; ответы кэшируются как артефакты с TTL.

## Контекст и инварианты

- MusicBrainz: 1 req/s на IP, обязательный `User-Agent` с контактом, кэш (`05-sources.md` §5.1).
- AcoustID: ≤3 req/s, client API key, некоммерческое использование; `fpcalc` — внешний бинарник (LGPL).
- Cover Art Archive: release/release-group MBID; front cover; fallback iTunes/Deezer/thumbnail.
- LRCLIB: plain/LRC по title/artist/album/duration; вежливые запросы; битые тайминги — warning.
- Сетевые ответы — observations, не решения; resolver выбирает.
- MBID/ISRC только из authority; никогда не выдумывать.
- Секреты — `TRACKFORGE_ACOUSTID_API_KEY`, `TRACKFORGE_MUSICBRAINZ_USER_AGENT` (не секрет, но обязателен); не логировать.
- `--offline` блокирует все HTTP/fpcalc-сеть; локальные observers продолжают.
- HTTP-клиент один (`httpx`), таймауты/ретраи/лимиты; кассеты — `respx`.
- D4: MusicBrainz через `httpx`, без `musicbrainzngs`.

## Прочитать перед началом

- `docs/design/05-sources.md` — §5, §8–9.
- `docs/design/06-analysis.md` — §3.3, §5, §9–11, §12.
- `docs/design/03-pipeline.md` — §2.4, §5, §7.
- `docs/design/04-storage.md` — §4, §6.4.
- `docs/design/02-architecture.md` — §5, §14, §16.
- `docs/design/10-operations.md` — §3, §8.
- `docs/plan/09-publish-cli-local-mvp.md` (handoff).

## Предусловия

```bash
uv sync --extra tagging
ffprobe -version
fpcalc -version          # если нет — см. ниже
uv run ruff check .
uv run mypy src
uv run pytest tests/integration/test_local_e2e.py -q
uv run lint-imports
```

`fpcalc` нужен для AcoustID; если его нет — Plan 12 может работать без AcoustID (degraded), но acceptance по fingerprint требует установки. Не устанавливай системные пакеты без разрешения.
## Scope

- `adapters/http/client.py`: общий `httpx.AsyncClient`, timeout, retry, User-Agent, redaction.
- `adapters/http/rate_limit.py`: per-host limiter (MB 1/s, AcoustID 3/s, прочие).
- `adapters/analysis/chromaprint.py`: `fpcalc` subprocess → `audio.fingerprint.v1`.
- `adapters/metadata/musicbrainz.py`: search/lookup → `musicbrainz.search.v1`.
- `adapters/metadata/acoustid.py`: lookup → `acoustid.lookup.v1`.
- `adapters/metadata/coverart.py`: CAA/iTunes/Deezer → `cover.candidates.v1`.
- `adapters/metadata/lrclib.py`: → `lyrics.candidates.v1`.
- `application/use_cases/observe_item.py` — регистрация провайдеров и desired outputs.
- `application/use_cases/interpret_item.py` — MB/AcoustID/CAA/LRCLIB claims + `identity.hypotheses.v1`.
- `doctor` checks: fpcalc, key presence, User-Agent.
- Policy registry: MB/AcoustID claims — authority/source priority; CAA/LRCLIB — provider chain.
- `[project.dependencies] httpx` (BSD-3); dev `respx` (BSD-3); `fpcalc` — external.
- Тесты `respx`/fixtures, rate limiter fake clock, TTL.
## Non-goals

- LLM/ranker/autopilot.
- ASR/lyrics merge (Plan 16); LRCLIB только candidates.
- Spotify/Discogs/Last.fm/Genius/web search — Deferred.
- Playlist context (Plan 18).
- Live-тесты в CI; `hishel` HTTP-кэш — Deferred (кэш артефактов достаточен).
- Нормализованные identity-таблицы.
## Deliverables

```text
src/trackforge/adapters/http/__init__.py
src/trackforge/adapters/http/client.py
src/trackforge/adapters/http/rate_limit.py
src/trackforge/adapters/analysis/chromaprint.py
src/trackforge/adapters/metadata/__init__.py
src/trackforge/adapters/metadata/musicbrainz.py
src/trackforge/adapters/metadata/acoustid.py
src/trackforge/adapters/metadata/coverart.py
src/trackforge/adapters/metadata/lrclib.py
src/trackforge/application/dto/providers.py
tests/adapters/http/test_rate_limit.py
tests/adapters/metadata/{test_musicbrainz,test_acoustid,test_coverart,test_lrclib}.py
tests/integration/test_metadata_resolve.py
tests/fixtures/http/{musicbrainz_search,musicbrainz_lookup,acoustid_lookup,caa_release,lrclib_get}.json
```
## Контракты

### HTTP / rate limit

```text
HttpClient:
  async get(url, params, headers, timeout) -> HttpResponse
  async post(url, data, headers, timeout) -> HttpResponse
  async get_bytes(url, max_bytes) -> bytes
```

- `User-Agent`: `TrackForge/<version> (<contact>)`; contact из env; если пуст — warning и requests не выполняются для MB.
- Retry: 429/5xx/network, max 2, exponential backoff + jitter; `Retry-After` уважается.
- Redirects: max 5; только http/https; SSRF guard (Plan 11) применяется к внешним URL, включая CAA/lyrics.
- Redaction: не логировать query с `api_key`, cookies, authorization.
- Per-host limiter: MB 1 req/s; AcoustID 3 req/s; CAA/LRCLIB/iTunes/Deezer — консервативный (например, 2 req/s); глобальный лимит.
- Лимитер тестируется с fake clock; реальные `sleep` в unit-тестах запрещены.

### Provider port
`MetadataProvider.search(query) -> list[Observation]`, `lookup(id) -> Observation`; observers оборачивают providers в capabilities. MusicBrainz/AcoustID/CAA/LRCLIB реализуют этот порт.

### Observations

```text
audio.fingerprint.v1: fingerprint, duration_seconds, tool{id:"fpcalc", version}
acoustid.lookup.v1: candidates[]{acoustid_id, recording_mbids[], score, title?, artist?}
musicbrainz.search.v1: query{title?,artist?,duration?}, recordings[]{mbid,title,artists[],
  duration,isrcs[],releases[]}, releases[]{mbid,title,date,country,label?,barcode?,
  release_group?}, artists[]{mbid,name,sort_name?}, raw_ref?
cover.candidates.v1: candidates[]{source,url?,release_mbid?,mime,width,height,size_bytes?,score,artifact_ref?}
lyrics.candidates.v1: candidates[]{source,title,artist,album?,duration?,plain?,lrc?,language?,has_timing,score}
```

- Все ответы сохраняются как артефакты с `expires_at` (TTL: MB/CAA 7 дней, AcoustID 30 дней, LRCLIB 7 дней) и `--refresh`-поддержкой.
- `source`/`method` в claims: `musicbrainz`, `acoustid`, `cover_art_archive`, `itunes`, `deezer`, `lrclib` + версия адаптера.
- MBID/ISRC валидируются; невалидные отбрасываются с warning.
- Cover: front cover, prefer ≥1000×1000; не upscale; bytes в CAS/artifact; MIME/size limits.
- Lyrics: plain и LRC; LRC парсится/валидируется (тайминги возрастают); битые — candidate с warning.

### Claims / identity

- `identity_resolver` дополняется: AcoustID candidates + MB search + duration + title similarity → `identity.hypotheses.v1` с score/evidence/status (`proposed|accepted|rejected|ambiguous`).
- MB claims:
  - `recording.title`, `recording.artist` (role main), `recording.isrc`, `recording.mbid`;
  - `release.title`, `release.date`, `release.country`, `release.label`, `release.barcode`, `release.mbid`;
  - `artist.name`, `artist.mbid`.
- CAA claims: `item.artwork` / `release.artwork` with evidence candidate.
- LRCLIB claims: `item.lyrics`/`item.synced_lyrics` candidates; не выбирать финал.
- Confidence: AcoustID score калибруется консервативно; MB match score — evidence, не истина.
- Если evidence противоречивы — `ambiguous`, не выбирать «наугад».
- `source.metadata.v1` (Plan 11) участвует в query, но не заменяет MB.

### Offline / TTL

- `config.offline=True` или `--offline`: сетевые capabilities `skipped` + warning `offline`; локальные (probe/tags/features) выполняются.
- `--refresh`: игнорирует TTL только для выбранных провайдеров; старые артефакты не удаляются.
- TTL-истёкший артефакт используется с warning `stale`; `--refresh` принудительно обновляет. Зафиксировать в тесте.
## Порядок работ

1. Добавь `httpx`/`respx`; реализуй HTTP client/rate limiter.
2. Реализуй `fpcalc` observer и `audio.fingerprint.v1`.
3. Реализуй AcoustID lookup + error/retry.
4. Реализуй MusicBrainz search/lookup + нормализацию.
5. Реализуй CAA/iTunes/Deezer и LRCLIB.
6. Расширь interpreters: MB/AcoustID/CAA/lyrics claims + identity hypotheses.
7. Добавь TTL/offline/refresh; doctor checks.
8. Добавь кассетные тесты; live — `network`.
9. Прогони quality gates; закоммить.
## Тесты

- **unit:** rate limiter timing (fake clock); URL/params; MB JSON parsing; MBID/ISRC validation; TTL stale/refresh; offline; cover dimensions; LRC parser.
- **contract:** providers реализуют `Observer`/`MetadataProvider`; claims имеют provenance; `identity.hypotheses.v1` schema.
- **integration (respx):** local file + cassettes → MB/AcoustID/CAA/LRCLIB observations → claims → resolve title/artist/album/MBID; no network.
- **fixtures:** JSON cassettes без cookies/токенов; generated fingerprint fixture; cover bytes fake.
- **negative:** 429/503; invalid JSON; missing API key; missing User-Agent; bad MBID; CAA 404; LRCLIB no match; SSRF URL; cookies not in cache key.
## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/http tests/adapters/metadata -q
uv run pytest tests/integration/test_metadata_resolve.py -q
uv run lint-imports
uv run trackforge doctor --json
```

Ожидается: кассетные тесты зелёные; MB claims появляются; `--offline` даёт warnings; `doctor` показывает fpcalc/key/User-Agent без значений.
## Acceptance criteria

- [ ] `uv run pytest tests/adapters/http tests/adapters/metadata tests/integration/test_metadata_resolve.py -q` зелёный.
- [ ] `fpcalc` → `audio.fingerprint.v1`; AcoustID lookup с key из env.
- [ ] MusicBrainz search/lookup: rate limit 1 req/s, User-Agent, retry/backoff.
- [ ] CAA/iTunes/Deezer cover candidates; LRCLIB plain/LRC candidates.
- [ ] MBID/ISRC только из ответа; невалидные отбрасываются.
- [ ] `--offline` не делает HTTP; деградация видима.
- [ ] TTL/`--refresh` работают; кэш не содержит cookies/токенов.
- [ ] `identity.hypotheses.v1` содержит evidence/score/status; ambiguous допустим.
- [ ] `doctor` проверяет fpcalc/ключи/User-Agent без значений.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- HTTP client/rate limiter.
- `audio.fingerprint.v1`, `acoustid.lookup.v1`, `musicbrainz.search.v1`,
  `cover.candidates.v1`, `lyrics.candidates.v1`.
- MB/AcoustID/CAA/LRCLIB claims и identity hypotheses.
- TTL/offline/refresh; doctor checks.
- Кассеты/fixtures без сети.

## Stop conditions / decision gates

- D4 не выбран — используй `httpx`; не добавляй `musicbrainzngs`.
- Нет `fpcalc` — AcoustID `skipped/degraded`; не подменяй fingerprint.
- Нет API key/User-Agent — provider `skipped`, не выдумывай ответы.
- Нужен LLM/lyrics merge — Plans 14/16.
- Нужны live-тесты в CI — стоп; только `@pytest.mark.network`.
