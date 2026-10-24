# Plan 03: CAS и artifact store

- **Статус:** ready
- **Тип:** MVP
- **Зависит от:** Plan 02
- **Разблокирует:** Plan 05, 06
- **Оценка:** M
- **Риск:** средний

## Роль

Ты — senior Python engineer, отвечающий за файловое хранилище. Работаешь в `trackforge` после домена. Задача — неизменяемые байты и типизированные артефакты; никакой БД и никакой бизнес-логики.

## Цель

Появляется `ArtifactStore`: content-addressed хранение байтов по SHA-256 и общий кэш артефактов с `manifest.json`/payload, каноническим JSON, проверкой целостности и защитой от path traversal. Повторная запись тех же байтов не создаёт второй файл.

## Контекст и инварианты

- CAS: `data_dir/cas/ab/cd/<sha256>`; байты неизменяемы (`ADR-0003`).
- Артефакты: `data_dir/artifacts/<cache_key>/manifest.json` + `payload.json`/`payload.bin`.
- `cache_key` — SHA-256 канонического JSON входов/версий/параметров; вычисляется вызывающим слоем (Plan 05), не адаптером.
- JSON canonical: UTF-8, sorted keys, no `NaN`/`Infinity`, стабильные числа (`03-pipeline.md` §3.2).
- Оригинальные файлы не изменяются; запись — temp + fsync + atomic rename.
- Все пути — только внутри `data_dir`/`cache_dir`; symlink/`..` не должны выводить за root.
- Секреты и cookies в артефакты не попадают.
- Домен не импортирует filesystem-адаптер; порт — в `application/ports`.

## Прочитать перед началом

- `docs/design/04-storage.md` — §1–4, §11.
- `docs/design/03-pipeline.md` — §3, §5.
- `docs/design/02-architecture.md` — §3, §5, §11.
- `docs/design/adr/0003-content-addressed-storage.md`.
- `docs/design/adr/0005-gpl-license-boundary.md`.
- `src/trackforge/application/`, `src/trackforge/domain/model/` (после Plan 02).
- `docs/plan/02-domain-core.md` (handoff).

## Предусловия

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/domain -q
uv run lint-imports
```

Все зелёные. Нет `pydantic` в `domain`; `application` может использовать pydantic для manifest DTO.

## Scope

- `application/ports/storage.py`: `ArtifactStore`, `ContentAddressedStore` protocols, `ArtifactRef`.
- `application/dto/artifacts.py`: pydantic-модель `ArtifactManifest`, `ArtifactProducer`, `ArtifactInput`.
- `application/services/canonical_json.py`: canonical JSON + `sha256_hex`.
- `adapters/persistence/filesystem/cas.py`: `FileSystemCas`.
- `adapters/persistence/filesystem/artifacts.py`: `FileSystemArtifactStore`.
- `adapters/persistence/filesystem/paths.py`: проверка root, атомарная запись, temp-файлы.
- `bootstrap/container.py`: фабрика store по `TrackForgeConfig`.
- Тесты unit/integration на `tmp_path`.

## Non-goals

- SQLite, записи в каталог, миграции.
- Скачивание, probe, render, публикация.
- GC/retention (Plan 20), backup (Plan 20).
- Шифрование, S3, hardlink.
- Кэш-политика TTL/`--force` — Plan 05 (здесь только примитивы).

## Deliverables

```text
src/trackforge/application/ports/storage.py
src/trackforge/application/dto/artifacts.py
src/trackforge/application/services/canonical_json.py
src/trackforge/adapters/persistence/filesystem/__init__.py
src/trackforge/adapters/persistence/filesystem/paths.py
src/trackforge/adapters/persistence/filesystem/cas.py
src/trackforge/adapters/persistence/filesystem/artifacts.py
tests/storage/test_canonical_json.py
tests/storage/test_cas.py
tests/storage/test_artifact_store.py
tests/storage/test_paths.py
```

## Контракты

### Content hash / CAS

```text
ContentHash = NewType("ContentHash", str)  # 64 lowercase hex

ContentAddressedStore:
  async put_bytes(data: bytes) -> ContentHash
  async put_file(path: Path) -> ContentHash
  async open(hash: ContentHash) -> BinaryIO
  async exists(hash: ContentHash) -> bool
  async verify(hash: ContentHash, expected_size: int | None = None) -> bool
  path_for(hash: ContentHash) -> Path
```

- `put_file` читает потоком; файл не загружается целиком в память.
- Если объект уже есть: проверить размер/хэш; вернуть тот же hash; второй файл не создавать.
- Если объект повреждён: `ArtifactCorrupted`, не перезаписывать молча.
- Temp-файл — в `cache_dir/tmp`, `fsync` файла, `os.replace` в CAS, `fsync` каталога (best effort).
- Права: CAS-файл `0644`, директории `0700` (создаются с `0o700`).
- `open` не следует symlink за пределы CAS root.

### Artifact manifest

```text
ArtifactProducer: id: str, version: str
ArtifactInput: type: str, hash: str
ArtifactManifest:
  artifact_id: str          # UUIDv7
  type: str                 # audio.probe.v1
  schema_version: int       # >= 1
  producer: ArtifactProducer
  inputs: list[ArtifactInput]
  params: dict              # JSON-safe
  params_hash: str          # sha256(canonical_json(params))
  payload_hash: str         # заполняет store
  warnings: list[dict]
  created_at: str           # ISO 8601 UTC, Z
  expires_at: str | null
  license: str | null
```

- Манифест валидируется pydantic; неизвестные поля запрещены (`extra="forbid"`).
- `payload_hash` и `params_hash` — 64 hex; store проверяет при записи.
- `type` соответствует `ArtifactType` из Plan 02.

### ArtifactStore

```text
ArtifactRef(cache_key, manifest_path, payload_path, payload_hash)

ArtifactStore:
  async put(cache_key: str, manifest: ArtifactManifest, payload: bytes | Path,
            payload_format: Literal["json", "bin"] = "json") -> ArtifactRef
  async find(cache_key: str) -> ArtifactRef | None
  async load(ref: ArtifactRef) -> tuple[ArtifactManifest, bytes]
  async verify(ref: ArtifactRef) -> bool
  async open_payload(ref: ArtifactRef) -> BinaryIO
```

- `cache_key` — 64 hex, не путь; непустой.
- `put`: если `cache_key` уже есть — сравнить payload hash; совпало → вернуть существующий ref; не совпало → `ArtifactConflict`, ничего не перезаписывать.
- `find`: отсутствие — `None`, не исключение.
- `load`/`verify`: проверяют manifest schema, payload hash, размер; при mismatch — `ArtifactCorrupted`.
- `payload.json` пишется только если payload — JSON-объект; бинарный payload — `payload.bin`. Store не угадывает: `payload` bytes всегда пишется как `payload.bin`, а JSON-артефакты передают канонические bytes и `payload.json`; выбор — по `manifest.type`? Лучше: `put(..., payload_format: Literal["json","bin"] = "json")`. Контракт: параметр `payload_format`; `json` требует валидный JSON и пишет `payload.json`, `bin` — `payload.bin`.
- `manifest.json` — canonical JSON + перевод строки в конце (стабильно); payload — ровно переданные bytes.

### Canonical JSON

- `canonical_json(value) -> bytes`: sorted keys, `ensure_ascii=False`, `allow_nan=False`, separators `(",", ":")`, UTF-8, без пробелов.
- `sha256_hex(data: bytes) -> str`.
- `cache_key_for(inputs, capability, params, config, model, prompt) -> str` — Plan 05 использует; в Plan 03 допускается чистая функция-помощник без политики.

## Порядок работ

1. Реализуй canonical JSON и тесты стабильности/unicode/NaN.
2. Реализуй path guard: `ensure_within(root, path)`, запрет `..`, symlink escape, temp-файлы.
3. Реализуй `FileSystemCas` с streaming hash и atomic rename.
4. Реализуй `ArtifactManifest`/`ArtifactRef` и `FileSystemArtifactStore`.
5. Подключи store в `bootstrap` (фабрика по config, без I/O при импорте).
6. Добавь тесты dedupe/corruption/conflict/провал посреди записи.
7. Прогони quality gates; закоммить.

## Тесты

- **unit:** canonical JSON sorted keys, UTF-8, no NaN, stable across runs; `sha256_hex` known vector; path guard; manifest validation.
- **contract:** `ArtifactStore` fake реализует протокол; адаптер проходит те же сценарии; `domain`/`application` не импортируют `adapters`.
- **integration:** real filesystem `tmp_path`: put bytes/file, dedupe, load, verify, permissions, original mtime/hash unchanged; 10 MB streaming file; interrupted temp cleanup.
- **fixtures:** `tmp_path`-based `store`, JSON payload, binary payload, generated 10 MB file.
- **negative:** `cache_key` с `/`, `..`, пустой; payload hash mismatch; corrupted manifest; symlink outside root; same cache_key different payload; NaN в JSON; запись за пределы root.

## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/storage -q
uv run lint-imports
uv run python -c "from pathlib import Path; from trackforge.adapters.persistence.filesystem.cas import FileSystemCas; print('cas import ok')"
```

Ожидается: тесты и type-check зелёные; повторный `put` не создаёт второй файл; `find`/`load` работают; path traversal отклоняется.

## Acceptance criteria

- [ ] `uv run pytest tests/storage -q` зелёный.
- [ ] Повторный `put_file` тех же байтов возвращает тот же `content_hash` и не создаёт второй файл.
- [ ] `put` с тем же `cache_key` и другим payload возвращает `ArtifactConflict`, старый артефакт не изменён.
- [ ] `load` повреждённого payload/manifest даёт `ArtifactCorrupted`, а не тихое значение.
- [ ] Пути с `..`, абсолютным путём и symlink за пределы root отклоняются.
- [ ] Оригинальный файл после `put_file` не изменён (hash/mtime).
- [ ] `manifest.json` стабилен: два запуска с теми же данными дают одинаковые bytes (кроме `created_at`/`artifact_id`, если они переданы; тест фиксирует их).
- [ ] `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` зелёные.
- [ ] В `src/trackforge/domain` нет импортов filesystem-адаптера; `docs/design/` не изменён.

## Handoff

Следующие планы могут считать готовыми:

- `ContentAddressedStore` и `FileSystemCas`.
- `ArtifactStore` с `put/find/load/verify` и manifest-схемой.
- `canonical_json`, `sha256_hex`, path guard.
- Фабрика store в `bootstrap`.
- Тесты dedupe/corruption/conflict/traversal.

## Stop conditions / decision gates

- Нужен `cache_key`-политика/TTL — это Plan 05, не расширяй.
- Нужна запись в SQLite — Plan 04.
- Нужно хранить секреты/куки в артефакте — стоп, это запрещено.
- Если pydantic нельзя использовать в `application/dto` из-за лишней зависимости — стоп и предложи stdlib-схему, не тащи её в домен.
