# Architecture Decision Records

> Короткие записи о существенных решениях: контекст → варианты → решение → последствия.
> Статусы: `proposed`, `accepted`, `superseded`, `deprecated`.

## Индекс

| ADR | Решение | Статус |
|---|---|---|
| [0001](0001-modular-monolith-hexagonal.md) | Модульный монолит + гексагональная архитектура | accepted |
| [0002](0002-claims-decisions.md) | Claims/decisions вместо одного dataclass метаданных | accepted |
| [0003](0003-content-addressed-storage.md) | Content-addressed storage + SHA-256 | accepted |
| [0004](0004-capability-dag-pipeline.md) | DAG возможностей + кэш/resume | accepted |
| [0005](0005-gpl-license-boundary.md) | GPL-3.0-or-later + license boundary | accepted |
| [0006](0006-llm-modes-ranker.md) | LLM-режимы + гибридный ranker | accepted |

## Шаблон

```markdown
# ADR-XXXX: Название

- **Статус:** proposed | accepted | superseded | deprecated
- **Дата:** YYYY-MM-DD
- **Контекст:** ...
- **Решение:** ...
- **Альтернативы:** ...
- **Последствия:** ...
```
