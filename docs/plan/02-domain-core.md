# Plan 02: Domain core

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 01
- **Разблокирует:** Plan 03, 04, 05
- **Оценка:** L
- **Риск:** средний

## Роль

Ты — senior Python engineer, отвечающий за доменную модель. Работаешь в `trackforge` после bootstrap. Твоя задача — чистые типы и инварианты, без I/O и без знания о БД, HTTP, CLI и форматах файлов.

## Цель

Появляется импортируемый `trackforge.domain`: сущности Item/Media/Intake, observations/claims/decisions, feedback, capabilities и outputs. Инварианты выражены типами и валидаторами, а не комментариями; домен не импортирует внешние слои.

## Контекст и инварианты

- `Observation` immutable; `Claim` append-only, статус — проекция; `DecisionRecord` append-only (`01-domain-model.md` §11).
- У каждого claim есть `source`, `method`, `confidence` и `evidence` либо явная причина, почему evidence — source snapshot.
- `unknown` — легальное решение; синтезированное значение обязано иметь `derived_from`.
- `MediaObject.content_hash` — SHA-256 исходных байтов; оригиналы не меняются.
- `Item` — агрегатный корень, ссылается на media/intake/claims/decisions/renders по ID.
- `Collection` — не трек; `Release` — не `Collection`.
- Домен не знает про `httpx`, `ffmpeg`, `mutagen`, SQLAlchemy, pydantic и Typer (`02-architecture.md` §3).
- ID — opaque, sortable: UUIDv7 внутри, но домен не зависит от конкретного генератора (D2).
- В v1 identity-сущности живут в claims/artifact, без нормализованных таблиц (`13-consistency-review.md` §4).

## Прочитать перед началом

- `docs/design/01-domain-model.md` — полностью, особенно §2–11.
- `docs/design/00-vision.md` — §5, §9, §10.
- `docs/design/02-architecture.md` — §3–4.
- `docs/design/07-resolution.md` — §6–9.
- `docs/design/04-storage.md` — §6.2, §6.2.1.
- `docs/design/glossary.md`.
- `docs/design/adr/0002-claims-decisions.md`, `docs/design/adr/0003-content-addressed-storage.md`.
- `docs/plan/01-bootstrap.md` (handoff).
- `src/trackforge/domain/`, `tests/domain/` (если есть).

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest
uv run lint-imports
```

Все зелёные. Если `trackforge.domain` ещё содержит заглушки из Plan 01 — это нормально, расширяй их, не ломая config/CLI.
## Scope

- `domain/model/ids.py`: ID-типы, парсинг/валидация, UUIDv7-генератор.
- `domain/model/source.py`: `SourceRef`, `Candidate`, `Format`, `Thumbnail`.
- `domain/model/media.py`: `MediaObject`, `FileInstance`, `Intake`.
- `domain/model/item.py`: `Item`, `ItemStatus`, допустимые переходы.
- `domain/model/claims.py`: `Observation`, `Claim`, `Provenance`, `Confidence`, `ClaimStatus`.
- `domain/model/decisions.py`: `FieldPolicy`, `Decision`, `DecisionRecord`, `DecisionMode`, `DecidedBy`, `FieldValueClass`.
- `domain/model/feedback.py`: `FeedbackEvent`, `Correction`, `ReviewSession`, `CorrectionScope`, `ReasonCode`.
- `domain/model/capabilities.py`: `ArtifactType`, `CapabilityManifest`, `Cost`, `Determinism`, `ProviderChain`, `AnalysisProfile`.
- `domain/model/outputs.py`: `ResolvedMetadata`, `Render`, `OutputBundle`, `LibraryEntry`.
- `domain/model/identity.py`: лёгкие `Work`, `Recording`, `Release`, `ReleaseGroup`, `Artist`, `Derivative`, `IdentityHypothesis` — только типы, без таблиц.
- `domain/errors.py`: `TrackForgeError` и подклассы из `09-interfaces.md` §2.8, поля `retryable`, `user_action`, `context`.
- `domain/__init__.py` — только удобные реэкспорты, не публичный API пакета.
## Non-goals

- Pydantic-модели, ORM, JSON-схемы, миграции, репозитории.
- Нормализация строк, стратегии policy, валидаторы полей — Plan 07.
- LLM/ranker, feedback export, review CLI.
- Полный MusicBrainz relationship-граф, credits, multilingual titles; для этого — explicit `Deferred`.
- Генерация файлов/путей/тегов.
## Deliverables

```text
src/trackforge/domain/model/__init__.py
src/trackforge/domain/model/ids.py
src/trackforge/domain/model/source.py
src/trackforge/domain/model/media.py
src/trackforge/domain/model/item.py
src/trackforge/domain/model/claims.py
src/trackforge/domain/model/decisions.py
src/trackforge/domain/model/feedback.py
src/trackforge/domain/model/capabilities.py
src/trackforge/domain/model/outputs.py
src/trackforge/domain/model/identity.py
src/trackforge/domain/errors.py
tests/domain/{test_ids,test_item,test_claims,test_decisions,test_capabilities,test_feedback,test_errors}.py
```
## Контракты

### ID

- `ItemId`, `MediaId`, `IntakeId`, `ObservationId`, `ClaimId`, `DecisionRecordId`, `DecisionId`, `RunId`, `StepId`, `RenderId`, `BundleId`, `CorrectionId` — `NewType` над `str`.
- `new_uuid7(now_ns: int | None = None) -> str`: RFC 9562 UUIDv7, version=7, variant RFC 4122; без внешней зависимости.
- `parse_id(value: str, kind: type) -> str`: проверяет UUID-формат; ошибка `InvalidId`.
- ID не хэш: одинаковые байты могут иметь разные `ItemId`.

### Source / media / item

```text
SourceRef = LocalPath(path) | Url(url, site_hint?) | TextQuery(text, locale?) | CollectionRef(kind, source, external_id?)
Candidate(id, source_ref, external_id, title, uploader, duration_seconds, formats[], thumbnails[], extra)
MediaObject(id, content_hash, size_bytes, cas_path, container, codec, bitrate, sample_rate, channels, duration_seconds, probe_artifact_id, created_at)
FileInstance(id, media_id, path, role, size_bytes, mtime, intake_id?)
Intake(id, item_id, source_ref, candidate, source_snapshot_artifact_id, downloader, downloader_version, media_id, status, created_at)
Item(id, media_id, primary_intake_id, collection_id?, kind?, status, identity_hypothesis_ids[], decision_record_id?, render_ids[], created_at, updated_at)
```

- `MediaObject.content_hash` — 64 lowercase hex; `size_bytes >= 0`; container из допустимого набора.
- `Intake.status`: `succeeded | failed | cancelled`.
- `ItemStatus`: `discovered | acquired | observed | interpreted | resolved | rendered | published | needs_review | failed | duplicate | archived`.
- Переходы: линейные вперёд; `needs_review`/`failed`/`archived` — ветки; переход в `published` только из `rendered`; назад — только явный reset (не в этом плане).

### Claims / decisions

```text
Provenance(source, method, observed_at, cost?, evidence[])
Claim(id, item_id, subject, subject_id?, field, value, role?, order?, locale?, source, method, confidence, evidence[], derived_from[], status, notes?, created_at)
Decision(field, subject, value, chosen_claim_id?, alternatives[], derived_from[], policy_id, mode, decided_by, confidence, rationale, locked, created_at)
DecisionRecord(id, item_id, version, decisions[], policy_versions[], llm_model?, prompt_version?, input_claim_ids[], created_at)
```

- `subject` — enum из 01-domain §6.2; `None` запрещён.
- `value` — `str | int | float | bool | list[str] | None`; для `None` допустим `unknown`, но `confidence` и rationale обязательны.
- `confidence` ∈ [0, 1]; `rationale` непустой для любого decision.
- `Decision.value is None` разрешён; `chosen_claim_id` может быть `None`; `alternatives` — все рассмотренные claim IDs.
- Если `value` синтезировано (`chosen_claim_id is None` и `value is not None`), `derived_from` непуст.
- `DecisionRecord.version >= 1`; decisions не перезаписываются.
- `FieldPolicy`: `field`, `scope`, `conditions[]`, `strategy`, `priority[]`, `validators[]`, `on_missing`, `version`. Strategy — enum из 07-resolution §6.1.

### Feedback

```text
FeedbackEvent(id, kind, item_id, field?, system_value?, user_value?, scope?, reason?, context_snapshot?, created_at)
Correction(id, item_id, field, system_decision_id, system_value, user_value, user_claim_id?, scope, reason_code, reason_text?, context_snapshot, created_at, applied)
ReviewSession(id, item_ids[], fields[], started_at, finished_at?, corrections[], notes[])
```

- `Correction` — специализация feedback с обязательными system/user values и `reason_code`.
- `CorrectionScope`: `item | source | artist | profile | global`; в v1 исполняется только `item`; широкие scope — `PreferenceRule` (Deferred).
- `context_snapshot` — JSON-совместимый `dict`; без аудио/секретов.

### Capabilities / outputs

- `ArtifactType(namespace, name, schema_version)`; `str` — `<namespace>.<name>.v<version>`; regex `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$`.
- `CapabilityManifest(id, version, kind, requires[], provides[], cost, determinism, cacheable, side_effects, rate_limit, config_schema, license)`.
- `Cost(cpu_seconds, memory_mb, network_calls, money_usd)` — неотрицательные.
- `AnalysisProfile(name, enabled[], disabled[], budget, quality_target, fallback_policy, selection_strategy)`.
- `ResolvedMetadata(item_id, decision_record_id, fields, unknowns[], warnings[])`.
- `Render`/`OutputBundle`/`LibraryEntry` — поля из 01-domain §10; без файловых операций.

### Errors

`TrackForgeError(message, retryable: bool, user_action: str | None, context: dict)`. Подклассы: `ConfigError`, `UnsupportedSource`, `CandidateNotFound`, `AcquisitionFailed`, `ProviderUnavailable`, `RateLimited`, `ResolutionFailed`, `RenderFailed`, `PublishFailed`, `ReviewRequired`, `PluginError`.
## Порядок работ

1. Реализуй ID/UUIDv7 и тесты версии/варианта/сортировки.
2. Реализуй `SourceRef`/`Candidate`/`MediaObject`/`Intake`/`Item` и переходы статусов.
3. Реализуй `Observation`/`Claim`/`Provenance`/`Confidence`; добавь валидаторы инвариантов.
4. Реализуй `FieldPolicy`/`Decision`/`DecisionRecord`; проверь unknown/derived_from/alternatives.
5. Реализуй feedback/review и ограничения scope.
6. Реализуй capabilities/outputs и `ArtifactType` parser.
7. Реализуй ошибки; проверь, что они не содержат секретов и сериализуемы в JSON.
8. Добавь domain тесты; прогони quality gates; закоммить.
## Тесты

- **unit:** UUIDv7 version/variant; 1000 ID сортируются по времени; invalid hash/confidence/status; item transitions; claim без provenance/evidence; decision без rationale; unknown без value; synthesized без derived_from; artifact type parse/render; cost negative; correction scope; error JSON.
- **contract:** `domain` не импортирует pydantic/sqlalchemy/httpx/typer; `lint-imports` зелёный.
- **property-based (если добавлен `hypothesis`):** confidence round-trip; ID parse/render; status transitions не создают цикл.
- **fixtures:** фабрики `make_item`, `make_observation`, `make_claim`, `make_decision_record` в `tests/domain/conftest.py`; без БД.
- **negative:** `subject=""`; `field=""`; `value=None` с `chosen_claim_id`; `derived_from` пуст; `locked` без rationale.

## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/domain -q
uv run lint-imports
uv run python -c "from trackforge.domain.model.ids import new_uuid7; print(new_uuid7())"
```

Ожидается: тесты зелёные; UUIDv7 печатается; импорт домена не тянет внешние адаптеры; `lint-imports` exit 0.

## Acceptance criteria

- [ ] `uv run pytest tests/domain -q` зелёный.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `new_uuid7()` возвращает version 7, variant RFC 4122 и монотонный по времени префикс.
- [ ] Claim без `source`/`method`/`confidence`/`evidence` отклоняется типизированной ошибкой.
- [ ] Decision с синтезированным значением без `derived_from` отклоняется.
- [ ] `unknown` — валидное решение; тест подтверждает `value=None`, rationale и alternatives.
- [ ] `Item` не допускает переход `published → observed` без явного reset.
- [ ] В `src/trackforge/domain` нет импортов `pydantic`, `sqlalchemy`, `httpx`, `typer`, `mutagen`, `essentia`, `torch`, `faster_whisper`, `demucs`.
- [ ] Handoff-файлы существуют; `docs/design/` не изменён.

## Handoff

Следующие планы могут считать готовыми:

- Все доменные типы и ID; `new_uuid7`.
- Инварианты claim/decision/feedback выражены валидаторами и тестами.
- `ArtifactType`/`CapabilityManifest`/`Cost`/`AnalysisProfile`.
- Типизированные ошибки с `retryable`/`user_action`/`context`.
- Domain-тесты и фабрики.

## Stop conditions / decision gates

- D2 не выбран: используй UUIDv7-генератор без зависимости; если пользователь требует `uuid6`/ULID — стоп и зафиксируй.
- Требуется pydantic в домене — стоп: это нарушение слоя; выноси модель в `application/dto`.
- Требуется полный MusicBrainz-граф или multilingual-модель — `Deferred`, не расширяй scope.
- Если инвариант из docs противоречит другому инварианту — стоп, не меняй docs/design, опиши конфликт.
