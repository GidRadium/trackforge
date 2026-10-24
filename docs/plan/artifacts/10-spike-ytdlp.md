# Artifact 10: yt-dlp / Deno / cookies spike

- **Статус:** go
- **План:** [10-spike-ytdlp](../10-spike-ytdlp.md)
- **Дата:** 2026-10-23 (UTC)
- **Исполнитель:** DeepSeek Harness coding agent
- **Расширение:** YouTube m4a + Yandex Music (cookies, plugin, без proxy)

> Результаты получены реальным запуском на целевой Linux-машине. Cookies, токены и
> нередактированные URL в отчёт не включены. Тестовые ссылки заменены на
> `<redacted-NN>`; соответствие «ID → URL» лежит в публичном
> [10-test-urls.md](10-test-urls.md) (канонические ссылки без tracking-параметров).

## 1. Окружение

| Параметр | Значение |
|---|---|
| Linux distro | Ubuntu 24.04.5 LTS (Noble), kernel 6.8.0-147-generic, x86_64 |
| CPU | QEMU Virtual CPU version 2.5+ (KVM), 18 vCPU |
| RAM | 21 GiB, swap 4 GiB |
| Python (в окружении yt-dlp) | 3.12.3 |
| `yt-dlp --version` | 2026.10.21 (stable, PyPI; установлен через `uv tool`) |
| `deno --version` | 2.9.8 (stable, x86_64-unknown-linux-gnu; V8 15.0.245.2) |
| `yt-dlp-ejs` | 0.8.0, зависимость `yt-dlp[default]`; модуль присутствует |
| `yt-dlp-yandex-music` | 2026.9.24 (PyPI, MIT); плагин загружается, перекрывает сломанный built-in extractor |
| `ffmpeg`/`ffprobe` | 6.1.1-3ubuntu5 |
| Сеть/регион | YouTube напрямую — TCP timeout; рабочий YouTube-прогон — через authenticated SOCKS5 proxy. Yandex Music напрямую — HTTP 200, **без proxy**. Регион выхода не подтверждён. SoundCloud/Bandcamp не тестировались. |

Дополнительно:

- `--cache-dir` указывал на writable-каталог в `/tmp`: без этого yt-dlp писал
  нефатальные warning'и о невозможности записи в `~/.cache`.
- `yt-dlp`/Deno/плагин стояли во временном tool env в `/tmp` и после spike
  удалены; для Plan 11 нужна постоянная user-space установка.
- Deno был доступен через `PATH`; `--js-runtimes` явно не передавался.
- `--remote-components` не использовался; EJS-скрипты брались из локального
  `yt-dlp-ejs`.
- Версия `yt-dlp` — актуальный stable на дату spike. В `pyproject.toml` ничего
  не менялось; диапазон/пин для Plan 11 остаётся решением D7.
- Для Yandex все proxy-переменные (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
  lowercase-варианты) явно снимались через `env -u`; с proxy Yandex не проверялся
  и в рабочем сценарии не должен использоваться.

## 2. Тестовые URL

Набор: **5 YouTube + 2 Yandex-трека + 1 Yandex-альбом**. Полные ссылки — в
[10-test-urls.md](10-test-urls.md). В таблицах ниже — redacted-метки.

### 2.1. YouTube: аудио в m4a

Для YouTube по требованию пользователя использован не `bestaudio` (он выбирал
webm/opus), а `-f "bestaudio[ext=m4a]/bestaudio/best"`. На всех пяти URL выбран
format `140`: m4a/AAC, 44.1 kHz, stereo, ~128 kbps. Все пять файлов проверены
`ffprobe` и полным decode-check.

| # | Extractor | URL (redacted) | Metadata OK | Audio OK | Container/codec | Duration | Size | Download time | Cookies | JS runtime | Error |
|---:|---|---|---|---|---|---:|---:|---:|---|---|---|
| 1 | youtube | `youtube.com/watch?v=<redacted-01>` | yes | yes | m4a / AAC, 44.1 kHz, stereo | 3634.921 s | 56.06 MiB | 42.83 s* | yes | yes | 403 mid-download → resume |
| 2 | youtube | `youtube.com/watch?v=<redacted-02>` | yes | yes | m4a / AAC, 44.1 kHz, stereo | 182.579 s | 2.82 MiB | 9.94 s | yes | yes | — |
| 3 | youtube | `youtube.com/watch?v=<redacted-03>` | yes | yes | m4a / AAC, 44.1 kHz, stereo | 216.699 s | 3.34 MiB | 8.95 s | yes | yes | — |
| 4 | youtube | `youtube.com/watch?v=<redacted-04>` | yes | yes | m4a / AAC, 44.1 kHz, stereo | 210.071 s | 3.24 MiB | 10.58 s | yes | yes | — |
| 5 | youtube | `youtube.com/watch?v=<redacted-05>` | yes | yes | m4a / AAC, 44.1 kHz, stereo | 164.281 s | 2.53 MiB | 9.78 s | yes | yes | — |

\* YT-01 — длинное видео (~60 мин). Три попытки, две упали с `HTTP 403`; на
  попытке, оставившей `.part`, 403 пришёл после ~28.9 MiB. Повтор с `--continue`
  докачал файл до полного размера. Время 42.83 s — сумма трёх попыток.

Примечания:

- `Duration` — из `ffprobe`; `Size` — фактический размер; `Download time` — wall
  time полного вызова `yt-dlp` (extraction + download, включая startup/retry).
- Metadata-запросы YouTube с cookies занимали 4.25–4.53 s (среднее 4.39 s).
- Все пять m4a прошли `ffprobe` и decode-check `ffmpeg -v error -xerror -i <file>
  -f null -` (exit 0, ошибок нет).
- `--simulate` для всех пяти URL подтвердил выбор `format_id=140`, `ext=m4a`,
  `acodec=mp4a.40.2`.

### 2.2. Yandex Music: полный трек, не preview

Yandex Music проверялся **без proxy**. Сначала официальный built-in extractor
`yt-dlp 2026.10.21` упал на `handlers/track.jsx`; после установки
`yt-dlp-yandex-music 2026.9.24` оба трека и альбом разрешились. Cookies —
Netscape-файл с валидным `Session_id`; без cookies сервер отдаёт preview.

| # | Extractor | URL (redacted) | Metadata OK | Audio OK | Container/codec | Duration | Size | Download time | Cookies | JS runtime | Error |
|---:|---|---|---|---|---|---:|---:|---:|---|---|---|
| 1 | yandexmusic:track (plugin) | `music.yandex.ru/album/<redacted>/track/<redacted-01>` | yes | yes | mp3 / MPEG-1 Layer III, 44.1 kHz, stereo, 192 kbps | 223.556 s | 5.12 MiB | 1.29 s | yes | no (not required) | — |
| 2 | yandexmusic:track (plugin) | `music.yandex.ru/album/<redacted>/track/<redacted-02>` | yes | yes | mp3 / MPEG-1 Layer III, 44.1 kHz, stereo, 192 kbps | 272.666 s | 6.24 MiB | 1.88 s | yes | no (not required) | — |
| 3 | yandexmusic:album (plugin) | `music.yandex.ru/album/<redacted-album>` | yes | n/a | — | — | — | 1.00 s (metadata) | yes | no (not required) | только metadata; 4 трека; bulk-download не выполнялся (Plan 18) |

Ключевая проверка «не 30 секунд»:

| Сценарий | Файл | `ffprobe` duration | Размер | Результат |
|---|---|---:|---:|---|
| Yandex, cookies + plugin | YM-01 | 223.556 s | 5,365,342 B | полный трек |
| Yandex, cookies + plugin | YM-02 | 272.666 s | 6,543,986 B | полный трек |
| Yandex, **без cookies** (negative control) | YM-01 | 15.334 s | 368,684 B | `smart_preview` |

Метаданные Yandex давали 223.51 s и 272.64 s; фактические файлы длиннее на
0.046 s и 0.026 s соответственно. Это не 30-секундный обрезок: размер и duration
совпадают с полным 192 kbps потоком. Без cookies тот же URL дал 15.334 s — ровно
тот silent-preview, который plugin называет `smart_preview`. Plugin запрашивал
lossless, но сервер отдал mp3 192 kbps; FLAC для этих треков не предложен.

Альбом `YM-ALBUM-01` разрешился в `yandexmusic:album`, 4 трека, все entries с
URL; скачивание альбома не запускалось, чтобы не выходить за non-goal Plan 10
(плейлисты/альбомы — Plan 18).

## 3. Success rate

| Метрика | Значение |
|---|---:|
| URL разрешено (metadata) с нужными cookies/plugin | 8 / 8 (100%) |
| YouTube metadata анонимно, без cookies | 0 / 5 (0%) |
| YouTube metadata с cookies + Deno | 5 / 5 (100%) |
| YouTube m4a audio | 5 / 5 (100%; YT-01 после resume) |
| YouTube m4a format 140 selected | 5 / 5 (100%) |
| Yandex metadata built-in extractor | 0 / 1 (0%) |
| Yandex metadata с plugin + cookies | 3 / 3 (100%) |
| Yandex audio с cookies + plugin | 2 / 2 (100%) |
| Yandex full audio без cookies | 0 / 1 (0%; 15.334 s preview) |
| Yandex album metadata (4 entries) | 1 / 1 (100%; без bulk-download) |
| End-to-end для 7 скачанных треков (5 YT + 2 YM) | 7 / 7 (100%) |
| `ffprobe` + decode-check | 7 / 7 (100%) |
| Повторяемость YouTube metadata (3 прогона) | 3 / 3 |
| YouTube среднее / медиана download | 16.42 s / 9.94 s |
| YouTube суммарно | 68.0 MiB / 82.08 s (с retry) |
| Yandex среднее / суммарно | 1.59 s / 11.36 MiB, 3.17 s |
| YouTube `--dump-single-json` | 55.6–597.9 KiB |
| Yandex `--dump-single-json` | 1.5–3.9 KiB |

Анонимный YouTube baseline: все 5 URL стабильно дали bot-check
(`Sign in to confirm you're not a bot`), без 403/429. Cookies меняют результат на
5/5. Rate limit не проявился; 3 повторных metadata-прогона — 3/3.

## 4. Ошибки и поведение

| Ошибка | URL # | Сообщение (redacted) | Retryable | Что помогло |
|---|---|---|---|---|
| YouTube BotCheck / cookies required | YT-01…05 | `Sign in to confirm you're not a bot... Use --cookies-from-browser or --cookies for the authentication` | no без cookies | Валидные YouTube cookies: 5/5 |
| YouTube JS runtime missing / n-challenge | YT-04 | `n challenge solving failed... Only images are available for download` → `Requested format is not available` | no без runtime | Deno 2.9.8 + `yt-dlp-ejs` 0.8.0: 5/5 |
| YouTube direct network timeout | YT-02 | `Read timed out` / `TransportError`; curl exit 28 | yes при смене маршрута | SOCKS5 proxy; после него 5/5 |
| YouTube HTTP 403 на длинном m4a | YT-01 | `unable to download video data: HTTP Error 403: Forbidden` после ~28.9 MiB | да, через resume | `--continue` + повтор: файл 58,786,145 B, decode OK |
| Cache write warning | YT-02 (первый прогон) | `Writing cache to '~/.cache/yt-dlp/...' failed` | n/a, не фатально | `--cache-dir` в writable-каталог |
| Yandex built-in extractor broken | YM-01 | `Failed to parse JSON...` / `ERROR: 'NoneType' object is not subscriptable` на `handlers/track.jsx` | no | Плагин `yt-dlp-yandex-music` 2026.9.24: 3/3 |
| Yandex smart_preview без cookies | YM-01 | `server served a 'smart_preview' stream...`; файл 15.334 s | no без cookies | Cookies с `Session_id`; затем 223.556 s |
| Yandex plugin: preview warning | YM-01 | `the Yandex session may be stale... or this track may be preview-only` | no без cookies | Cookies + обязательная проверка duration |

Не наблюдались: HTTP 429, `DrmProtected`, geo-block, удалённое/приватное видео.
YouTube 403 возник посреди длинной m4a-загрузки; причину (CDN или per-request
лимит proxy) не изолировали, но resume помог в этом прогоне. Это нельзя считать
безопасным во всех сетях — Plan 11 должен уметь продолжать `.part`.

## 5. Cookies / JS runtime

- YouTube cookies: `.spike-private/www.youtube.com_cookies_2.txt`, `0600`,
  Netscape, git-excluded.
- Yandex cookies: `.spike-private/music.yandex.ru_cookies.txt`, `0600`,
  Netscape, git-excluded; 31 запись, `Session_id` присутствует и не истёк на дату
  теста. Содержимое в отчёт не выводилось.
- Cookies не попали в stdout/stderr/JSON yt-dlp: **yes**.
  - 0 вхождений `Cookie:`, `Set-Cookie:` и Netscape-маркеров в yt-dlp-выводе и
    JSON;
  - 0 JSON-ключей с именами cookie-полей;
  - taint-scan: 40 уникальных значений (≥12 символов) из двух cookie-файлов
    искались в 58 файлах yt-dlp stdout/stderr/JSON — 0 совпадений. Сессионные
    значения (`Session_id`, `sessionid2`, `L`) в yt-dlp-выводе отсутствуют.
    Отладочный curl-cookie-jar и HTML-тело, созданные при диагностике, удалены;
    в отчёт они не попали.
- Yandex cookies обязательны: без cookies 15.334 s preview; с cookies 223.556 s
  и 272.666 s. Это не «успешная загрузка» без проверки duration.
- YouTube cookies + Deno обязательны: без cookies 0/5 bot-check; без JS runtime
  0/1, только images. С ними — 5/5.
- EJS/PO Token: `yt-dlp-ejs` 0.8.0, source — python package; `--remote-components`
  не требовался; PO Token provider не настраивался (`PO Token Providers: none`).
  GVS PO Token эксперимент наблюдался, но на этих URL не блокировал форматы.
- Yandex JS runtime не нужен; проверка шла с Deno на `PATH`, но plugin его не
  использует. Yandex plugin — сторонний (MIT), поэтому его версия должна быть
  закреплена и проверена перед production.
- Внешние ориентиры: официальный
  [EJS setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS) подтверждает
  Deno ≥ 2.3.0 и `yt-dlp[default]`; плагин
  [yt-dlp-yandex-music](https://github.com/fgsfds1/yt-dlp-yandex-music-plugin)
  нужен потому, что built-in extractor вызывает retired `handlers/*.jsx`.

## 6. Вывод

- **YouTube: go.** Metadata + m4a audio 5/5; ошибки классифицированы; cookies не
  текут. Анонимный YouTube — no-go (0/5 bot-check).
- **Yandex Music: go с обязательным сторонним plugin.** С cookies + plugin 2/2
  полных трека; built-in extractor — no-go. Если Plan 11 не готов принять
  third-party plugin, Yandex-ветку нужно **defer**, а не выдавать за рабочий
  built-in сценарий.
- **Решение для Plan 11: proceed** для YouTube; Yandex — proceed с опциональным
  pinned plugin `yt-dlp-yandex-music==2026.9.24` (или defer, если plugin
  запрещён архитектурой). Локальный MVP от URL-ветки не зависит.
- Обязательные ограничения Plan 11:
  - YouTube audio: `-f "bestaudio[ext=m4a]/bestaudio/best"`; не `bestaudio` без
    ограничения, иначе выбирается webm/opus. Все 5 тестовых URL дают format 140.
  - YouTube: `--continue` и верхнеуровневый retry/resume для 403 на длинных
    файлах; проверять итоговый размер/duration; `--no-playlist`, cookies,
    Deno/EJS, writable `--cache-dir`.
  - Yandex: **всегда без proxy**; `--cookies <path>`, `-f "bestaudio/best"`,
    plugin 2026.9.24; proxy-переменные вычищать per-call, а не наследовать
    глобально.
  - Yandex: после скачивания сравнивать `ffprobe` duration с metadata duration;
    если файл <50% ожидаемого — `PreviewOnly`/`CookiesExpired`, не success.
    Plugin уже предупреждает, но проверка должна быть в адаптере.
  - Cookies — per-site, `0600`; проверять existence/права, не логировать
    содержимое; при отсутствии — `CookiesRequired`, не общий bot-check.
  - JS runtime проверять в `doctor`: Deno ≥ 2.3.0, `yt-dlp-ejs` присутствует;
    для Yandex дополнительно проверять plugin (или явно объявлять Yandex
    unsupported без него).
  - Не задавать в окружении yt-dlp произвольные переменные вида `*_PROXY`:
    yt-dlp интерпретирует их как дополнительные proxy-схемы.
  - Плейлисты/альбомы — Plan 18; в этом spike альбом только перечислен (4 entries),
    без bulk-download.
  - Выборка ограничена: 5 YouTube + 2 Yandex-трека; SoundCloud/Bandcamp, 429,
    DRM и geo не проверялись. Один cookie-set и один proxy-выход — не доказательство
    долговременной стабильности.

## 7. Проверка на секреты

- [x] В отчёте нет cookies/токенов/полных URL; только redacted-метки.
- [x] Публичный corpus `10-test-urls.md` содержит канонические URL без `si`/`utm_*`
  и без cookies; это тестовые ссылки, не секреты.
- [x] Временные аудио/логи/инструменты — в `/tmp/tf-spike`; после spike удалены.
- [x] Cookies не закоммичены: `.spike-private/` исключён локально через
  `.git/info/exclude`, `.env` — через `.gitignore`.

Проверка на секреты: `rg` по отчёту находит только описательные `cookies` и
`PO Token`; 0 видео-ID, 0 полных тестовых URL, 0 значений cookies и proxy. Taint-scan
40 cookie-значений по yt-dlp-выводу — 0 совпадений.
