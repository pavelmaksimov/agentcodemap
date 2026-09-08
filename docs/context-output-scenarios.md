# `codenav context`: реальные сценарии и варианты вывода

Состояние на 2026-08-31: после A/B-теста выбран единый eager workflow
`context`. Progressive-команды `select`, `read`, `expand` удалены из CLI.
Формат `context` пока намеренно не менялся: этот документ фиксирует baseline,
чтобы сначала выбрать улучшения, а затем сравнить варианты на одинаковых данных.

## Текущий Interface

```text
codenav context NAME [--root DIR] [--nodes N]
                [--max-output-bytes N] [--format json|text]
codenav context --id ENTITY_ID [--root DIR] [--nodes N]
                [--max-output-bytes N] [--format json|text]
```

- default `--format json`: compact JSON без завершающего newline;
- `--format text`: человекочитаемая проекция того же report;
- default `--nodes 5`: бюджет различных узлов influence paths;
- default `--max-output-bytes 16384`: budget сначала удаляет paths, затем
  outgoing/incoming relations, source lines, next actions и candidates;
- `NAME` разрешается только при единственном совпадении;
- `--id` адресует точное определение из предыдущего ответа;
- `ok`, `partial`, `ambiguous`, `not_found` — нормальные JSON-результаты с exit 0;
- ошибка выбора аргументов (`NAME` отсутствует либо передан вместе с `--id`)
  возвращает usage exit 2.

Стабильные факты строит `build_context()`. `limit_report()` применяет budget,
а `encode_json()` и `render_text()` являются сменными проекциями. Этот seam
сохранён специально для будущего сравнения вариантов вывода без повторной
реализации анализа репозитория.

## Baseline размеров

Все значения ниже получены реальными вызовами с `--root src/codenav` после
удаления progressive CLI.

| Символ | JSON bytes | Status | Incoming | Outgoing | Paths |
|---|---:|---|---:|---:|---:|
| `_parse_lines_spec` | 1 499 | `ok` | 1 | 0 | 1 |
| `render_outline` | 8 238 | `ok` | 1 | 13 | 1 |
| `slice_diff` | 12 144 | `ok` | 1 | 17 | 3 |
| `RepoIndex` | 16 322 | `partial` | 11 | 5 | 0 |
| `cmd_diff` | 7 128 | `ok` | 1 | 11 | 3 |

Для `RepoIndex` при default budget были исключены 29 outgoing relations и
4 paths; source остался полным.

## Что изменилось относительно baseline

Короткий и важный ответ: **публичный `codenav context` пока не получил новый
формат**. Baseline A оставлен рабочим и неизменным по смыслу; сейчас добавлены
документ, экспериментальные Adapter-ы и contract-тесты. Поэтому таблица ниже
показывает не уже включённый релиз, а измеренные улучшения кандидатов, которые
ещё предстоит выбрать и поднять в production.

| Проблема baseline | Что даёт экспериментальный вывод | Где это есть |
|---|---|---|
| `next_actions` могут остаться после удаления их relation при byte cap | action удаляется вместе с невидимой relation/evidence; dangling-ссылок нет | все четыре Adapter-а + тест |
| `status=partial` одновременно означает resolution и урезанный payload | `outcome` и `completeness` разделены | `evidence`, `hybrid` |
| text печатает размер внутреннего JSON, а не своего stdout | `OUTPUT bytes` проверяется по фактическому UTF-8 text | test Adapter + тест |
| paths используют параллельные `entity_ids` и `labels` | exact IDs остаются в одной структурированной записи пути или relation closure | `evidence`, `hybrid` |
| relation повторяет entity-поля и раздувает ответ | source принадлежит target; relation несёт только нужный контекст | `minimal`, `task`, `hybrid` |
| агенту приходится самому угадывать первый шаг | `attention`, relation-based `next_action` или counts дают явную навигацию | `task`, `hybrid`, частично `evidence` |

Например, при `RepoIndex --max-output-bytes 1024` baseline всё ещё выдаёт
`2` действия к уже скрытым relations. Экспериментальные проекции сохраняют
адрес target и честную отметку truncation, но не выдают такие действия. Это и
есть главное практическое улучшение для агента; меньший размер сам по себе не
считался бы улучшением, если бы вместе с ним исчезали доказательства.

## Эксперимент четырёх проекций через Luna-max

Все варианты прогонялись через один и тот же `build_context()` на пяти символах.
Каждый Adapter получил JSON и text режимы, budgets 16 KiB и 1 KiB. Финальный
post-fix Luna-max metrics-run сделал 2 warmup + 3 timed повтора для каждой пары
variant/symbol; latency собрана по 15 timed samples (три на каждый из пяти
символов).

| Variant | Median JSON 16 KiB | Median JSON 1 KiB | Median text 16 KiB | Median text 1 KiB | Chain latency median/p95 |
|---|---:|---:|---:|---:|---:|
| `minimal` | 7 637 B | 508 B | 4 861 B | 626 B | 86,7 / 112,8 ms |
| `evidence` | 13 438 B | 989 B | 4 862 B | 627 B | 87,4 / 111,0 ms |
| `task` | 8 093 B | 994 B | 4 858 B | 623 B | 89,1 / 126,8 ms |
| `hybrid` | 8 209 B | 808 B | 4 860 B | 625 B | 85,6 / 108,9 ms |

Для каждой строки проверены 5/5 валидных JSON и точное совпадение
`output_bytes` с фактическим UTF-8 размером. В 16 KiB усечение появляется
преимущественно на большом `RepoIndex`; в 1 KiB все варианты честно помечают
`truncated=true`. Это проверка сериализации и контракта, не гарантия полноты
графа.

Метрики — ориентир, а не обещание фиксированной скорости: они зависят от
файловой системы и размера дерева. Для решения важнее контрактные инварианты
ниже и слепая оценка агентом.

### Слепая оценка агентом

Четыре Luna-max агента получили одинаковые задачи: локальная правка
`_parse_lines_spec`, глубокий impact `render_outline`, ambiguity `name` и
`RepoIndex` с 1 KiB cap. Оценивались correctness, decision clarity, evidence
discoverability, payload economy и cap honesty — по 0–2 балла за каждую ось.

| Variant | T1 | T2 | T3 | T4 | Сумма |
|---|---:|---:|---:|---:|---:|
| `minimal` | 9 | 8 | 9 | 7 | **33/40** |
| `evidence` | 10 | 8 | 9 | 5 | **32/40** |
| `hybrid` | 9 | 9 | 8 | 4 | **30/40** |
| `task` | 9 | 8 | 9 | 3 | **29/40** |

Качественный итог:

- `minimal` победил по совокупности: маленький Interface и низкая цена joins;
- `evidence` лучше всего объясняет, почему relation является лишь heuristic,
  но его envelope заметно тяжелее и он не спасает impact под 1 KiB;
- `hybrid` хорошо направляет первый шаг при полном budget, но теряет
  `attention` одновременно с relation при жёстком cap;
- `task` удобен для обычного agent loop, однако natural-language summary
  дублирует facts, а JSON cap оставляет слишком мало доказательств.

Для `RepoIndex` Luna-агенты независимо подтвердили P0: после усечения безопасно
решить только «запросить больший budget», но не делать выводов о влиянии. Это
проверяется отдельным action-ablation и contract-тестами ниже: action допустим
только если его relation/evidence видимы в том же ответе.

### Нужен ли обычный `next_action`

Чтобы не угадывать ответ, первый Luna-max qualitative round отдельно сравнил
`minimal` с `include_actions=True` и `include_actions=False` на тех же четырёх
задачах.
При 1 KiB guidance не спасает impact: relations/evidence исчезают раньше,
поэтому action корректно удаляется вместе с основанием.

| Задача | JSON True/False | Text True/False | Rubric True/False |
|---|---:|---:|---:|
| `_parse_lines_spec` | 875 / 875 B | 400 / 699 B | 3/8 · 4/8 |
| `render_outline` | 548 / 548 B | 415 / 723 B | 3/8 · 3/8 |
| `name` | 692 / 692 B | 307 / 307 B | 7/8 · 7/8 |
| `RepoIndex` | 527 / 527 B | 502 / 686 B | 3/8 · 4/8 |

В полном JSON обычный action помогает локально перейти к caller, но под cap
1 KiB он не даёт безопасного шага. Более того, в text `True` иногда короче:
его action занимает budget и вытесняет source, после чего фильтр всё равно
удаляет действие как dangling. Предварительный вывод: action полезен только
при сохранённом relation/evidence; для cap-недостатка нужен отдельный
`increase_budget`, а не ссылка на скрытый neighbor.

Финальная автоматическая ablation после исправления truncation показывает тот
же trade-off на реальных проектных символах (размеры `True/False`, байты):

| Cap/format | `_parse_lines_spec` | `render_outline` | `RepoIndex` |
|---|---:|---:|---:|
| JSON 16 KiB | 1350 / 1245 | 7637 / 7416 | 16040 / 16219 |
| text 16 KiB | 923 / 808 | 4861 / 4625 | 12472 / 12242 |
| JSON 1 KiB | 979 / 979 | 523 / 523 | 502 / 502 |
| text 1 KiB | 552 / 674 | 390 / 698 | 376 / 661 |

То есть обычные actions дают навигацию только пока ответ достаточно велик;
при жёстком cap они удаляются вместе с невидимой relation, а компактный
recovery action (например, `increase_budget`) должен быть отдельным контрактом.

## Сценарий 1. Локальная правка небольшого helper

Задача агента: найти `_parse_lines_spec`, прочитать реализацию и понять, кто её
вызывает.

```bash
uv run codenav context _parse_lines_spec --root src/codenav --format text
```

Полный реальный stdout:

```text
STATUS ok  WORKFLOW eager_context
TARGET function _parse_lines_spec
  id: e:cli.py:39:_parse_lines_spec
  location: cli.py:39-48
SOURCE complete=true
def _parse_lines_spec(spec: str) -> set[int]:
    """'10,15-20' -> {10, 15..20}"""
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out
RELATIONS analysis=name_based_heuristic
  incoming:
    cmd_diff  cli.py:85-119  unique_by_name
      evidence: cli.py:109  changed = _parse_lines_spec(args.lines)
  outgoing:
PATHS
  main -> cmd_diff -> _parse_lines_spec
COVERAGE files_parsed=5 entities_indexed=102
OUTPUT bytes=1499 limit=16384 truncated=false
NEXT
  inspect_related_symbol: codenav context --id e:cli.py:85:cmd_diff --root src/codenav
```

Как агент использует ответ:

1. `target.id` становится точным адресом символа;
2. `source` достаточно для локальной правки;
3. incoming evidence доказывает реальный call site на `cli.py:109`;
4. готовый `NEXT` позволяет перейти к caller без повторного разрешения имени.

Наблюдение: фактический text stdout занимает 873 байта, однако
`OUTPUT bytes=1499` сообщает размер JSON-проекции. Сейчас поле описывает внутренний
report, а не выданный Adapter-ом результат.

## Сценарий 2. Оценить влияние перед изменением

Задача агента: изменить `render_outline` и проверить callers, dependencies и
influence paths.

```bash
uv run codenav context render_outline --root src/codenav --format text
```

Фрагмент реального stdout; source и длинный хвост relations здесь сокращены
только в документации:

```text
STATUS ok  WORKFLOW eager_context
TARGET function render_outline
  id: e:outline.py:20:render_outline
  location: outline.py:20-62
SOURCE complete=true
def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
...
RELATIONS analysis=name_based_heuristic
  incoming:
    cmd_outline  cli.py:122-131  unique_by_name
      evidence: cli.py:128  outline = render_outline(parsed.entities, path, with_lines=args.lines)
  outgoing:
    module_name  outline.py:12-17  unique_by_name
      evidence: outline.py:48  lines: list[str] = [f"{module_name(file_path)}:"]
    Entity  core.py:210-233  unique_by_name
      evidence: outline.py:20  def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
...
PATHS
  main -> cmd_outline -> render_outline -> module_name -> path
COVERAGE files_parsed=5 entities_indexed=102
OUTPUT bytes=8238 limit=16384 truncated=false
NEXT
  inspect_related_symbol: codenav context --id e:cli.py:122:cmd_outline --root src/codenav
  inspect_related_symbol: codenav context --id e:outline.py:12:module_name --root src/codenav
```

Влияние `--nodes` на тот же реальный report:

| `--nodes` | JSON bytes | Paths в выводе |
|---:|---:|---|
| 3 | 8 162 | `cmd_outline -> render_outline -> Entity` |
| 5 | 8 238 | `main -> cmd_outline -> render_outline -> module_name -> path` |
| 8 | 8 834 | 4 paths |

При `--nodes 8` первые два пути имеют одинаковые labels, но разные точные ID:

```text
... -> module_name -> e:core.py:567:DiffFile.path
... -> module_name -> e:core.py:240:ParsedFile.path
```

Text Adapter показывает оба как `... -> module_name -> path`, поэтому визуально
они выглядят дубликатами. JSON сохраняет различие в `entity_ids`, но заставляет
агента синхронно сопоставлять два параллельных массива.

## Сценарий 3. Неоднозначное имя

Задача агента: пользователь назвал `name`, но в индексе есть два определения.

```bash
uv run codenav context name --root src/codenav | jq
```

Реальный stdout, pretty-printed через `jq`:

```json
{
  "candidates": [
    {
      "id": "e:core.py:207:Slice.name",
      "kind": "attr",
      "location": {"end_line": 207, "path": "core.py", "start_line": 207},
      "name": "name",
      "preview": "name: str  # entity name or \"<unknown>\" / \"<import>\"",
      "qualified_name": "Slice.name"
    },
    {
      "id": "e:core.py:214:Entity.name",
      "kind": "attr",
      "location": {"end_line": 214, "path": "core.py", "start_line": 214},
      "name": "name",
      "preview": "name: str",
      "qualified_name": "Entity.name"
    }
  ],
  "coverage": {"entities_indexed": 102, "files_parsed": 5},
  "next_actions": [
    {
      "argv": ["codenav", "context", "--id", "e:core.py:207:Slice.name", "--root", "src/codenav"],
      "reason": "select_candidate"
    },
    {
      "argv": ["codenav", "context", "--id", "e:core.py:214:Entity.name", "--root", "src/codenav"],
      "reason": "select_candidate"
    }
  ],
  "query": "name",
  "schema": "codenav.agent/v1",
  "status": "ambiguous",
  "truncation": {"limit_bytes": 16384, "omitted": {}, "output_bytes": 896, "truncated": false},
  "workflow": "eager_context"
}
```

Агент не должен выбирать `found[0]`: он сопоставляет задачу с qualified name или
исполняет один из готовых `argv`. Это поведение надо сохранить во всех вариантах.

## Сценарий 4. Большой символ под жёстким byte budget

```bash
uv run codenav context RepoIndex --root src/codenav \
  --max-output-bytes 1024 --format text
```

Полный реальный text stdout:

```text
STATUS partial  WORKFLOW eager_context
TARGET class RepoIndex
  id: e:core.py:621:RepoIndex
  location: core.py:621-835
SOURCE complete=false
class RepoIndex:
... 213 lines omitted ...
        return [[rep[q] for q in path] for path in picked]
RELATIONS analysis=name_based_heuristic
  incoming:
  outgoing:
PATHS
COVERAGE files_parsed=5 entities_indexed=102
OUTPUT bytes=982 limit=1024 truncated=true
NEXT
  inspect_related_symbol: codenav context --id e:agent.py:42:_entities --root src/codenav
  inspect_related_symbol: codenav context --id e:agent.py:49:_resolve --root src/codenav
```

Соответствующий JSON сообщает, что именно исключено:

```json
{
  "status": "partial",
  "source": {"complete": false},
  "truncation": {
    "limit_bytes": 1024,
    "omitted": {
      "incoming": 11,
      "outgoing": 34,
      "paths": 4,
      "source_lines": 213
    },
    "output_bytes": 982,
    "truncated": true
  }
}
```

Здесь видны два дефекта текущей проекции:

- text Adapter скрывает `omitted`, оставляя только `truncated=true`;
- `NEXT` ссылается на relations, которые уже удалены budget-ом, поэтому агент
  не видит evidence и причину предложенного перехода.

## Сценарий 5. Символ не найден

```bash
uv run codenav context DefinitelyMissingSymbol --root src/codenav | jq
```

Полный реальный результат:

```json
{
  "candidates": [],
  "coverage": {"entities_indexed": 102, "files_parsed": 5},
  "next_actions": [],
  "query": "DefinitelyMissingSymbol",
  "schema": "codenav.agent/v1",
  "status": "not_found",
  "truncation": {
    "limit_bytes": 16384,
    "omitted": {},
    "output_bytes": 283,
    "truncated": false
  },
  "workflow": "eager_context"
}
```

`not_found` возвращает exit 0 как нормальный результат поиска. Агенту не нужно
отделять stderr от stdout или разбирать сообщение об ошибке.

## Сценарий 6. Точный переход по ID

Команда из `next_actions` предыдущего ответа:

```bash
uv run codenav context --id e:cli.py:85:cmd_diff --root src/codenav
```

Проекция реального результата:

```json
{
  "status": "ok",
  "target": {
    "id": "e:cli.py:85:cmd_diff",
    "kind": "function",
    "location": {"path": "cli.py", "start_line": 85, "end_line": 119},
    "qualified_name": "cmd_diff"
  },
  "source": {
    "complete": true,
    "first_line": "def cmd_diff(args: argparse.Namespace) -> None:"
  },
  "relation_counts": {"incoming": 1, "outgoing": 11},
  "truncation": {"output_bytes": 7128, "truncated": false}
}
```

Это основной механизм навигации между ответами. Формат ID можно менять только с
версией schema или с поддержкой старых ID.

## Что в текущем выводе мешает агенту

| Приоритет | Наблюдение | Последствие |
|---|---|---|
| P0 | `next_actions` вычисляются до truncation | Возможен переход по скрытой relation без evidence |
| P0 | В text скрыта карта `omitted` | Агент-человек не знает, чего именно не хватает |
| P1 | `status=partial` смешивает успешное разрешение цели и неполноту payload | Один статус описывает две независимые оси |
| P1 | JSON сортирует ключи алфавитно | В raw stdout `coverage` идёт раньше `target` и `source` |
| P1 | `OUTPUT bytes` в text означает размер JSON | Метрика не соответствует Adapter-у |
| P1 | Paths содержат параллельные `entity_ids` и `labels` | Нужен positional join; разные ID выглядят одинаково в text |
| P2 | `target.location` повторяется в `source.location` | Лишние байты почти в каждом успешном ответе |
| P2 | Relation повторяет полный entity record | Высокая цена на связных символах |
| P2 | `workflow=eager_context` больше ничего не различает | Поле стало постоянным после удаления B |
| P2 | `coverage` не сообщает skipped files | Нельзя отличить полный scan от частичного |

## Кандидаты нового Interface

Ниже сравниваются четыре формы. Они меняют только проекцию общего
context Module; новый CLI-командный набор, cache, daemon или MCP не требуются.
JSON в этом разделе — иллюстрации предлагаемых контрактов, а не вывод уже
реализованных команд. Реальным baseline являются сценарии выше.

### Вариант M. Minimal Context

Цель: минимизировать Interface и payload, не теряя source, evidence и paths.
Две смысловые секции — `result` и `meta`:

```json
{
  "schema": "codenav.context/v2",
  "status": "ok",
  "result": {
    "target": {
      "id": "e:cli.py:39:_parse_lines_spec",
      "name": "_parse_lines_spec",
      "kind": "function",
      "location": {"path": "cli.py", "start_line": 39, "end_line": 48},
      "source": "def _parse_lines_spec(spec: str) -> set[int]:\n    ..."
    },
    "relation_analysis": "name_based_heuristic",
    "relations": [
      {
        "direction": "incoming",
        "entity_id": "e:cli.py:85:cmd_diff",
        "name": "cmd_diff",
        "kind": "function",
        "location": {"path": "cli.py", "start_line": 85, "end_line": 119},
        "resolution": "unique_by_name",
        "evidence": [
          {"path": "cli.py", "line": 109, "text": "changed = _parse_lines_spec(args.lines)"}
        ]
      }
    ],
    "paths": [
      {
        "entity_ids": ["e:cli.py:245:main", "e:cli.py:85:cmd_diff", "e:cli.py:39:_parse_lines_spec"],
        "names": ["main", "cmd_diff", "_parse_lines_spec"]
      }
    ]
  },
  "meta": {
    "coverage": {"files_parsed": 5, "files_skipped": 0, "entities_indexed": 102},
    "truncation": {"limit_bytes": 16384, "output_bytes": 1181, "omitted": {}}
  }
}
```

Инварианты и решения:

- `status` описывает только resolution: `ok`, `ambiguous`, `not_found`;
- неполнота живёт только в `meta.truncation.omitted`;
- source вложен в target, дублирующая location удалена;
- incoming/outgoing объединены в один список с `direction`;
- обычные эвристические `next_actions` удалены; action появляется только как
  восстановительная команда, например увеличить budget;
- `target`, coverage и truncation не удаляются;
- если минимальный envelope не помещается, stdout пуст, exit 1.

Плюсы: самый маленький Interface, один проход потребителя, ориентировочно
1 181 B вместо 1 499 B на `_parse_lines_spec` (около −21%). Минусы: relation
records продолжают повторять entity fields; исчезает удобный переход к caller;
параллельные ID/names в path сохраняют positional join.

Depth высокая за счёт малого Interface. Locality хорошая: resolution, budget и
сортировка остаются за одним seam. Этот вариант проще всего реализовать и
проверить.

### Вариант E. Evidence Ledger

Цель: сделать каждое графовое утверждение проверяемым и никогда не выдавать
name match за доказанный call graph.

```json
{
  "schema": "codenav.context/v2",
  "status": {"outcome": "resolved", "completeness": "complete"},
  "resolution": {
    "method": "exact_name",
    "candidate_count": 1,
    "selected_entity_id": "e:cli.py:39:_parse_lines_spec"
  },
  "analysis": {
    "relation_method": "parsed_reference_name_match",
    "semantic_binding": false,
    "claim_strength": "heuristic"
  },
  "target": "e:cli.py:39:_parse_lines_spec",
  "entities": [
    {
      "id": "e:cli.py:39:_parse_lines_spec",
      "roles": ["target", "path_node"],
      "qualified_name": "_parse_lines_spec",
      "kind": "function",
      "location": {"path": "cli.py", "start_line": 39, "end_line": 48}
    },
    {
      "id": "e:cli.py:85:cmd_diff",
      "roles": ["relation_candidate", "path_node"],
      "qualified_name": "cmd_diff",
      "kind": "function",
      "location": {"path": "cli.py", "start_line": 85, "end_line": 119}
    }
  ],
  "source": {
    "entity_id": "e:cli.py:39:_parse_lines_spec",
    "complete": true,
    "chunks": [
      {"start_line": 39, "end_line": 48, "text": "def _parse_lines_spec(spec: str) -> set[int]:\n    ..."}
    ]
  },
  "evidence": [
    {
      "id": "ev:0",
      "kind": "lexical_name_occurrence",
      "owner_entity_id": "e:cli.py:85:cmd_diff",
      "location": {"path": "cli.py", "line": 109},
      "snippet": "changed = _parse_lines_spec(args.lines)"
    }
  ],
  "relations": [
    {
      "id": "rel:0",
      "from_entity_id": "e:cli.py:85:cmd_diff",
      "candidate_entity_ids": ["e:cli.py:39:_parse_lines_spec"],
      "resolution": {
        "state": "unique_candidate",
        "candidate_count": 1,
        "semantic_binding": false
      },
      "evidence_ids": ["ev:0"],
      "roles": ["incoming_to_target", "path_step"]
    }
  ],
  "paths": [
    {"id": "path:0", "entity_ids": ["e:cli.py:85:cmd_diff", "e:cli.py:39:_parse_lines_spec"], "relation_ids": ["rel:0"]}
  ],
  "omissions": [],
  "next_action": {
    "kind": "inspect_related_entity",
    "based_on_relation_id": "rel:0",
    "argv": ["codenav", "context", "--id", "e:cli.py:85:cmd_diff", "--root", "src/codenav"]
  }
}
```

Инварианты и решения:

- outcome и completeness — независимые оси;
- любая ссылка на entity/evidence/relation обязана разрешаться внутри ответа;
- path состоит из явных relation steps, скрытых рёбер нет;
- `unique_candidate` означает одно совпадение имени, а не semantic binding;
- несколько одноимённых candidates группируются в одной relation;
- truncation удаляет замыкание ссылок, а не отдельные list items;
- source режется line-addressable chunks, без синтетического сдвига номеров;
- next action может опираться только на сохранённую relation и evidence.

Плюсы: максимальная аудируемость, отсутствие дублированного evidence, честные
ambiguous edges и безопасная truncation. Минусы: самый тяжёлый envelope, агент
должен соединять несколько ID-таблиц, limiter становится сложнее. Это сильный
эталон корректности, но может оказаться слишком широким Interface для обычной
локальной правки.

Depth высокая по возможностям, но leverage снижается из-за цены изучения схемы.
Locality всё ещё хорошая: нормализация и referential integrity сосредоточены в
одном context Module.

### Вариант T. Task-first Briefing

Цель: оптимизировать самый частый caller — «понять символ перед изменением».
Первые поля сразу говорят, куда агенту смотреть.

```json
{
  "schema": "codenav.context/v2",
  "status": "ok",
  "summary": "_parse_lines_spec: source complete; review 1 caller before changing behavior.",
  "next_action": {
    "kind": "review_relation",
    "relation_index": 0,
    "reason": "cmd_diff calls the target at cli.py:109"
  },
  "target": {
    "entity_id": "e:cli.py:39:_parse_lines_spec",
    "qualified_name": "_parse_lines_spec",
    "kind": "function",
    "location": {"path": "cli.py", "start_line": 39, "end_line": 48}
  },
  "source": {"complete": true, "text": "def _parse_lines_spec(spec: str) -> set[int]:\n    ..."},
  "relations": {
    "analysis": "name_based_heuristic",
    "counts": {
      "incoming": {"found": 1, "shown": 1},
      "outgoing": {"found": 0, "shown": 0}
    },
    "items": [
      {
        "direction": "incoming",
        "entity_id": "e:cli.py:85:cmd_diff",
        "qualified_name": "cmd_diff",
        "resolution": "unique_by_name",
        "evidence": [
          {"path": "cli.py", "line": 109, "text": "changed = _parse_lines_spec(args.lines)"}
        ]
      }
    ]
  },
  "paths": {
    "counts": {"reported": 1, "shown": 1},
    "items": [
      {"entity_ids": ["e:cli.py:245:main", "e:cli.py:85:cmd_diff", "e:cli.py:39:_parse_lines_spec"], "display": "main -> cmd_diff -> _parse_lines_spec"}
    ]
  },
  "truncation": {"truncated": false, "limit_bytes": 16384, "omitted": []}
}
```

Инварианты и решения:

- semantic status не меняется при truncation;
- `next_action` всегда один и указывает только на показанный item;
- `found` считается до budget, `shown` — после;
- source принадлежит target, отдельная location не повторяется;
- JSON физически выводится в порядке «решение → детали → диагностика»;
- при малом budget сначала остаются target, counts, source signature и по одному
  evidenced incoming/outgoing; paths имеют низший приоритет.

Плюсы: первые поля хорошо направляют agent loop, counts делают потери видимыми,
нет dangling action. Минусы: natural-language `summary` дублирует facts;
единственный action кодирует мнение эвристики; payload может стать больше v1.

Depth максимальна для common caller, но меньше для универсального потребителя:
summary и action добавляют task policy в Interface. Locality хорошая, если policy
генерируется в Module, а не в CLI Adapter.

### Сравнение вариантов

| Свойство | M: Minimal | E: Ledger | T: Task-first | H: Hybrid |
|---|---|---|---|---|
| Маленький payload | Лучший | Худший | Средний | Средний |
| Первый следующий шаг | Только recovery | Через relation ID | Явный и первый | Через `attention` + relation ID |
| Честность graph | Как v1, но компактнее | Максимальная | Средняя | Высокая |
| Truncation safety | Простая | Referential closure | Counts + valid action | Inline relation closure |
| Работа с ambiguity relations | Отдельные records | Группа candidates | Как v1 | Relation ID + candidates |
| Сложность для агента | Низкая | Высокая, несколько joins | Низкая | Средняя |
| Сложность реализации | Низкая | Высокая | Средняя | Средняя |
| Seam | Тот же report Adapter | Нормализованный fact ledger | Report + task policy | Report + structured attention |

С точки зрения depth вариант M даёт наибольшее leverage на единицу Interface.
E прячет больше сложной реализации, но заставляет caller изучить больше схемы.
T лучше всего обслуживает common caller, однако делает task policy частью
Interface. Во всех четырёх locality сохраняется, если CLI остаётся тонким Adapter-ом.

## Моя предварительная рекомендация

Первый раунд уже охватил M/E/T/H и action-ablation. Я бы не реализовывал все
четыре целиком; для следующего раунда достаточно двух чистых проекций одного
report:

1. **M+** — Minimal Context как нижняя граница payload, но сохранить готовый
   `argv` внутри первой показанной relation с evidence. Так action не требует
   отдельной ссылки и исчезает вместе с relation при truncation.
2. **H** — лёгкий гибрид M и E: inline relation/evidence без глобального evidence
   registry, стабильный `relation_id`, двумерный status, paths по exact IDs и
   структурированный `attention` вместо natural-language summary.

Предлагаемая форма H:

```json
{
  "schema": "codenav.context/v2",
  "status": {"outcome": "resolved", "completeness": "complete"},
  "target": {"id": "e:cli.py:39:_parse_lines_spec", "qualified_name": "_parse_lines_spec", "source": "..."},
  "attention": [
    {"kind": "review_caller", "relation_id": "rel:0"}
  ],
  "relations": [
    {
      "id": "rel:0",
      "direction": "incoming",
      "entity": {"id": "e:cli.py:85:cmd_diff", "qualified_name": "cmd_diff", "location": "cli.py:85-119"},
      "resolution": "unique_name_candidate",
      "semantic_binding": false,
      "evidence": [{"path": "cli.py", "line": 109, "text": "changed = _parse_lines_spec(args.lines)"}],
      "argv": ["codenav", "context", "--id", "e:cli.py:85:cmd_diff", "--root", "src/codenav"]
    }
  ],
  "paths": [["e:cli.py:245:main", "e:cli.py:85:cmd_diff", "e:cli.py:39:_parse_lines_spec"]],
  "meta": {"coverage": {"files_parsed": 5}, "truncation": {"omitted": []}}
}
```

Почему не брать E целиком: его referential integrity полезна как проверяемый
инвариант, но отдельные registries `entities/evidence/relations` требуют слишком
много joins. Почему не брать T целиком: структурированный `attention` легче
проверить и дешевле, чем дублирующий natural-language summary.

Главные вопросы для пользователя перед реализацией тестовых проекций:

- нужен ли normal `next_action`, или агент сам выбирает relation;
- важнее минимальный payload M+ или явное `attention` H;
- допустим ли один join по `relation_id`, если он гарантирует отсутствие
  dangling actions;
- сохранять ли text Adapter или после выбора оставить только JSON.

## Инфраструктура будущего теста

После выбора 1–2 кандидатов не следует добавлять публичные `--format v2a|v2b`.
Каждый кандидат лучше реализовать как чистую test-only проекцию одного report;
победитель затем заменит текущие `encode_json()`/`render_text()`.

Сохранённые seams и fixtures:

- `build_context()` — единый набор фактов;
- `limit_report()` — текущий baseline budget;
- `encode_json()` / `render_text()` — заменяемые Adapter-ы;
- `experiments/context_output_variants.py` — четыре disposable JSON/text
  проекции и action-ablation `include_actions=True|False`;
- `tests/test_agent_workflows.py::CHAIN` — компактный граф fixture;
- `tests/test_context_output_variants.py` — общий contract/matrix harness;
- проверки ambiguity, exact-ID roundtrip, byte cap и selector usage;
- историческая методика и latency baseline в
  [agent workflow benchmark](agent-workflow-benchmark.md).

Запуск текущего набора:

```bash
uv run pytest -q tests/test_context_output_variants.py  # 43 passed
uv run pytest -q                                      # 81 passed
```

Экспериментальные Adapter-ы не подключены к публичному CLI: это позволяет
менять форму payload и прогонять одинаковый contract, не ломая выбранный A.
Когда вариант будет выбран, его можно поднять в `codenav.agent` одним заменяемым
seam и удалить disposable-проекцию после миграционного теста.

Матрица будущего сравнения:

| Case | Что проверяет |
|---|---|
| `_parse_lines_spec` | Компактный helper, один caller, полный source |
| `render_outline` | Много relations, evidence и неоднозначные attrs |
| `slice_diff` | Средний source и несколько paths |
| `RepoIndex` | Большой source, truncation 16 KiB и 1 KiB |
| `name` | Ambiguity и исполняемые candidate actions |
| missing symbol | `not_found` без prose parsing |
| exact ID | Стабильный переход между context-вызовами |

Для каждого кандидата:

1. semantic parity по target/source/relations/evidence/paths;
2. JSON validity и детерминированная длина;
3. полный stdout не превышает byte cap;
4. нет actions на скрытые evidence/relations;
5. text и JSON честно сообщают собственные bytes и omissions;
6. 2 warmup + 10 отдельных CLI-процессов на символ;
7. одинаковые агентские задачи: локальная правка, impact-анализ, ambiguity;
8. статистика calls, bytes, latency, правильных решений и неиспользованных
   секций.

Обязательный порог для любого победителя: 100% semantic parity, 0 dangling
actions, 0 invalid JSON и сохранение usage exit 2. Затем сравниваются payload и
удобство; экономия байтов не может покупать скрытую потерю evidence.
