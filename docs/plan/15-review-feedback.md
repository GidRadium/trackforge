# Plan 15: Review + feedback

- **Статус:** ready
- **Тип:** Post-MVP
- **Зависит от:** Plan 09, Plan 14
- **Разблокирует:** Plan 19
- **Оценка:** M
- **Риск:** средний

## Роль

Ты — senior Python engineer, отвечающий за обратную связь пользователя. Работаешь в `trackforge` после LLM/ranker. Задача — review, corrections, locks, review sessions и экспорт/статистика; без автообучения и без широких PreferenceRule.

## Цель

Пользователь может в `trackforge review` принять/изменить/залочить/снять поле, увидеть альтернативы и rationale; каждая правка сохраняется как `Correction` + user claim + lock + append-only событие с `context_snapshot`. `feedback export` и `feedback stats` дают JSONL/CSV и correction rate; повторный resolve уважает lock.

## Контекст и инварианты

- `Correction` создаётся только по явным правкам в TrackForge (`Q19`); внешние правки/неявные сигналы — Deferred.
- Scope по умолчанию `item`; повышение до `source/artist/profile/global` — явное, но в v1 не исполняется (`PreferenceRule` Deferred).
- Lock сильнее policy/LLM; lock не удаляет claims; снятие — feedback-событие.
- Feedback append-only; `feedback/events.jsonl` + DB; corrections snapshot.
- `context_snapshot`: claims/alternatives/versions/system/user values; без аудио, cookies, секретов.
- Никакого автоматического обучения/изменения политик по одной правке.
- `DecisionRecord`/claims не перезаписываются; correction создаёт новый claim/decision.
- Review не должен тихо менять output; после review нужен re-render/publish (явно).

## Прочитать перед началом

- `docs/design/01-domain-model.md` — §8.
- `docs/design/07-resolution.md` — §10–11.
- `docs/design/04-storage.md` — §9.
- `docs/design/09-interfaces.md` — §2.3, §3.5.
- `docs/design/11-quality.md` — §4.
- `src/trackforge/application/use_cases/review_item.py` (Plan 09), `resolve_item.py`.
- `docs/plan/09-publish-cli-local-mvp.md`, `docs/plan/14-llm-ranker.md` (handoff).

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/cli/test_review.py tests/integration/test_local_e2e.py -q
uv run lint-imports
```

Зелёные. Basic review/lock из Plan 09 работает; LLM-режимы из Plan 14 могут быть недоступны без ключа, но policy работает.

## Scope

- `application/use_cases/review_item.py` — расширить: sessions, accept/edit/lock/unlock/unknown/why/skip, `--json`.
- `application/use_cases/export_feedback.py`.
- `application/use_cases/feedback_stats.py`.
- `application/services/context_snapshot.py`.
- `manual` mode: resolve/re-resolve с user corrections; `decided_by=user`, locks/validators обязательны.
- `adapters/persistence/feedback/jsonl.py` — append-only `events.jsonl` + correction files.
- CLI `review`, `feedback export`, `feedback stats`.
- `FeedbackRepository` methods для stats.
- Tests unit/integration/CLI.

## Non-goals

- `PreferenceRule` исполнение/автообучение/калибровка политик.
- ExternalEdit/scan/неявные сигналы.
- Web/GUI review.
- Удаление feedback (только явное, позже).
- Мультиязычный UI.
- Полный evaluation harness.

## Deliverables

```text
src/trackforge/application/use_cases/review_item.py
src/trackforge/application/use_cases/export_feedback.py
src/trackforge/application/use_cases/feedback_stats.py
src/trackforge/application/services/context_snapshot.py
src/trackforge/adapters/persistence/feedback/__init__.py
src/trackforge/adapters/persistence/feedback/jsonl.py
src/trackforge/interfaces/cli/commands/feedback.py
tests/application/test_review_corrections.py
tests/application/test_feedback_export.py
tests/application/test_feedback_stats.py
tests/cli/test_feedback.py
```

## Контракты

### Review actions

```text
ReviewAction = accept | edit | lock | unlock | unknown | why | skip

ReviewRequest:
  item_id
  field?
  action
  value?          # для edit
  reason_code?    # wrong_title | wrong_artist | wrong_album | wrong_cover |
                  # wrong_type | wrong_lyrics | wrong_date | other
  reason_text?
  scope: item = "item"   # v1: только item
  lock: bool = False

ReviewResult:
  item_id, decisions[], correction?, lock?, warnings[]
```

- `accept` — approval event, решения не меняются.
- `edit` — user claim (`source=user`, `method=manual_override`, `confidence=1.0`), correction, feedback event; `lock=true` — upsert lock; re-resolve.
- `lock` — upsert lock на текущее/заданное значение; событие `lock`.
- `unlock` — удалить lock, событие `unlock`; не удалять claims.
- `unknown` — decision value=None + reason; correction/event.
- `why` — `ExplanationView` (Plan 07), без mutation.
- `skip` — ничего не менять, review note.
- `scope != item` → `UnsupportedScope`; в v1 не создавать PreferenceRule.
- Review не рендерит и не публикует; CLI сообщает, что нужен `render`/`publish` (или `import --force`).

### ReviewSession

- `ReviewSession` создаётся на `review` с `item_ids`, `fields`, `started_at`; `finished_at` при завершении; `notes`.
- `ReviewSessionItem` связывает item; corrections ссылаются на session.
- `review --json` возвращает `ReviewView` с decisions/alternatives/actions.
- Interactive actions через Typer/rich: `[a]ccept [e]dit [l]ock [u]nknown [w]hy [s]kip`.

### Feedback storage

- `feedback/events.jsonl`: append-only, one JSON object per line, UTF-8, canonical JSON; fsync.
- `feedback/corrections/<correction-id>.json`: полный snapshot контекста.
- DB `feedback_events` — индекс для stats; запись и JSONL должны быть согласованы (DB commit + append; при расхождении — warning/repair, не молча).
- `context_snapshot`: `system_value`, `user_value`, `claims[]` (id/field/value/source/method/confidence), `alternatives[]`, `policy_versions`, `model/prompt`, `source_ref`; без аудио/секретов.
- Ошибки JSONL append → `FeedbackStorageError`; не терять событие молча.

### Export / stats

```text
feedback export [--format jsonl|csv] [--output path] [--item] [--field] [--since]
feedback stats [--group-by field|source|method] [--json]
```

- Export читает JSONL/DB, redacts секреты, не включает аудио; CSV — стабильные колонки.
- Stats: correction count/rate по полям/источникам/методам; total decisions; review rate; unknown rate. Не заявлять accuracy без ground truth.
- `--json` — один объект (D6).
- `feedback export` не отправляет данные в сеть.

## Порядок работ

1. Расширь доменные/репозиторные методы для correction/session.
2. Реализуй context snapshot и JSONL append.
3. Реализуй review actions и re-resolve.
4. Реализуй export/stats.
5. Добавь CLI и `--json`.
6. Добавь тесты lock/correction/export/stats/privacy.
7. Прогони quality gates; закоммить.

## Тесты

- **unit:** correction fields/reason; user claim; lock upsert/delete; scope validation; snapshot redaction; JSONL append; stats calculations; CSV columns.
- **contract:** `FeedbackRepository` fake; JSONL/DB consistency; `ReviewView` schema.
- **integration:** local item → review edit title → correction + lock + event → re-resolve → decision locked; `feedback export` JSONL parses; `feedback stats` counts; DB/JSONL both.
- **fixtures:** item with claims/decisions; existing lock; review session; feedback events.
- **negative:** invalid field; empty value; scope=global; missing item; JSONL write failure; duplicate correction id; secret string in context redacted; delete feedback attempt.

## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/application/test_review_corrections.py tests/application/test_feedback_export.py tests/application/test_feedback_stats.py tests/cli/test_feedback.py -q
uv run lint-imports
uv run trackforge review <item-id> --field title --set "New Title" --lock --json
uv run trackforge feedback export --format jsonl --output /tmp/tf-feedback.jsonl
uv run trackforge feedback stats --json
```

Ожидается: correction/lock сохранены; export JSONL валиден; stats считает correction rate; повторный resolve уважает lock.

## Acceptance criteria

- [ ] Все тесты review/feedback/export/stats зелёные.
- [ ] `review --set ... --lock` создаёт user claim, correction, feedback event и lock; `explain` показывает новое решение.
- [ ] `review --unlock` снимает lock, но не удаляет claims/corrections.
- [ ] `feedback export` JSONL/CSV валиден; `feedback stats` считает correction rate по field/source/method.
- [ ] `context_snapshot` содержит claims/alternatives/versions, но не аудио/секреты.
- [ ] Scope шире `item` отклоняется с понятной ошибкой; PreferenceRule не создаётся.
- [ ] Feedback append-only; повторный review не перезаписывает историю.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён.

## Handoff

Следующие планы могут считать готовыми:

- Review sessions/corrections/locks/unlock.
- Feedback JSONL + DB + corrections snapshots.
- `feedback export`/`stats`.
- Context snapshot и privacy redaction.
- Тесты, что lock побеждает при re-resolve.

## Stop conditions / decision gates

- Нужен `PreferenceRule`/автообучение — Deferred; не расширяй scope.
- Нужны external edits/scan — Deferred.
- Нужно удалить feedback — не в v1; только explicit future.
- JSONL и DB расходятся — стоп, не «почини» удалением истории; опиши repair.
- Нужен scope шире item — Plan 15 фиксирует `UnsupportedScope`, не делает вид, что применил.
