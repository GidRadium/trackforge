# TrackForge

Local-first инструмент для скачивания, анализа и аккуратного тегирования аудио: от ссылки или локального файла до объяснимого набора метаданных, обложки, текста и файла с предсказуемым именем.

> **Статус:** проектирование. Код ещё не пишется; сначала фиксируем архитектуру, доменную модель и контракты.

## Документация

- [`docs/reflection/start-ideas.md`](docs/reflection/start-ideas.md) — исходная идея.
- [`docs/design/README.md`](docs/design/README.md) — карта проектных документов и процесс.
- [`docs/design/00-vision.md`](docs/design/00-vision.md) — видение, принципы, границы.
- [`docs/design/01-domain-model.md`](docs/design/01-domain-model.md) — сущности, claims, decisions, feedback.
- [`docs/design/02-architecture.md`](docs/design/02-architecture.md) — слои, порты, адаптеры, API, license boundary.
- [`docs/design/open-questions.md`](docs/design/open-questions.md) — открытые вопросы и принятые решения.

## Ключевая идея

TrackForge — не просто скачивалка с тегами, а система доказательств и решений:

```text
Observation → Claim → Decision → Render
```

Каждое значение имеет источник, метод, уверенность и доказательство. Оригиналы не изменяются; результат можно пересобрать, а спорные случаи уходят в review.

## Конфигурация

Все доступные параметры собраны в комментариях в [`config.example.toml`](config.example.toml):

```bash
mkdir -p ~/.config/trackforge
cp config.example.toml ~/.config/trackforge/config.toml
cp .env.example .env   # секреты; chmod 600 .env
```

Приоритет: параметры CLI > переменные окружения > `config.toml` > значения по умолчанию.
Секреты (`LLM API key`, AcoustID, MusicBrainz User-Agent) не хранятся в `config.toml`.

## Требования (предварительно)

- Python 3.12+;
- `uv`;
- `ffmpeg`/`ffprobe`;
- опционально: `faster-whisper`, `demucs`, `essentia`, cross-encoder/ONNX, локальная LLM.

## Лицензия

Copyright (C) 2026 GidRadium.

Проект распространяется под **GNU General Public License v3.0 or later**. Полный текст — в [`LICENSE`](LICENSE).

```text
TrackForge is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

TrackForge is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with TrackForge.  If not, see <https://www.gnu.org/licenses/>.
```

GPL/AGPL-зависимости (`mutagen`, `essentia` и др.) изолируются за портами и optional adapters; ядро не импортирует их напрямую. Это сохраняет возможность заменить адаптеры при будущей смене лицензии.
