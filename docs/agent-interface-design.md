# Агент-ориентированный интерфейс codenav

Этот документ продолжает аудит `current-cli-audit.md`: сначала фиксирует, какая
информация нужна AI-агенту, затем сравнивает три разных Interface и предлагает
целевую форму. Это проект, а не описание уже реализованных флагов.

## Что агенту действительно нужно получить

Текст исходника сам по себе — только кусок карты. Чтобы следующий шаг не
приходилось угадывать, один результат должен содержать пять слоёв в таком порядке:

1. **Идентичность и полнота** — стабильный ID определения, repo-relative путь,
   координаты, версия схемы, статус и coverage сканирования.
2. **Фокус и доказательства** — исходник цели/изменения/совпадения и точные строки,
   из-за которых элемент попал в результат.
3. **Ближайшее влияние** — входящие и исходящие references с местом каждого
   reference; не просто список похожих имён.
4. **Честная неопределённость** — ambiguity, способ извлечения, пропуски,
   применённые лимиты и факт усечения.
5. **Исполнимое продолжение** — несколько детерминированных `argv`, а не совет
   свободным текстом.

Тогда результат становится не тупиковой распечаткой, а звеном цепочки:

```text
discover -> choose exact Entity ID -> inspect evidence -> follow one edge
         -> edit -> refresh/reinspect -> test
```

## Три рассмотренных Interface

### Вариант A: одна универсальная команда

```text
codenav context [SCOPE...]
  [--symbol NAME | --match REGEX | --diff FILE|- | --lines PATH SPEC]
  [--root DIR] [--budget N] [--format json|text]
```

Без selector команда возвращает bounded-карту scope. С selector она объединяет
нынешние `symbol`, `impact`, `graph`, `grep` или `diff` в один отчёт.

Плюсы:

- самая маленькая поверхность: одна команда, одна схема, один budget;
- один scan репозитория вместо нескольких процессов;
- высокая Depth: выбор, source, relations, paths и продолжение скрыты за одним
  Seam;
- легко дать агенту единственную инструкцию использования.

Минусы:

- selector-флаги относятся к разным задачам и со временем могут разрастись;
- «без selector = map» неочевидно без хорошего `--help`;
- единый приблизительный token budget сложнее объяснить и воспроизводить;
- есть риск превратить команду в склад опций, если не ограничить v1.

### Вариант B: один вызов для самого частого сценария

```text
codenav context NAME
codenav context --id ENTITY_ID
codenav context --at PATH:LINE[-LINE]
codenav context --diff FILE|-
codenav context --grep PATTERN

Common:
  --root DIR
  --depth 0..3
  --max-output-bytes N
  --format json|text
```

Module имеет один внешний метод:

```python
class CodeContext:
    def inspect(self, request: ContextRequest) -> ContextReport:
        ...
```

Старые команды становятся compatibility views одного `ContextReport`:
`symbol` показывает target slice, `impact` — direct relations, `graph` — paths,
`diff` — changed slices, `grep` — match slices.

Плюсы:

- частый вызов тривиален: `codenav context RepoIndex`;
- subject явно типизирован, без natural-language магии;
- paths `graph` уже используют relation resolver `impact`, поэтому правила
  разрешения не расходятся;
- точный ID делает следующий запрос дешевле и надёжнее повторного поиска имени;
- multi-file diff можно обработать одним вызовом.

Минусы:

- менее удобен как конструктор нестандартных запросов;
- JSON-envelope многословнее нынешнего compact text;
- evidence-bearing references требуют хранить координаты каждой ссылки, а не
  только множество owner-сущностей;
- один вызов всё равно должен аккуратно ранжировать большой объём данных.

### Вариант C: композиционная алгебра

```text
codenav map
codenav select symbol|text|changed|at|id
codenav expand
codenav read
codenav pack
codenav capabilities
codenav serve --stdio
```

Каждая операция принимает и возвращает versioned `NavBundle`; агент строит
pipeline `select -> expand -> read -> pack`. Долгоживущий stdio Adapter держит
индекс и immutable snapshot в памяти.

Плюсы:

- максимальная гибкость и ясные ортогональные операции;
- snapshot/cursor discipline защищает от смешивания данных до и после правки;
- один selector автоматически сочетается со всеми способами expand/read;
- `serve --stdio` хорошо подходит длинной агентской сессии и не переиндексирует
  репозиторий на каждом шаге;
- overlays позволяют исследовать предполагаемую правку до записи на диск.

Минусы:

- агент должен изучить пять операций и большой bundle;
- shell pipeline передаёт объёмный JSON и между процессами всё равно может
  повторять индексирование;
- `pack`, cache, cursor, snapshot и overlay резко увеличивают Implementation;
- для нынешнего небольшого проекта это слишком широкий первый Interface.

## Рекомендация

Выбрать **вариант B**, добавив в него две идеи из C: `snapshot` в envelope сейчас
и долгоживущий stdio/MCP Adapter позже. Это лучший баланс Depth и простоты:
агент учит одну команду, но Implementation не обещает произвольный query DSL.

Рекомендуемый v1:

```text
codenav context [NAME]
  [--id ENTITY_ID | --at PATH:LINE[-LINE] | --grep REGEX | --diff FILE|-]
  [--root DIR]
  [--depth 0..2]
  [--max-output-bytes N]
  [--format json|text]
```

Правила:

- ровно один subject; отсутствие subject даёт bounded map текущего `--root`;
- новый `context` по умолчанию выдаёт JSON; `--format text` предназначен человеку;
- старые команды пока сохраняются без смены default-формата;
- `--max-output-bytes` называется с единицами, а не притворяется точным
  model-token budget;
- `--depth 0` означает только target/source, `1` добавляет direct relations,
  `2` — bounded paths;
- repo scan, file size, relation и evidence caps имеют безопасные defaults и
  всегда отражаются в `coverage`/`truncation`; скрытых лимитов нет;
- для одного корректно обработанного запроса порядок byte-stable и не зависит
  от `os.walk`, `set` или hash seed.

Не стоит добавлять `--task "исправь авторизацию"`: без embeddings/LSP это создало
бы видимость семантического поиска, которой у инструмента нет.

## Предлагаемый JSON v1

Сокращённый envelope:

```json
{
  "schema": "codenav.context/v1",
  "status": "partial",
  "snapshot": "sha256:...",
  "request": {
    "subject": {"kind": "symbol", "query": "RepoIndex"},
    "root": ".",
    "depth": 1,
    "max_output_bytes": 16384
  },
  "resolution": {
    "status": "resolved",
    "selected_ids": ["e:src/codenav/core.py:621:RepoIndex"],
    "reason": "unique_exact_simple_name"
  },
  "entities": [
    {
      "id": "e:src/codenav/core.py:621:RepoIndex",
      "qualified_name": "RepoIndex",
      "kind": "class",
      "location": {
        "path": "src/codenav/core.py",
        "start_line": 621,
        "end_line": 824
      }
    }
  ],
  "slices": [
    {
      "entity_id": "e:src/codenav/core.py:621:RepoIndex",
      "role": "target",
      "complete": false,
      "included_lines": [[621, 680]],
      "omitted_lines": [[681, 824]],
      "text": "class RepoIndex:\n    ..."
    }
  ],
  "relations": [
    {
      "from_id": "e:src/codenav/cli.py:125:cmd_symbol",
      "to_candidates": ["e:src/codenav/core.py:621:RepoIndex"],
      "resolution": "unique_by_name",
      "extraction": "identifier",
      "evidence": [
        {
          "path": "src/codenav/cli.py",
          "start_line": 126,
          "end_line": 126,
          "text": "index = RepoIndex(args.root)"
        }
      ]
    }
  ],
  "paths": [],
  "coverage": {
    "files_seen": 11,
    "files_parsed": 10,
    "files_skipped": 1,
    "resolution_model": "syntax_and_short_name/v1"
  },
  "truncation": {
    "truncated": true,
    "output_bytes": 15820,
    "limit_bytes": 16384,
    "omitted": {"slices": 1, "relations": 4, "paths": 2}
  },
  "diagnostics": [
    {
      "code": "file_too_large",
      "path": "generated/client.py",
      "message": "Skipped: 734112 bytes exceeds 524288-byte limit"
    }
  ],
  "next_actions": [
    {
      "reason_code": "inspect_direct_dependent",
      "argv": [
        "codenav", "context",
        "--id", "e:src/codenav/cli.py:125:cmd_symbol",
        "--root", ".",
        "--depth", "0"
      ]
    }
  ]
}
```

Ключевое отличие от нынешнего `impact`: relation честно говорит
`unique_by_name`, показывает строку evidence и не называет эвристику call graph.

## Как агент использует результат дальше

### Неоднозначный символ

`resolution.status="ambiguous"` содержит candidates и по одному `next_actions.argv`
для `--id`. Никакого скрытого `found[0]`.

### Усечённый source

`slices[].complete=false` показывает сохранённые и пропущенные диапазоны.
Следующее действие запрашивает тот же ID с `--depth 0` и большим
`--max-output-bytes`.

### Эвристическая связь

Агент видит `extraction`, `resolution` и точную `evidence`. Неоднозначная связь
содержит несколько `to_candidates` и не участвует в multi-hop paths, пока агент
не выберет конкретное определение.

### Неполное покрытие

`coverage` отличает `seen`, `parsed`, `skipped` и `failed`. Если scan cap
достигнут, `next_actions` предлагает тот же запрос с более узким `--root`, а не
молча выдаёт неполную карту.

### Diff

Один `--diff -` возвращает все изменённые файлы. Приоритет данных:

1. changed/added/deleted hunks и affected Entity;
2. source изменённых Entity;
3. direct dependents — сначала то, что может сломаться;
4. direct dependencies;
5. paths, если они помещаются.

Для нового файла возвращаются bounded outline/slices; для удалённого — diff hunks,
но не выдуманный AST текущей рабочей копии.

## Exit и stderr

Для нового agent-first `context`:

- exit `0` — запрос корректно обработан, включая `empty`, `not_found`,
  `ambiguous` и usable `partial`; агент читает `status`;
- exit `2` — неверный selector, regex, line range или несовместимые параметры;
- exit `3` — root/file/grammar недоступны и пригодного отчёта нет;
- exit `70` — дефект Implementation.

JSON на stdout всегда является одним завершённым документом. Stderr содержит
только транспортную диагностику; traceback появляется лишь с `--debug`.

Так ambiguity не превращается для agent runtime в «tool failed»: это полезный
результат с готовым выбором продолжения.

## Seam и внутренняя структура

Внешний Seam — `CodeContext.inspect(ContextRequest) -> ContextReport`. Внутри
Module могут быть отдельные файлы `scan`, `resolve`, `relations`, `slice`,
`budget`, `render`; это не расширяет Interface.

Нужен один внутренний путь:

```text
ReferenceEvidence
    -> resolution candidates
    -> Relation
    -> direct impact / paths / next actions
```

Раньше `impact()` и `_adjacency()` независимо строили почти одну семантику. Это
дублирование дало regression: в `67c8ce3` `graph` падал из-за неопределённого
`defs`. Теперь `graph` получает соседей через `impact_entity()`, а отдельный
name-based `_adjacency()` удалён. Полноценная модель `ReferenceEvidence` всё ещё
остаётся следующим архитектурным шагом.

Filesystem и Tree-sitter — local-substitutable зависимости, их не надо выносить
в публичные ports. Реальные Adapters:

- `argparse`: `argv/stdin -> ContextRequest`;
- JSON и text renderers над одним `ContextReport`;
- позднее stdio/MCP transport над тем же Module.

Disk cache, daemon и plugin system не нужны в v1. Сначала надо измерить стоимость
одного объединённого scan; новый Interface позволяет добавить cache внутри Seam,
не меняя callers.

## Порядок реализации

### Фаза 0: закрепить честный рабочий baseline

- сохранить regression-тесты общего resolver для `impact` и `graph`;
- синхронизировать root help, README и parser;
- сортировать обход файлов и все множества перед выводом;
- заменить traceback для invalid regex/path/grammar на структурированные ошибки;
- добавить `--version` и `codenav capabilities --format json`;
- завести маленькие golden smoke-тесты для каждого заявленного языка и явно
  обозначить уровень поддержки.

### Фаза 1: единая модель фактов

- добавить `EntityId`, `SourceLocation`, `ReferenceEvidence`, `Relation`;
- сохранять координаты identifier/string references;
- сделать ambiguity явной;
- проверить, что каждое ребро имеет evidence.

### Фаза 2: `context` и JSON v1

- реализовать `CodeContext.inspect()`;
- добавить subject `NAME`, `--id`, `--at`, затем `--grep`;
- ввести coverage, diagnostics, deterministic ordering и byte budget;
- сделать text renderer и перевести legacy commands на projections.

### Фаза 3: изменения как связный контекст

- multi-file `--diff`;
- affected Entity, direct dependents first, added/deleted semantics;
- bounded paths и детерминированные next actions.

### Фаза 4: длинная агентская сессия

- после измерений добавить `serve --stdio` или MCP Adapter;
- immutable snapshot, `refresh` после правок и stale-ID diagnostics;
- overlays только если появится реальный сценарий анализа незаписанного кода.

## Проверка готовности

Новый Interface считается готовым, когда выполняются свойства:

- два одинаковых запуска на одном snapshot дают byte-identical JSON;
- ни один ambiguous symbol/reference не выбирается автоматически;
- каждое relation имеет хотя бы одно evidence location;
- любой budget возвращает валидный JSON и точные сведения об omissions;
- partial scan нельзя спутать с полным;
- multi-file diff не требует предварительно извлекать список путей;
- cold grammar failure не печатает traceback;
- legacy text views используют те же IDs, resolution и relation data;
- golden smoke-корпус покрывает все заявленные расширения.
