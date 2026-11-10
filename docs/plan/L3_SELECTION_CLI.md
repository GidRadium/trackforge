# L3 — выбор пользователя и CLI

**Предусловие:** [L2_DOWNLOAD.md](./L2_DOWNLOAD.md)  
**Следующий чекпоинт:** [L4_INGEST_HANDOFF.md](./L4_INGEST_HANDOFF.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

До этого пользовательского выбора не было: `inspect` и `download` работали по явным ID. Теперь добавляем CLI/UX и текстовый поиск.

**Не делать:** финальные имена/теги (L4), VK/Pinterest (L5).

---

## Цель

- `tfg URL` — inspect + выбор варианта + download.
- `tfg "Artist - Title"` — поиск → выбор кандидата → URL → inspect/download.
- `--json`/`--yes`/`--variant`/`--audio`/`--video` для автоматизации.
- Рекомендации loader'а используются, а не «первый вариант».

---

## Задачи

### 1. Неинтерактивный режим

```bash
tfg "https://..." --variant 1080p-mp4 --yes
tfg "https://..." --audio recommended --yes
tfg "https://..." --video recommended --yes
tfg "https://..." --json
tfg "https://..." --dry-run --json
```

- `--variant`, `--audio`, `--video`, `--yes`, `--json`, `--dry-run`;
- если stdin не TTY и выбор не задан — список вариантов + exit 2;
- `--yes` для URL берёт `recommendations.video`/`audio`; для текста требует `--pick`.

### 2. Интерактивный режим

- нумерованный список, без тяжёлого TUI;
- `★` у recommended;
- thumbnail URL/размеры (опционально preview);
- `a`/`v` — recommended audio/video;
- подтверждение для плейлиста;
- пустой ввод — recommended/первый.

### 3. Текстовый поиск

`MediaLoader.search` + `SearchService`:

- YouTube: `ytsearch` + фильтр доступности;
- SoundCloud: `scsearch` (если реализуем);
- Yandex/VK — по возможности API; иначе `SearchNotSupported`;
- `SearchCandidate` с thumbnail;
- `--pick N`;
- не автовыбирать первый результат;
- дедуп/rank.

### 4. `tfg` команды

| Команда | Что |
|---|---|
| `tfg URL` | inspect → выбор → download |
| `tfg "query"` | search → выбор → URL → ... |
| `tfg input ...` | явный алиас |
| `tfg i ...` | короткий алиас |
| `tfg inspect ...` | только данные |
| `tfg search ...` | только поиск |
| `tfg sources` | список loaders/возможностей |
| `tfg doctor` | проверка |

- `tfg URL` не должен конфликтовать с `tfg info/set/scan`; subcommands имеют приоритет.
- `--json` — финальный результат; `--events jsonl` — прогресс.

### 5. Ошибки/коды

- 0 успех;
- 1 операционная ошибка;
- 2 нужен выбор/неверные аргументы;
- 130 отмена.

### 6. Тесты

- `test_cli_inspect.py`;
- `test_cli_download_variant.py`;
- `test_cli_noninteractive.py` — no TTY;
- `test_cli_search.py` — fake SearchService;
- `test_cli_json.py` — схема/секреты;
- `test_cli_exit_codes.py`.

---

## Ручная проверка на реальных данных

1. URL без выбора:

```bash
uv run tfg "https://www.youtube.com/watch?v=..." 
```

Проверить: список вариантов, `★`, выбор, download, прогресс.

2. Non-interactive:

```bash
uv run tfg "https://..." --variant ... --yes
uv run tfg "https://..." --audio recommended --yes
```

3. Playlist:

```bash
uv run tfg "https://www.youtube.com/playlist?list=..." --flat --limit 5
```

Выбрать 1–2 item, скачать.

4. Текст:

```bash
uv run tfg "Artist - Track"
```

Проверить: кандидаты с thumbnail, выбор, URL, варианты.

5. `--json` не содержит cookies/tokens; exit codes.

---

## Критерии готовности

- [ ] `tfg URL` работает интерактивно.
- [ ] `--variant/--audio/--video/--yes` работают.
- [ ] Текстовый поиск работает.
- [ ] Recommended используется.
- [ ] Non-TTY даёт exit 2 без выбора.
- [ ] JSON стабилен.
- [ ] `uv run pytest -q` зелёный.
- [ ] ручной smoke URL/playlist/search.

---

## Что не делать

- Не писать теги/финальные имена.
- Не делать VK/Pinterest.
- Не делать TUI/бота.
- Не автовыбирать первый результат поиска.
- Не добавлять БД.

---

## Передача дальше

Следующий чат получает:

- CLI/UX;
- `SearchService`/`MediaLoader.search`;
- выбор вариантов и рекомендаций;
- JSON/exit codes.

Дальше — [L4_INGEST_HANDOFF.md](./L4_INGEST_HANDOFF.md).
