# ADR-0004: DAG возможностей + кэш/resume

- **Статус:** accepted
- **Дата:** 2026-10-22

## Контекст

Анализы дорогие (ASR, Demucs, LLM), источники/провайдеры ненадёжны, пользователь хочет добавлять новые анализаторы и не повторять уже сделанное. Линейный `resolve_metadata()` не даёт resume/выбора/деградации.

## Решение

- Каждый шаг — capability с манифестом `requires/provides/cost/version/determinism`.
- Планировщик строит DAG от желаемых outputs и профиля.
- Provider chains дают альтернативы (например, lyrics: LRCLIB → Genius → ASR).
- Кэш по `hash(inputs + capability/version + params + relevant config + model/prompt)`.
- Исполнитель поддерживает retry, budget, cancellation, degraded/skipped статусы.

## Альтернативы

1. Захардкоженный pipeline — проще, но не расширяется и не кэширует.
2. Готовый workflow engine (Prefect/Celery) — тяжёлая инфраструктура для локального инструмента.
3. Отдельные скрипты на анализатор — нет общего resume/прогресса.

## Последствия

- Появляется планировщик/executor — больше кода.
- Контракты capability нужно версионировать.
- Добавление анализатора/источника становится предсказуемым.
- Кэш/resume работают единообразно.
