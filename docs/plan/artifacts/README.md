# Артефакты планов

Здесь лежат результаты spike/preflight/benchmark и release evidence. Артефакт — это **данные**, а не план: команды, окружение, таблицы, выводы, go/no-go.

## Правила

- Один артефакт — один план (`NN-<slug>.md`).
- Заполняется исполнителем после реального запуска; нельзя выдумывать числа.
- Секреты, cookies, токены, полные URL с query-токенами не попадают в артефакт.
- Если ресурс недоступен — статус `blocked` с причиной, а не фиктивные данные.
- Артефакт не заменяет acceptance плана; он фиксирует evidence.
- `pending` в шаблоне означает, что spike ещё не выполнен; после выполнения статус — `go`, `no-go` или `blocked`.

## Индекс

| Артефакт | План | Назначение |
|---|---|---|
| [`10-spike-ytdlp.md`](10-spike-ytdlp.md) | 10 | yt-dlp/Deno/cookies: success rate и go/no-go |
| [`13-spike-cpu-models.md`](13-spike-cpu-models.md) | 13 | Whisper/Demucs/cross-encoder: RTF/RAM/go/no-go |
| [`14-llm-preflight.md`](14-llm-preflight.md) | 14 | LLM schema/cost/latency/injection |
| [`16-asr-benchmark.md`](16-asr-benchmark.md) | 16 | Фактический ASR benchmark после реализации |
| [`20-release-report.md`](20-release-report.md) | 20 | Release evidence и known limitations |
