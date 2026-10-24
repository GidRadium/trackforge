# Plan 10: Phase 0 spike — yt-dlp/Deno/cookies

- **Статус:** conditional
- **Тип:** Spike
- **Зависит от:** Plan 01
- **Разблокирует:** Plan 11
- **Оценка:** M
- **Риск:** высокий

## Роль

Ты — senior engineer, проводящий технический spike. Работаешь в `trackforge`, но **не пишешь production-код** и не меняешь `src/`. Задача — измерить реальную работоспособность `yt-dlp` на целевом Linux/CPU с cookies и JS runtime и дать go/no-go.

## Цель

Заполнен `docs/plan/artifacts/10-spike-ytdlp.md`: версии инструментов, 5–10 реальных URL (redacted), success rate, форматы, ошибки, роль cookies/Deno, время скачивания. Явный go/no-go для Plan 11.

## Контекст и инварианты

- `yt-dlp` вызывается subprocess, без shell; JSON-вывод стабильнее Python API (`05-sources.md` §4.1).
- YouTube может требовать JS runtime: Deno ≥ 2.3.0, `yt-dlp-ejs`, актуальный `yt-dlp` (`14-phase0-spikes-assessment.md` §3.1).
- Cookies — Netscape-файлы `0600`, по сайтам; не логировать содержимое (`05-sources.md` §4.5).
- DRM/платный контент не поддерживается; ошибка `DrmProtected`.
- Cookie/PO token/bot-check — операционный риск; успех зависит от ссылок, региона, аккаунта.
- Spike не меняет архитектуру; если no-go, локальный MVP всё равно возможен (`14-phase0-spikes-assessment.md` §6).
- Никаких реальных cookies/URL с токенами в артефакте; только redacted placeholder.

## Прочитать перед началом

- `docs/design/05-sources.md` — §4, §8–9.
- `docs/design/14-phase0-spikes-assessment.md` — §3.1, §6–8.
- `docs/design/10-operations.md` — §3, §8.4.
- `docs/design/03-pipeline.md` — §2.2, §12.
- `docs/plan/artifacts/10-spike-ytdlp.md` (шаблон).
- `docs/plan/01-bootstrap.md` (какие инструменты уже есть).

## Предусловия

```bash
uv --version
yt-dlp --version
deno --version
ffprobe -version
test -d docs/plan/artifacts
```

Если `yt-dlp`/`deno` нет — установи в user-space (`uv tool install yt-dlp` / официальный installer Deno) только с разрешения пользователя; если разрешения нет — стоп и запиши blocker.

Нужны 5–10 своих ссылок (YouTube/YouTube Music/SoundCloud/Bandcamp и т.п.) и, при необходимости, cookies. Без ссылок — статус `blocked`; не выдумывай результаты.

## Scope

- Проверить версии и наличие EJS/Deno.
- Прогнать metadata-only (`--dump-single-json --no-download`) на 5–10 URL.
- Скачать audio-only в `/tmp` на 3–5 URL; замерить время/размер/формат.
- Проверить cookies: без cookies / с cookies; ошибки login/403/429.
- Проверить bot-check/JS runtime; зафиксировать, что именно помогло.
- Проверить, что cookies не попадают в stdout/stderr/JSON-артефакт.
- Заполнить артефакт и дать go/no-go.

## Non-goals

- Писать `YtDlpSource`/`YtDlpAcquirer` (Plan 11).
- Скачивать плейлисты (Plan 18).
- Обходить DRM/платный доступ.
- Хранить cookies/URL с токенами в репозитории.
- Настраивать CI/live-тесты.

## Deliverables

```text
docs/plan/artifacts/10-spike-ytdlp.md   # заполненный отчёт
```

Никаких изменений в `src/`, `tests/`, `pyproject.toml`, `docs/design/`.

## Контракты (формат отчёта)

Артефакт обязан содержать:

```text
1. Дата, Linux distro, CPU, RAM, версии yt-dlp/Deno/ffmpeg, наличие yt-dlp-ejs.
2. Таблица URL: id/redacted URL, extractor, metadata OK, audio OK, container/codec,
   duration, size, download time, cookies (yes/no), JS runtime (yes/no), error.
3. Success rate: metadata, audio, end-to-end.
4. Ошибки: тип, сообщение, retryable, что помогло.
5. Cookies: путь/наличие, 0600, не попали ли в лог/JSON.
6. Bot-check/JS: воспроизводимость, нужен ли Deno.
7. Go/no-go по критериям.
8. Решение для Plan 11: proceed / defer / blocked, с причиной.
```

Критерии go/no-go (`14-phase0-spikes-assessment.md` §7):

- Go: ≥4/5 URL дают metadata + audio; ошибки login/rate-limit/DRM/geo понятны; cookies не текут.
- No-go: <3/5, постоянные bot-check/403, cookies обязательны и нестабильны.
- Blocked: нет URL/cookies/разрешения на установку.

## Порядок работ

1. Зафиксируй окружение и версии.
2. Проверь `yt-dlp --dump-single-json --no-download` на одном URL без cookies.
3. Повтори на 5–10 URL; запиши ошибки.
4. Скачай 3–5 URL в `/tmp/tf-spike/` с `-f "bestaudio/best"`; замерь `time`/размер.
5. Повтори проблемные URL с cookies/Deno; зафиксируй разницу.
6. Проверь, что cookies не попали в вывод: `rg` по отчёту/логам на `cookie|authorization|token` (значения не печатать).
7. Заполни артефакт; сделай вывод.
8. Удали временные файлы; cookies не коммить.

## Тесты

Spike не добавляет unit-тестов. Проверки:

- commands exit codes;
- JSON parsing `yt-dlp --dump-single-json`;
- `ffprobe` на скачанном файле;
- отсутствие cookies в артефакте (`rg`).

## Проверка

```bash
yt-dlp --version
deno --version
yt-dlp --dump-single-json --no-download --no-warnings "<URL>" > /tmp/tf-spike/meta.json
python3 -c "import json; d=json.load(open('/tmp/tf-spike/meta.json')); print(d.get('extractor'), d.get('title'), d.get('duration'))"
yt-dlp -f "bestaudio/best" --no-playlist -o "/tmp/tf-spike/%(id)s.%(ext)s" "<URL>"
ffprobe -v error -show_format -show_streams -of json /tmp/tf-spike/<file>
rg -n -i "cookie|authorization|bearer|token" docs/plan/artifacts/10-spike-ytdlp.md || true
```

Ожидается: metadata/audio получены или ошибка классифицирована; артефакт заполнен; `rg` не находит значений секретов (только слово `cookies` в описании допустимо, значений — нет).

## Acceptance criteria

- [ ] Отчёт `docs/plan/artifacts/10-spike-ytdlp.md` заполнен по формату.
- [ ] Проверено 5–10 URL (или зафиксирован `blocked` с причиной).
- [ ] Есть таблица success rate и хотя бы 3 скачанных файла (если go).
- [ ] Зафиксированы версии `yt-dlp`/Deno/ffmpeg и наличие EJS.
- [ ] Проверено влияние cookies и JS runtime.
- [ ] В отчёте нет реальных cookies/токенов/полных URL с секретами.
- [ ] Явный go/no-go и решение для Plan 11.
- [ ] `src/`, `tests/`, `pyproject.toml`, `docs/design/` не изменены.

## Handoff

- При go: Plan 11 может реализовывать subprocess-адаптер с известными ограничениями; в handoff — версии, обязательные флаги, cookie policy, известные ошибки.
- При no-go: Plan 11 `deferred`; URL-ветка не блокирует local MVP; зафиксируй в README/артефакте.
- При blocked: Plan 11 не стартует; нужно решение пользователя по URL/cookies/установке.

## Stop conditions / decision gates

- Нет 5 URL или разрешения на cookies/установку — `blocked`, не выдумывай данные.
- Нужно обойти DRM/платный доступ — стоп; это non-goal.
- Cookies нужно закоммитить — стоп; только путь и `0600`.
- D7 (версия yt-dlp) не решён — используй рекомендованный диапазон + lock, но не меняй `pyproject` в spike.
