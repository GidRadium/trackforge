# Plan 14: LLM + ranker

- **Статус:** ready
- **Тип:** Post-MVP
- **Зависит от:** Plan 12
- **Разблокирует:** Plan 15, 16, 17
- **Оценка:** L
- **Риск:** высокий
## Роль

Ты — senior Python engineer, отвечающий за качество на шумных описаниях и ограниченное доверие LLM. Работаешь в `trackforge` после metadata providers. Задача — OpenAI-compatible provider, strict-schema claims, режимы `llm_assisted`/`llm_autopilot`, бюджет/стоимость и гибридный ranker; без ASR/Demucs.
## Цель

Работают режимы `policy`, `llm_assisted`, `llm_autopilot`: LLM получает claims/цитаты, возвращает строго валидированные claims/решения, не выдумывает MBID/ISRC/URL, locks всегда сильнее, стоимость и модель фиксируются. Lexical ranker работает всегда; cross-encoder — опционально через `[ranker]` extra. Preflight-отчёт `artifacts/14-llm-preflight.md` заполнен.

## Контекст и инварианты

- LLM — источник claims/арбитр с strict schema, evidence и `derived_from`, не истина (`ADR-0006`).
- LLM не выдумывает новые MBID/ISRC/URL; только выбирает существующие.
- Locks сильнее policy/LLM; валидаторы обязательны.
- `policy` — default; `llm_assisted` — LLM предлагает, policy валидирует; `llm_autopilot` — LLM решает в разрешённых полях.
- Autopilot только API-модель; локальный 0.5B — **Deferred**, не autopilot.
- Текст — в облако можно; аудио — только opt-in (`Q7`); в этом плане аудио не отправляется.
- `base_url`/`model`/`provider` — в `config.toml`, не секрет; API key — в `.env` через `api_key_env`, без default endpoint.
- Prompt injection: description — недоверенный текст; обрамление + инструкция игнорировать + валидация.
- Cost/budget: `max_cost_usd_per_item`, `max_tokens`, temperature в config.
- Ranker не генерирует текст, только упорядочивает claims; score — evidence.

## Прочитать перед началом

- `docs/design/adr/0006-llm-modes-ranker.md`.
- `docs/design/07-resolution.md` — §6, §9–10.
- `docs/design/06-analysis.md` — §9–10, §14.
- `docs/design/09-interfaces.md` — §3, §7.
- `docs/design/10-operations.md` — §2–3.
- `docs/design/14-phase0-spikes-assessment.md` — §3.7–3.9.
- `docs/plan/artifacts/13-spike-cpu-models.md` (результаты ranker).
- `docs/plan/12-metadata-providers.md` (handoff).

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/integration/test_metadata_resolve.py -q
uv run lint-imports
```

Зелёные. Plan 13 содержит рекомендацию по cross-encoder; если Plan 13 no-go — Plan 14 работает в lexical-only режиме (D8). Нужен API key для live preflight; без него — fake provider и `blocked` в preflight-артефакте.
## Scope

- `application/ports/llm.py`: `LLMProvider`, `LLMRequest`, `LLMResponse`.
- `application/ports/ranking.py`: `ClaimRanker`, `ScoredCandidate`.
- `adapters/analysis/llm/openai_compatible.py` — httpx, JSON schema, retries, cost.
- `application/services/prompts.py` + `src/trackforge/prompts/<task>/v1.md`.
- `application/use_cases/resolve_item.py` — режимы/guardrails.
- `application/services/llm_guardrails.py` — schema, IDs, locks, derived_from.
- `adapters/analysis/ranker/lexical.py` — rapidfuzz/нормализация.
- `adapters/analysis/ranker/cross_encoder.py` — optional `[ranker]` ONNX.
- `application/ports/models.py` + `adapters/models/local.py` — `ModelManager`, registry.
- Migration `0002_llm_cost_model_registry.py` (`model_registry`, `decision_records.llm_cost_usd`, `llm_usage_json`); расширить domain `DecisionRecord` optional cost/usage.
- CLI `models list` (без download).
- Default policies: `llm` claims — источник после authority/source/existing; autopilot decisions проходят validators/locks.
- `rapidfuzz` (MIT) — core для lexical; `[ranker]` extra: `onnxruntime` (MIT), `tokenizers` (Apache-2.0), `numpy` (BSD).
- Preflight report.
## Non-goals

- Локальная LLM 0.5B — **Deferred**.
- ASR/Demucs/lyrics merge — Plans 16/17.
- Feedback export/stats — Plan 15.
- Автоматическое дообучение/PreferenceRule.
- Аудио в облако; web search; Anthropic-native API.
- Генерация текста ranker'ом.
## Deliverables

```text
src/trackforge/application/ports/llm.py
src/trackforge/application/ports/ranking.py
src/trackforge/application/ports/models.py
src/trackforge/application/services/prompts.py
src/trackforge/application/services/llm_guardrails.py
src/trackforge/adapters/analysis/llm/__init__.py
src/trackforge/adapters/analysis/llm/openai_compatible.py
src/trackforge/adapters/analysis/ranker/__init__.py
src/trackforge/adapters/analysis/ranker/lexical.py
src/trackforge/adapters/analysis/ranker/cross_encoder.py
src/trackforge/adapters/models/__init__.py
src/trackforge/adapters/models/local.py
src/trackforge/prompts/extract/v1.md
src/trackforge/prompts/adjudicate/v1.md
src/trackforge/prompts/autopilot/v1.md
migrations/versions/0002_llm_cost_model_registry.py
tests/adapters/llm/test_openai_compatible.py
tests/adapters/ranker/test_lexical.py
tests/application/{test_llm_modes,test_guardrails,test_budget}.py
docs/plan/artifacts/14-llm-preflight.md
```
## Контракты

### LLMProvider

```text
LLMRequest:
  task: extract | adjudicate | autopilot
  schema: dict            # JSON Schema, strict
  claims: list[dict]      # IDs, field, value, source, method, confidence, evidence
  source_snippets: list[dict]  # text + source, недоверенные
  target_fields: list[str]
  max_tokens, temperature

LLMResponse:
  claims: list[LLMClaim]  # field, value, confidence, evidence_claim_ids[], quote, reason
  decisions: list[LLMDecision]  # field, value, chosen_claim_ids[], derived_from[], confidence, reason
  unknown: list[str]
  model, prompt_version, usage {input_tokens, output_tokens}, cost_usd
```

- `LLMProvider.complete(request) -> LLMResponse`; ошибки: `ProviderUnavailable`, `RateLimited`, `LLMInvalidOutput`, `BudgetExceeded`.
- HTTP: `POST {base_url}/chat/completions`; JSON schema/tool call по возможности; если провайдер не поддерживает strict schema — JSON mode + pydantic-валидация.
- Retries: 429/5xx/timeout, max 1 retry (как docs: retry 1), backoff.
- `api_key` только из env по `api_key_env`; не логировать; не класть в артефакт.
- Стоимость: из usage + config price (если заданы `price_in/price_out`); иначе `cost_usd=None` с warning. Не выдумывать цену.
- Модель/промпт/стоимость — в `DecisionRecord` (`llm_model`, `prompt_version`, `llm_cost_usd`, `llm_usage_json`).

### Prompts

- Файлы `vN.md`, версия в имени; prompt version фиксируется в response/DecisionRecord.
- Содержат: задачу, поля, правила (не выдумывать ID/URL, не менять subject, unknown), JSON-схему, 2–3 few-shot без реальных данных.
- Description/source snippet обрамляется тегами и инструктирует игнорировать инструкции внутри.
- Изменение промпта — новая версия; не перезаписывать старую.

### Guardrails

1. Schema validation; unknown fields reject.
2. `value` для ID-полей — только из claims; новый MBID/ISRC/URL → reject claim + warning.
3. `derived_from` обязателен для синтеза.
4. Locks применяются после LLM и всегда побеждают.
5. Hard validators (Plan 07) до записи.
6. Invalid/contradictory → fallback `policy`/review, запись warning; не тихий успех.
7. Cost > budget → `BudgetExceeded`, fallback.
8. Autopilot ограничен `allowed_fields`; по умолчанию title/artist/album/date/genre/cover; IDs — policy only.
9. LLM не получает аудио; только текст.

### Modes

- `policy`: LLM extractor может создать claims (source=`llm`, method=model+prompt), policy решает.
- `llm_assisted`: LLM предлагает decisions; policy валидирует и выбирает; `decided_by=policy+llm`.
- `llm_autopilot`: LLM decisions с guardrails; `decided_by=llm`; locks/validators; fallback при ошибке.
- `manual` — Plan 14 возвращает `UnsupportedMode`; Plan 15 реализует review/corrections, после чего режим становится доступен.

### Ranker

```text
ClaimRanker.rank(query: str, candidates: list[Claim], limit: int) -> list[ScoredCandidate]
```

- Lexical: normalization + `rapidfuzz` token-set/ratio; всегда доступен; score в [0,1].
- Cross-encoder: optional `[ranker]` (`onnxruntime`, `tokenizers`, `numpy`), model path из `[models.ranker]`; если path/model нет — `skipped` warning.
- Cross-encoder не генерирует текст; score — evidence; калибровка не заявляется как вероятность.
- `ModelManager`: lazy load, max 1 heavy model, checksum, license, `--offline`; `model_registry` table.
- `models list` показывает id/version/license/status; download — Plan 16.
## Порядок работ

1. Заполни preflight: один реальный API вызов, schema, cost, latency, injection; если key нет — `blocked`, fake tests.
2. Реализуй `LLMProvider`/prompts/JSON schema.
3. Реализуй guardrails и режимы; добавь поля/миграцию стоимости.
4. Реализуй lexical ranker; optional cross-encoder/ModelManager.
5. Добавь тесты fake HTTP/guardrails/budget.
6. Добавь CLI `models list` (без скачивания) и doctor model checks.
7. Прогони quality gates; закоммить.
## Тесты

- **unit:** JSON schema; invalid JSON; unknown field; new MBID reject; derived_from; locks; budget; cost calculation; prompt version; lexical ranking; cross-encoder skip.
- **contract:** `LLMProvider`/`ClaimRanker` fake; `DecisionRecord` fields; `model_registry`.
- **integration (respx):** fake OpenAI-compatible server: extract/adjudicate/autopilot; invalid output fallback; 429 retry; cost recorded; no network.
- **preflight:** real API optional; artifact; no secrets.
- **fixtures:** claims table, noisy description, prompt-injection text, fake LLM responses.
- **negative:** 401/403; schema failure; hallucinated URL/MBID; lock overwrite attempt; autopilot on ID field; budget exceeded; missing base_url/model.
## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/adapters/llm tests/adapters/ranker tests/application/test_llm_modes.py tests/application/test_guardrails.py tests/application/test_budget.py -q
uv run lint-imports
uv run trackforge models list --json
test -s docs/plan/artifacts/14-llm-preflight.md
```

Ожидается: fake-тесты зелёные; preflight заполнен (или `blocked` без ключа); `models list` не скачивает модели.
## Acceptance criteria

- [ ] Fake-тесты LLM/guardrails/budget зелёные; live preflight заполнен или `blocked`.
- [ ] LLM не может создать новый MBID/ISRC/URL; тест подтверждает reject.
- [ ] Locks побеждают LLM во всех режимах.
- [ ] `llm_assisted` валидирует LLM policy; `llm_autopilot` падает в fallback при invalid output.
- [ ] `DecisionRecord` содержит модель, prompt version, input claim IDs, cost/usage (или null + warning).
- [ ] Budget превышение не делает вид, что всё хорошо; fallback/`BudgetExceeded`.
- [ ] Lexical ranker работает без extra; cross-encoder — только при наличии модели; score — evidence.
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] `docs/design/` не изменён; секретов в артефакте нет.
## Handoff

Следующие планы могут считать готовыми:

- `LLMProvider`/prompts/guardrails; режимы `policy`/`llm_assisted`/`llm_autopilot`.
- Lexical ranker + optional cross-encoder/ModelManager/model_registry.
- Cost/usage в DecisionRecord; budget.
- Preflight-отчёт.
- CLI `models list`.

## Stop conditions / decision gates

- D8 не выбран — lexical only + warning; не добавляй модель без лицензии.
- Нет API key — preflight `blocked`, fake tests обязательны; не выдумывай cost/accuracy.
- Нужен локальный 0.5B/7B — Deferred, не расширяй.
- Нужно отправить аудио — стоп; Q7 требует явного opt-in, не в этом плане.
- Провайдер не поддерживает strict schema — используй JSON mode + pydantic; если и это невозможно, зафиксируй degraded.
- Нужен `openai` SDK — не обязателен; используй `httpx`, не добавляй лишнюю зависимость.
