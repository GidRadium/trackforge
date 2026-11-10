# L1.0 — cookies, proxy и doctor

**Предусловие:** [L0_FOUNDATION.md](./L0_FOUNDATION.md)  
**Следующий чекпоинт:** [L1_1_YTDLP_SINGLE.md](./L1_1_YTDLP_SINGLE.md)  
**Общий контекст:** [../INGESTION_ARCHITECTURE.md](../INGESTION_ARCHITECTURE.md), [../INGESTION_RESEARCH.md](../INGESTION_RESEARCH.md)

---

## Контекст для нового чата

Реализуется **L1.0** — обязательный gate перед первым запросом к YouTube. Без cookies/proxy YouTube может не отдать данные, поэтому этот слой делается **до** любого `get_info`/`extract_info`.

В L0 уже есть `SourceProfile`, `AppConfig`, `LoaderRegistry`, `MediaLoader`, `ProgressSink`, `FakeLoader`. Если чего-то нет — сначала добей L0, не начинай L1.0.

Ключевые документы:

- `docs/INGESTION_ARCHITECTURE.md` §4.5 (registry/profile), §9 (config);
- `docs/INGESTION_RESEARCH.md` §6 (cookies/proxy), §8 (yt-dlp deps).

---

## Цель

Сделать так, чтобы до первого сетевого запроса к YouTube:

- cookies и proxy были загружены и проверены;
- `YoutubeDL` получал `cookiefile`/`cookiesfrombrowser` и `proxy`;
- cookies не мутировались и не логировались;
- `doctor` показывал состояние;
- можно было выполнить контролируемый smoke.

## Границы

**Входит:**

- config для `[sources.youtube]`, `[sources.yandex_music]`, `[sources.soundcloud]`;
- CLI/API overrides `--cookies`, `--proxy`, `--source`;
- копия cookies на job `0600`;
- `proxy` URL/`env`/`none`;
- `doctor` без сети + `--check-network` (опционально);
- unit-тесты захвата `YoutubeDL` options;
- smoke-инструкция для YouTube.

**Не входит:**

- реальный `get_info` и маппинг (L1.1);
- скачивание (L2);
- Yandex-плагин (L1.3), хотя `doctor` может проверять его наличие.

---

## Задачи

### 1. Конфиг источников

Поддержать:

```toml
[sources.default]
socket_timeout = 20
retries = 3

[sources.youtube]
cookies = "~/.config/trackforge/cookies/youtube.txt"
proxy = "${YOUTUBE_PROXY}"
impersonate = "chrome"

[sources.yandex_music]
cookies = "~/.config/trackforge/cookies/yandex.txt"

[sources.soundcloud]
# cookies не нужны
```

Требования:

- `cookies` → `cookies_file`; `cookies_from_browser` — альтернатива;
- `cookies` и `cookies_from_browser` взаимоисключающие;
- `${VAR}` подставляется из env; отсутствие переменной — `ConfigError` с понятным текстом;
- `~` раскрывается; относительные пути — от файла конфига;
- `proxy`: URL, `"env"`, `"none"`;
- неизвестные ключи — warning, не падение;
- `token`/`cookies`/`proxy` не попадают в `repr`, JSON, логи.

### 2. Cookie passthrough

- Передавать в yt-dlp оригинальный файл из профиля напрямую.
- Не копировать cookies в staging: yt-dlp может обновлять файл на месте.
- Оригинал может меняться yt-dlp; TrackForge его не перезаписывает сам.
- Проверить формат Netscape (первая строка/поля) и права, но не читать содержимое в лог.

### 3. Proxy

- `proxy` попадает в `_base_options`/`YoutubeDL(proxy=...)`.
- `"none"` — явно без прокси.
- `"env"` — взять `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`.
- credentials redact в логах.
- `doctor` умеет проверить доступность прокси **только** с `--check-network`; по умолчанию — без сети.

### 4. Doctor

Команда:

```bash
tfg doctor --source youtube
tfg doctor --source youtube --check-network
```

Проверки по умолчанию (без сети):

- config читается;
- cookies существуют/читаемы/не world-readable;
- `yt-dlp` импортируется;
- `yt_dlp_ejs` импортируется;
- `deno.find_deno_bin()` существует и запускается;
- `ffmpeg`/`ffprobe` найдены (для будущего L2);
- `yt-dlp-yandex-music` установлен/импортируется (общая проверка);
- proxy URL валиден; credentials не печатаются.

С `--check-network`:

- lightweight HTTP-запрос к YouTube (или `extract_info` короткого тестового URL);
- понятный отчёт: cookies приняты/не приняты, proxy работает/нет.

### 5. Тесты

Добавить:

- `tests/acquire/test_auth_config.py`;
- `tests/acquire/test_cookie_handling.py`;
- `tests/acquire/test_ytdlp_options.py`;
- `tests/acquire/test_doctor.py`.

Проверить:

- captured `YoutubeDL` opts содержат `cookiefile`/`proxy`;
- `cookies_from_browser` tuple;
- `cookiefile` указывает на настроенный файл; yt-dlp может его обновлять;
- `${MISSING}` → `ConfigError`;
- `proxy="none"` не попадает в opts;
- `proxy="env"` подхватывает env;
- redaction в логах/repr;
- `doctor` без сети не делает сетевых вызовов (monkeypatch).

---

## Ручная проверка на реальных данных

### 1. Без сети

```bash
uv run tfg doctor --source youtube
```

Ожидаемо: отчёт по cookies/yt-dlp/ejs/deno/ffmpeg, без сетевых запросов.

### 2. Cookies

Экспортировать Netscape cookies для YouTube (расширение браузера или `--cookies-from-browser`) в путь из конфига.

```bash
uv run tfg doctor --source youtube
```

Ожидаемо: cookies найдены, права не world-readable.

### 3. Smoke с сетью

Только после успешного doctor:

```bash
uv run tfg doctor --source youtube --check-network
```

Если проверка проходит — можно переходить к L1.1. Если нет — исправить cookies/proxy, не начинать `get_info`.

### 4. Proxy

Задать `[sources.youtube].proxy` (или `--proxy`) и повторить `--check-network`. Убедиться, что:

- запрос идёт через прокси;
- в выводе нет логина/пароля;
- при выключенном прокси поведение явно отличается.

---

## Критерии готовности

- [ ] `SourceProfile`/`AppConfig` читают cookies/proxy.
- [ ] `cookiefile`/`proxy` реально попадают в `YoutubeDL` options (тест).
- [ ] Cookie passthrough: используется настроенный файл; yt-dlp может его обновлять.
- [ ] `doctor` без сети зелёный.
- [ ] `doctor --check-network` с рабочими cookies/proxy проходит.
- [ ] Секреты не утекают.
- [ ] Без cookies/proxy loader не идёт молча в сеть, а даёт `AuthRequired`/`ConfigError`.
- [ ] `uv run pytest -q` зелёный.

**Не переходить к L1.1, пока smoke не пройден.**

---

## Что не делать

- Не реализовывать `get_info`/маппинг.
- Не скачивать медиа.
- Не логировать cookies/proxy.
- Не делать глобальный proxy для всех источников.
- Не добавлять `--cookies-from-browser` как обязательный; файл — минимум.

---

## Передача дальше

Следующий чат получает:

- рабочий профиль YouTube с cookies/proxy;
- `doctor` и тесты;
- подтверждённый smoke.

Дальше — [L1_1_YTDLP_SINGLE.md](./L1_1_YTDLP_SINGLE.md): generic/YouTube single `inspect`.
