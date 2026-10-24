# Artifact 11: per-component / per-site proxy routing

- **Статус:** completed
- **План:** [11-url-ingest-ytdlp](../11-url-ingest-ytdlp.md)
- **Дата:** 2026-10-24

## 1. Решение

Глобального прокси для приложения нет. Прокси задаётся отдельно для каждого
сетевого компонента; для `yt-dlp` дополнительно поддерживаются per-site
исключения. Пустая строка означает **direct connection**, а не «взять
глобальный прокси».

Стандартные переменные окружения `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
`NO_PROXY` (и lowercase-варианты) приложением **игнорируются**. Это важно:
раньше `yt-dlp` наследовал окружение, и глобальный прокси мог попасть на
Яндекс.Музыку, где он недопустим. Теперь:

- `yt-dlp` получает явный `--proxy <url>` или `--proxy ""` (direct);
- `HttpClient` создаётся с `trust_env=False`;
- LLM-клиент создаётся с `trust_env=False`;
- загрузка моделей очищает proxy-переменные на время вызова и использует
  только `[network.models]`.

## 2. Конфигурация

```toml
[network.ytdlp]
# Default for yt-dlp when a site rule does not match.
proxy = "socks5h://user:pass@proxy.example:1080"

[network.ytdlp.sites]
# Host suffix -> proxy. Empty string means explicitly direct.
"youtube.com" = "socks5h://user:pass@proxy.example:1080"
"youtu.be" = "socks5h://user:pass@proxy.example:1080"
"music.yandex.ru" = ""
"music.yandex.com" = ""

[network.ytdlp.cookies]
# Host suffix -> cookie file. Several domains may point to the same account
# file. Empty string explicitly disables cookies for the route.
"youtube.com" = "~/.config/trackforge/cookies/youtube.com.txt"
"youtu.be" = "~/.config/trackforge/cookies/youtube.com.txt"
"music.yandex.ru" = "~/.config/trackforge/cookies/yandex.txt"
"music.yandex.com" = "~/.config/trackforge/cookies/yandex.txt"

[network.metadata]
# One proxy for MusicBrainz/AcoustID/CAA/LRCLIB/Deezer/iTunes HTTP calls.
proxy = ""

[network.llm]
# OpenAI-compatible endpoint only.
proxy = ""

[network.models]
# Explicit `models download` calls (Hugging Face / Demucs checkpoint).
proxy = ""
```

Правила:

1. Для `yt-dlp` сначала ищется самый специфичный host suffix. Побеждает
   `music.youtube.com` над `youtube.com`.
2. Если правило найдено, используется его значение, включая `""` (direct).
3. Если правила нет, используется `[network.ytdlp].proxy`.
4. Если и он пуст — direct.
5. `metadata`, `llm`, `models` — независимые компоненты; настройка одного не
   влияет на остальные.
6. Cookies: сначала ищется host suffix в `[network.ytdlp.cookies]`, включая
   несколько доменов на один файл. Если маршрута нет — используется старый
   поиск `<config_dir>/cookies/<host>.txt`. Пустой путь в маршруте отключает
   cookies и fallback для этого домена. Относительные пути считаются от
   `config_dir`.

Поддерживаемые схемы: `http`, `https`, `socks4`, `socks4a`, `socks5`,
`socks5h`. Для HTTP-прокси с логином/паролем кодируй специальные символы
URL-encoded.

Env-переопределения (это именно конфигурация TrackForge, а не глобальные
proxy-переменные):

```bash
TRACKFORGE_YTDLP_PROXY='socks5h://user:pass@proxy.example:1080'
TRACKFORGE_YTDLP_SITE_PROXIES='{"music.yandex.ru":"","youtube.com":"socks5h://user:pass@proxy.example:1080"}'
TRACKFORGE_METADATA_PROXY='http://metadata-proxy.example:3128'
TRACKFORGE_LLM_PROXY='http://llm-proxy.example:3128'
TRACKFORGE_MODELS_PROXY='http://models-proxy.example:3128'
```

## 3. Яндекс.Музыка: direct + plugin

В текущей сети YouTube требует proxy, а Яндекс.Музыка — **без proxy**.
Конфигурация выше делает это явным: YouTube-правило задаёт proxy, а
`music.yandex.ru = ""` принудительно отключает его даже при непустом default.

Яндекс.Музыка также требует сторонний plugin: встроенный extractor в
`yt-dlp 2026.10.21` сломан. Одиночный трек вида
`https://music.yandex.ru/album/<id>/track/<id>` принимается (это не playlist),
а голый `/album/<id>` по-прежнему отклоняется. Plugin ставится в то же
окружение, что и `yt-dlp`:

```bash
# Если yt-dlp установлен как uv tool:
uv tool install --force 'yt-dlp[default]' \
  --with 'yt-dlp-yandex-music==2026.9.24'

# Если yt-dlp установлен через pip:
python3 -m pip install -U 'yt-dlp[default]' 'yt-dlp-yandex-music==2026.9.24'
```

Проверка без скачивания:

```bash
yt-dlp --proxy "" --simulate --print '%(title)s' \
  'https://music.yandex.ru/album/1/track/2'
```

`TrackForge` не устанавливает и не обновляет внешние yt-dlp-плагины
автоматически; `doctor` проверяет наличие `yt-dlp`, Deno и cookies, но не
содержимое plugin-окружения.

## 4. Безопасность и наблюдаемость

- Значения прокси не печатаются: `config show` показывает `<configured>`,
  `doctor` — только bool/список хостов.
- Proxy-пароли не попадают в `SourceSnapshot`, Intake, claims, sidecar, логи.
- `config.toml` с proxy credentials рекомендуется держать с правами `0600`.
- `--offline` по-прежнему блокирует любые сетевые вызовы до открытия сокета.
- `yt-dlp` запускается без shell, proxy передаётся отдельным argv-элементом.

## 5. Ограничения

- `[network.metadata]` — один прокси на весь metadata-компонент, не per-host.
  Per-host маршрутизация сейчас нужна только `yt-dlp` (YouTube vs Yandex).
- Для SOCKS-прокси в metadata/LLM/model HTTP-клиентах нужен `httpx[socks]`
  (в базовый install не входит); для `yt-dlp` SOCKS поддержан нативно.
- Прокси не применяется к локальным файлам и к чтению уже скачанных артефактов.
