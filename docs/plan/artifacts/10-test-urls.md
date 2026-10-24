# Test URL corpus (yt-dlp spikes)

- **Статус:** active
- **Обновлено:** 2026-10-23
- **Назначение:** воспроизводимые URL для Plan 10/11 и последующих live-проверок.
- **Правило:** только публичные канонические ссылки; `si`/`utm_*` и другие
  tracking-параметры удалены. Cookies, токены и приватные ссылки сюда не добавлять.

## YouTube

Требования в текущей сети: cookies + Deno/EJS + proxy. Для аудио использовать
`-f "bestaudio[ext=m4a]/bestaudio/best"`; все пять ссылок выбирают format `140`
(m4a/AAC, ~128 kbps).

| ID | URL | Длительность (ffprobe) | Примечание |
|---|---|---:|---|
| YT-01 | https://www.youtube.com/watch?v=k1T1wCVSJfY | 3634.921 s | длинное видео ~60 мин; при 403 помогает resume |
| YT-02 | https://www.youtube.com/watch?v=rhIl7650p0M | 182.579 s | короткий трек |
| YT-03 | https://www.youtube.com/watch?v=-YZBqM0JM9o | 216.699 s | короткий трек |
| YT-04 | https://www.youtube.com/watch?v=ghFOQc5TbCo | 210.071 s | короткий трек |
| YT-05 | https://www.youtube.com/watch?v=FrGv_izsOCA | 164.281 s | короткий трек |

## Yandex Music

Требования: cookies, **без proxy**, плагин `yt-dlp-yandex-music==2026.9.24`
(встроенный extractor в yt-dlp 2026.10.21 сломан). Cookies должны содержать
валидный `Session_id`; без него сервер отдаёт `smart_preview` (~15–30 s).

| ID | URL | Длительность (ffprobe) | Примечание |
|---|---|---:|---|
| YM-01 | https://music.yandex.ru/album/38695076/track/144098388 | 223.556 s | mp3 192 kbps; полный трек с cookies |
| YM-02 | https://music.yandex.ru/album/43600735/track/154937558 | 272.666 s | mp3 192 kbps; полный трек с cookies |
| YM-ALBUM-01 | https://music.yandex.ru/album/17515095 | — | альбом из 4 треков; проверена только metadata, без bulk-download (Plan 18) |

## Как использовать

```bash
# YouTube (пример; proxy/Deno/cookies — из локального окружения)
yt-dlp --cookies "$YT_COOKIES" -f "bestaudio[ext=m4a]/bestaudio/best" \
  --no-playlist -o "/tmp/%(id)s.%(ext)s" "https://www.youtube.com/watch?v=<ID>"

# Yandex Music: proxy должен быть выключен
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
  -u https_proxy -u http_proxy -u all_proxy \
  yt-dlp --cookies "$YM_COOKIES" -f "bestaudio/best" \
  -o "/tmp/%(id)s.%(ext)s" "https://music.yandex.ru/album/<album>/track/<track>"
```

Для Yandex обязательно проверять длительность через `ffprobe`: при протухшей
сессии загрузка завершается успешно, но файл оказывается preview-фрагментом.
Подробный отчёт: [10-spike-ytdlp.md](10-spike-ytdlp.md).
