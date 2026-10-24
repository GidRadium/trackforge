# ADR-0006: LLM-режимы + гибридный ranker

- **Статус:** accepted
- **Дата:** 2026-10-22

## Контекст

LLM хорошо разбирает шумные описания, но дорог, нестабилен и склонен выдумывать. Локальные LLM на CPU слабее. Нужен fallback, который выбирает из конечного набора, а не генерирует.

## Решение

- Режимы: `policy` (default), `llm_assisted`, `llm_autopilot`, `manual`.
- LLM — источник claims/арбитр с strict schema, evidence и `derived_from`; не выдумывает MBID/ISRC/URL.
- Пользовательские locks всегда сильнее.
- Fallback: API → локальная 0.5B (только classification/ranking) → cross-encoder → lexical → rules/unknown.
- Ranker не генерирует текст; только упорядочивает кандидатов.
- Все решения/модели/промпты/стоимость — в `DecisionRecord`.

## Альтернативы

1. LLM-only — дорого/необъяснимо.
2. Rules-only — дешево, но плохо на шумных описаниях.
3. Local-only — приватно, но качество/скорость на CPU слабые.

## Последствия

- Нужны prompt versioning/evaluation, cost tracking, guardrails.
- Дополнительные зависимости (cross-encoder, ONNX; 0.5B fallback — опционально).
- Деградация видима; пользователь может выбрать режим.
