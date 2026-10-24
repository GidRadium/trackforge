# Artifact 20: Release report

- **Статус:** pending
- **План:** [20-release](../20-release.md)
- **Версия:** fill
- **Commit/tag:** fill
- **Дата:** fill

> Заполнять после реального release-прогона. Секреты, cookies, токены не вставлять.

## 1. Scope

| Plan | Статус | Вошёл в релиз |
|---|---|---|
| 01–09 local MVP | fill | yes/no |
| 10–12 URL + providers | fill | yes/no |
| 13–15 CPU/LLM/review | fill | yes/no |
| 16 ASR | fill | yes/no/deferred |
| 17 Demucs | fill | yes/no/deferred |
| 18 Playlists | fill | yes/no/deferred |
| 19 HTTP API | fill | yes/no/optional |

- Release version: fill (`1.0.0` | `0.9.0`).
- D10 decision: fill.
- Deferred/known limitations: fill.

## 2. Окружение

| Параметр | Значение |
|---|---|
| Linux distro | fill |
| Python | fill |
| `uv` | fill |
| `ffmpeg`/`ffprobe` | fill |
| `yt-dlp`/Deno | fill |
| Extras installed | fill |
| Models | fill |

## 3. Quality gates

| Команда | Результат |
|---|---|
| `uv run ruff check .` | fill |
| `uv run ruff format --check .` | fill |
| `uv run mypy src` | fill |
| `uv run pytest -q` | fill |
| `uv run lint-imports` | fill |
| `uv build` | fill |
| clean venv install + `trackforge --version` | fill |
| `trackforge doctor --json` | fill |

## 4. Smoke

- Local E2E: fill (file, format, tags, cover, sidecar, LRC).
- Original hash unchanged: yes/no.
- Optional URL E2E: fill (`passed` | `deferred` | `blocked`).
- Read-back: yes/no.
- Repeat import/cache: yes/no.

## 5. Migrations / backup / GC

- Empty DB → head: fill.
- Upgrade from previous: fill.
- Backup → restore → doctor: fill.
- `library scan`: fill.
- `gc --dry-run`: fill (objects/bytes, no deletion).
- Restore documented: fill.

## 6. Security / license

- [ ] No secrets/cookies in git/logs/artifacts.
- [ ] Cookies `.env` 0600, config 0700.
- [ ] SSRF/path traversal/symlink checks.
- [ ] SQL parameterized.
- [ ] GPL boundary (`lint-imports`) green.
- [ ] License table updated.
- [ ] Model licenses/checksums.
- [ ] No DRM/paid bypass.

## 7. Release decision

- Go/no-go: fill.
- Blocking issues: fill.
- Known limitations: fill.
- Next version (v1.1): GUI/HTTP optional, Essentia/0.5B Deferred.
