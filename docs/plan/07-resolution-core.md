# Plan 07: Resolution core

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 05, 06
- **Разблокирует:** Plan 08, 09
- **Оценка:** L
- **Риск:** высокий

## Роль

Ты — senior Python engineer, отвечающий за ядро продукта: превращение наблюдений в claims и решений. Работаешь в `trackforge` после local ingest. Задача — policy engine, нормализация, locks, unknown и explain; без LLM, сети и записи файлов.

## Цель

Появляется сценарий «observations → claims → `DecisionRecord`»: для локального файла строятся claims из существующих тегов, пути и типа контента, применяются политики и locks, создаётся объяснимый `DecisionRecord`; `explain` показывает выбранное значение, альтернативы и rationale.

## Контекст и инварианты

- Резолвер не ходит в сеть и не читает аудио; работает с claims, политиками, locks (`07-resolution.md` §1).
- Порядок: lock → valid → normalize → group → strategy → validate → review/unknown.
- Нормализация только для сравнения; исходные значения сохраняются.
- `unknown` — легальное решение; нельзя подставлять выдуманное значение.
- Claims append-only; decision record append-only; locks сильнее policy.
- `title`/`artist` — desired, не required; отсутствие evidence → `unknown`.
- `subject` не смешивается: `item.title`, `recording.title`, `derivative.title` — разные claims.
- Никаких MBID/ISRC без authority; Plan 07 не создаёт внешние ID.
- LLM/ranker — Plan 14; здесь только `policy`-стратегии.

## Прочитать перед началом

- `docs/design/07-resolution.md` — §1–8, §10–12.
- `docs/design/01-domain-model.md` — §6–7, §9.6.
- `docs/design/06-analysis.md` — §3, §8.1–8.3.
- `docs/design/08-metadata.md` — §2, §6.
- `docs/design/03-pipeline.md` — §2.5–2.6, §7.
- `src/trackforge/domain/model/claims.py`, `decisions.py`, `outputs.py`.
- `src/trackforge/application/use_cases/observe_item.py`.
- `docs/plan/06-local-ingest-probe.md` (handoff).

## Предусловия

```bash
uv sync --extra tagging
uv run ruff check .
uv run mypy src
uv run pytest tests/adapters tests/application/test_local_ingest.py -q
uv run lint-imports
```

Зелёные. `initial.tags.v1`, `audio.probe.v1`, `path.structure.v1` доступны; `locks`/claims/decision репозитории — Plan 04.
## Scope

- `domain/policies/normalization.py`: строки, артисты, даты, BPM/key.
- `domain/policies/validators.py`: полевые и cross-field валидаторы.
- `domain/policies/registry.py`: default `FieldPolicy` с версиями.
- `domain/policies/strategies.py`: `user_lock`, `source_priority`, `keep_existing`, `derive`, `review`, `unknown`, `consensus` (базовая).
- `application/use_cases/interpret_item.py`: claims из observations.
- `application/use_cases/resolve_item.py`: policy → `DecisionRecord`.
- `application/use_cases/explain_item.py`.
- `application/dto/decisions.py`: `ResolvedMetadata`, `ExplanationView`.
- Interpreters как capabilities: `initial_tags_interpreter`, `kind_classifier`, `derivative_detector`, `identity_resolver`.
- Тесты domain/policies и application.
## Non-goals

- LLM/ranker/autopilot/manual review UI.
- MusicBrainz/AcoustID/authority claims (Plan 12).
- Feedback export/PreferenceRule (Plan 15).
- Render/tag writer/publish.
- Полная нормализация транслитерации и multilingual titles — Deferred.
- Identity-таблицы; identity в v1 — artifact-only.
## Deliverables

```text
src/trackforge/domain/policies/__init__.py
src/trackforge/domain/policies/normalization.py
src/trackforge/domain/policies/validators.py
src/trackforge/domain/policies/strategies.py
src/trackforge/domain/policies/registry.py
src/trackforge/application/use_cases/interpret_item.py
src/trackforge/application/use_cases/resolve_item.py
src/trackforge/application/use_cases/explain_item.py
src/trackforge/application/dto/decisions.py
tests/domain/policies/{test_normalization,test_strategies,test_validators}.py
tests/application/{test_interpret,test_resolve,test_explain}.py
```
## Контракты

### Normalization

- Строки: NFKC, trim, collapse whitespace; casefold только для сравнения.
- `feat.`/`ft.`/`featuring`, `&`/`and`, `vs.`/`versus`; `’` → `'`.
- `Artist - Topic` → `Artist`; `VEVO` не артист; source сохраняется.
- Скобки и суффиксы (`Official Video`, `Lyrics`, `HD`, `4K`) не удаляются автоматически; помечаются как `source_noise`.
- Артисты: разделение по `,`, `&`, `feat.`, `ft.`, `x`, `×` — эвристика; роли `main/featured/remixer/composer/lyricist/producer/albumartist`.
- Даты: ISO `YYYY`, `YYYY-MM`, `YYYY-MM-DD`; не угадывать день/месяц.
- BPM: число, half/double без «исправления»; key: `C`, `C#`, `Db`, `Am`, `F#m`; исходное значение сохраняется.

### Validators

- Hard: пустой title/artist, path separators, MBID/ISRC format, confidence вне [0,1], track/disc ≤ 0, date в будущем (кроме явного анонса).
- Soft: подозрительная длина/жанр/дата → warning, не reject.
- Cross-field: `albumartist` при `album`; `track <= tracktotal`; `disc <= disctotal`; `date >= originaldate`, если оба; `compilation` согласован.
- Если все claims поля невалидны → `unknown`/review.

### Policies

```text
FieldPolicy:
  field, scope, conditions[], strategy, priority[], validators[], on_missing, version
```

Default policies:

| Поле | Стратегия v1 | Priority |
|---|---|---|
| title | source_priority + derive | user_lock → item.title → path.title → unknown |
| artist | source_priority | user_lock → item.artist → path.artist → unknown |
| album | source_priority | user_lock → item.album → unknown |
| date | source_priority | user_lock → item.date → unknown |
| genre | source_priority | user_lock → item.genre → unknown |
| track/disc | source_priority | user_lock → item.track/disc → unknown |
| comment/source | keep_existing + source | user_lock → item.comment → source_url/source_id → unknown |
| bpm/key | source_priority | user_lock → item.bpm/key → unknown |
| cover | source_priority | user_lock → item.artwork → unknown |
| lyrics | source_priority | user_lock → item.lyrics → unknown |
| IDs | authority only | user_lock → external claim → unknown (Plan 07: всегда unknown) |

- `derive` для title: если `derivative` claim содержит transformation и title не содержит qualifier — добавить `(<Qualifier>)`; если содержит — не дублировать.
- `consensus` — базовая: два независимых claim с одинаковым нормализованным значением повышают confidence; в Plan 07 источники в основном зависимые, поэтому использовать осторожно.
- `on_missing`: `unknown` по умолчанию; `review` для полей, помеченных политикой.

### Interpreters

```text
initial_tags_interpreter: requires [initial.tags.v1, path.structure.v1] -> claims.v1
kind_classifier: requires [audio.probe.v1, source.metadata.v1?] -> claims.v1 (kind)
derivative_detector: requires [initial.tags.v1, path.structure.v1, audio.probe.v1] -> claims.v1 (derivative)
identity_resolver: requires [claims.v1, audio.probe.v1] -> identity.hypotheses.v1
```

- `initial_tags_interpreter`: subject=`item`, `source=initial_tags`, `method=mutagen_tag_reader/v1`, confidence ≤0.4; MBID claims — только если валидны, но Plan 07 не использует их для authority.
- `kind_classifier`: `music` по умолчанию для аудио; `audiobook`/`podcast` только при явных evidence (длительность/главы/теги); confidence.
- `derivative_detector`: правила по title/description/tags: `nightcore`, `sped up`, `slowed`, `reverb`, `8d`, `remix`, `cover`, `instrumental`, `acapella`; без tempo-анализа (Plan 17). Не утверждать derivative без evidence.
- `identity_resolver`: при отсутствии внешних ID возвращает artifact с `hypotheses: []`, `status: "ambiguous"`, warning `no_identity_evidence`; claims `recording.*` не создаются.
- Интерпретатор не выбирает финал; он создаёт claims с provenance.

### Resolve / explain

```text
ResolveItem.resolve(item_id, mode="policy", policy_overrides?) -> ResolvedMetadata
ExplainItem.explain(item_id, field) -> ExplanationView
```

- `ResolveItem` читает claims из БД/artifact, locks, применяет политики, валидирует, создаёт `DecisionRecord` и `Decision[]`, сохраняет, обновляет item status `resolved`.
- `DecisionRecord.id`/version; `input_claim_ids`; `policy_versions`; `alternatives` включают отклонённые claim IDs.
- Lock: `Decision.locked=True`, value из `locks`, chosen user claim если есть; lock не удаляет старые claims.
- `ExplanationView`: field, decision, mode, policy id/version, chosen claim, derived_from, alternatives (source/confidence/reason), rationale, warnings.
- `policy` mode only; `llm_assisted`/`llm_autopilot`/`manual` → `UnsupportedMode` until Plan 14/15.
- `unknown`: `value=None`, `confidence`, `rationale`, alternatives.
## Порядок работ

1. Реализуй нормализацию и тесты строк/артистов/дат/BPM/key.
2. Реализуй validators и default policy registry.
3. Реализуй strategies для source_priority/keep_existing/derive/unknown; user_lock читает lock.
4. Реализуй interpreters как capabilities и registrations.
5. Реализуй `ResolveItem`, сохранение record/decisions, обновление item.
6. Реализуй `ExplainItem`.
7. Добавь тесты на locks, unknown, derive, noise, alternatives.
8. Прогони quality gates; закоммить.
## Тесты

- **unit:** normalization; validators; policy selection; lock wins; unknown; derive qualifier без дубля; alternatives include rejected; consensus boost; `Topic`/`VEVO`; source noise.
- **contract:** interpreters возвращают `claims.v1`/`identity.hypotheses.v1`; Domain policies не импортируют I/O; `ResolveItem` не вызывает сеть/файлы.
- **integration:** local file fixtures → interpret → resolve → DecisionRecord; explain title; lock after resolve; re-resolve creates new record.
- **fixtures:** `make_observation`, `make_claim`, `make_lock`, `make_policy`; local MP3 with dirty tags; derivative title fixture.
- **negative:** claim without provenance; confidence >1; invalid date; lock on unknown field; policy cycle? (нет); all claims invalid → unknown; unsupported mode.
## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/domain/policies tests/application/test_interpret.py tests/application/test_resolve.py tests/application/test_explain.py -q
uv run lint-imports
```

Ожидается: decisions объяснимы; lock побеждает; unknown валиден; derivative title собирается без дубля; повторный resolve создаёт новый record.
## Acceptance criteria

- [ ] Все policy-тесты и resolve/explain тесты зелёные.
- [ ] `ResolveItem` создаёт `DecisionRecord` с version, policy_versions, input_claim_ids и decisions.
- [ ] `Decision` содержит alternatives и rationale; неизвестное поле даёт `unknown`, а не выдуманное значение.
- [ ] Lock на поле побеждает policy и остаётся `locked=True`; старые claims сохранены.
- [ ] `explain` показывает chosen claim, alternatives, policy и rationale.
- [ ] Derivative title `Song` + `nightcore` → `Song (Nightcore)`; повторный qualifier не дублируется.
- [ ] `llm_autopilot`/`manual` возвращают `UnsupportedMode`, а не тихо деградируют.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] В `domain/policies` нет импортов БД/HTTP/CLI; `docs/design/` не изменён.
## Handoff

Следующие планы могут считать готовыми:

- Нормализация/валидаторы/default policies/strategies.
- Interpreters и `claims.v1`/`identity.hypotheses.v1`.
- `ResolveItem`/`ExplainItem`/`ResolvedMetadata`/`ExplanationView`.
- Locks применяются; `DecisionRecord` сохраняется.
- Тесты policy/derive/unknown/lock.

## Stop conditions / decision gates

- Нужна authority-логика MBID/ISRC — Plan 12; здесь только `unknown`.
- Нужен LLM/ranker — Plan 14; не добавляй сеть.
- Нужна широкая scope (`source/artist/profile/global`) — Plan 15/PreferenceRule; lock в v1 только item/field.
- Если policy требует признать derivative без evidence — стоп: это нарушает инвариант.
- Если `DecisionRecord` нельзя сохранить без изменения схемы Plan 04 — стоп и предложи миграцию, не меняй docs/design.
