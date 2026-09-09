# codenav: команды в сценариях AI-агента

> Исторический документ A/B-эксперимента. Выбран eager `context`; команды
> `select`, `read` и `expand` удалены из текущего CLI. Актуальные примеры и
> варианты нового вывода: [context output scenarios](context-output-scenarios.md).

Этот документ использовался для выбора одного из двух экспериментальных workflow.
Примеры получены реальными запусками рабочего дерева поверх commit `06f3cf3`.
Обе версии были реализованы и использовали общий анализ, ID, JSON schema и renderers.

> Обновление 2026-09-08: `--nodes` теперь означает максимум узлов в каждой
> цепочке. Старые примеры общего бюджета ниже оставлены как данные эксперимента.

> Обновление 2026-09-09: у `graph`/`trace`/`info` флаг переименован в `--depth`
> (семантика и числа те же; `context --nodes` не менялся). Примеры
> `graph ... --nodes N` в сценарии 7 соответствуют текущему поведению при
> `--depth N`.

## Версия A: eager context

Один вызов сразу возвращает source, direct relations и bounded paths:

```text
codenav context NAME [--root DIR] [--nodes N] [--max-output-bytes N] [--format json|text]
codenav context --id ENTITY_ID [--root DIR] [--nodes N] [--max-output-bytes N] [--format json|text]
```

Она заменяет последовательность:

```text
symbol NAME -> impact NAME -> graph NAME
```

Сильная сторона — один process и один scan. Слабая — команда может принести
relations и paths, которые агенту для конкретной задачи не понадобятся.

## Версия B: progressive navigation

Агент сначала выбирает точное определение, затем запрашивает только нужную часть:

```text
codenav select NAME [--root DIR] [--max-output-bytes N] [--format json|text]
codenav read ENTITY_ID [--root DIR] [--max-output-bytes N] [--format json|text]
codenav expand ENTITY_ID [--root DIR] [--nodes N] [--max-output-bytes N] [--format json|text]
```

Типичный полный маршрут:

```text
select NAME -> read ENTITY_ID -> expand ENTITY_ID
```

`select` возвращает preview и готовые `next_actions.argv`. Агент может остановиться
после locate, выполнить только `read` для локальной правки или добавить `expand`
для анализа влияния. Сильная сторона — progressive disclosure и меньший лишний
вывод. Слабая — 2–3 process и повторное индексирование `--root`.

## Общие решения прототипа

- `--format json` — default, потому что команда предназначена агенту;
- `--format text` — представление того же результата для человека;
- `--nodes 5` — существующий default для числа уникальных узлов paths;
- `--max-output-bytes 16384` — общий default; низкоприоритетные элементы
  исключаются целиком, а результат остаётся валидным JSON и сообщает omissions;
- один `RepoIndex` и одно разрешение символа на весь вызов;
- при нескольких определениях результат `ambiguous`, а не молчаливый `found[0]`;
- каждое определение получает `ENTITY_ID` вида
  `e:outline.py:20:render_outline` относительно `--root`;
- связи явно помечены как `name_based_heuristic`;
- expected `ok`/`ambiguous`/`not_found` возвращаются как JSON, без prose parsing.

В обе версии намеренно не добавлены `--diff`, `--grep`, `--at`, query DSL, cache,
daemon, MCP, snapshot или overlays. Для discovery остаются `outline`/`grep`, для
изменений — `diff`.

## Сценарий 1. Найти точку входа в незнакомом модуле

Задача агента: «Измени формат outline, но сначала найди код, который его строит».

### Шаг 1: получить адресуемую карту

```bash
uv run codenav outline src/codenav/outline.py --lines
```

Реальный stdout:

```text
src.codenav.outline:
A LETTERS  L9-9

F module_name  L12-17

F render_outline  L20-62
```

Как агент использует результат:

- выбирает `render_outline`, а не читает весь файл;
- знает точный диапазон `L20-62` для следующего чтения или патча;
- при неясном имени может сначала выполнить `grep`.

### Шаг 2: найти определения и употребления

```bash
uv run codenav grep render_outline src/codenav
```

Реальный stdout:

```text
src/codenav/cli.py
134         outline = render_outline(parsed.entities, path, with_lines=args.lines)
---
src/codenav/cli.py
42  from codenav.outline import render_outline
---
src/codenav/outline.py
20  def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
```

Как агент использует результат:

- строка 20 — определение;
- строка 134 — реальный caller, который надо учитывать при изменении Interface;
- строка 42 — только import, поэтому она менее важна, чем call site.

`outline` и `grep` здесь не надо прятать внутрь `context`: это дешёвые discovery
команды с разными входами.

## Сценарий 2. Понять символ перед изменением

Задача агента: «Измени `render_outline`, оцени влияние».

### Сейчас: три независимых вызова

Первый вызов читает исходник:

```bash
uv run codenav symbol render_outline --root src/codenav
```

Начало реального stdout:

```text
### render_outline  (src/codenav/outline.py:20-62, function)
20  def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
21      """Format::
...
62      return "\n".join(lines)
```

Второй вызов даёт прямые именные связи:

```bash
uv run codenav impact render_outline --root src/codenav
```

Фрагмент реального stdout:

```text
impact chain for render_outline (src/codenav/outline.py:20-62):
  depends-on:
    Entity  (src/codenav/core.py:210-233, class)
    module_name  (src/codenav/outline.py:12-17, function)
    LETTERS  (src/codenav/outline.py:9-9, constant)
  dependents:
    cmd_outline  (src/codenav/cli.py:128-137, function)
```

Фактический полный вывод также содержит `Slice.start_line`, `Slice.kind` и другие
совпадения по коротким именам. Агент не должен принимать список за точный call
graph: это кандидаты для проверки.

Третий вызов показывает bounded path:

```bash
uv run codenav graph render_outline --root src/codenav
```

Реальный stdout:

```text
render_outline: main -> cmd_outline -> render_outline -> module_name -> path
```

Проблема для агента: он сам сшивает три формата, повторно индексирует `--root` и
не видит, где именно доказана каждая связь.

### Версия A: один вызов

```bash
uv run codenav context render_outline --root src/codenav --format text
```

Фрагмент реального text stdout (source и часть outgoing сокращены только в
документации):

```text
STATUS ok  WORKFLOW eager_context
TARGET function render_outline
  id: e:outline.py:20:render_outline
  location: outline.py:20-62

SOURCE complete=true
def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
...
    return "\n".join(lines)

RELATIONS analysis=name_based_heuristic
  incoming:
    cmd_outline  cli.py:128-137  unique_by_name
      evidence: cli.py:134  outline = render_outline(...)
  outgoing:
    module_name  outline.py:12-17  unique_by_name
      evidence: outline.py:48  module_name(file_path)
    Entity  core.py:210-233  unique_by_name
      evidence: outline.py:20  list[Entity]

PATHS
  main -> cmd_outline -> render_outline -> module_name -> path

COVERAGE files_parsed=5 entities_indexed=109
OUTPUT bytes=8238 limit=16384 truncated=false

NEXT
  inspect_related_symbol: codenav context --id e:cli.py:128:cmd_outline --root src/codenav
  inspect_related_symbol: codenav context --id e:outline.py:12:module_name --root src/codenav
```

Как агент использует результат:

1. читает target source;
2. проверяет `analysis=name_based_heuristic` и не переоценивает graph;
3. смотрит evidence прямого caller на строке 134;
4. перед правкой при необходимости выполняет один готовый `NEXT`.

## Сценарий 3. Машиночитаемый результат для tool loop

```bash
uv run codenav context _parse_lines_spec --root src/codenav
```

Default stdout — compact JSON в одну строку. Ниже сокращённая pretty-printed
проекция реального результата: `source`, часть полей вложенных `entity` и
`path.entity_ids` опущены только ради длины документа.

```json
{
  "schema": "codenav.agent/v1",
  "workflow": "eager_context",
  "status": "ok",
  "target": {
    "id": "e:cli.py:45:_parse_lines_spec",
    "qualified_name": "_parse_lines_spec",
    "kind": "function",
    "location": {
      "path": "cli.py",
      "start_line": 45,
      "end_line": 54
    }
  },
  "relations": {
    "analysis": "name_based_heuristic",
    "incoming": [
      {
        "entity": {
          "id": "e:cli.py:91:cmd_diff",
          "qualified_name": "cmd_diff"
        },
        "resolution": "unique_by_name",
        "evidence": [
          {
            "path": "cli.py",
            "line": 115,
            "text": "changed = _parse_lines_spec(args.lines)"
          }
        ]
      }
    ],
    "outgoing": []
  },
  "paths": [
    {
      "labels": ["main", "cmd_diff", "_parse_lines_spec"]
    }
  ],
  "coverage": {
    "files_parsed": 5,
    "entities_indexed": 109
  },
  "truncation": {
    "truncated": false,
    "limit_bytes": 16384,
    "output_bytes": 1499,
    "omitted": {}
  },
  "next_actions": [
    {
      "reason": "inspect_related_symbol",
      "argv": [
        "codenav", "context", "--id", "e:cli.py:91:cmd_diff",
        "--root", "src/codenav"
      ]
    }
  ]
}
```

Почему агенту удобен именно такой JSON:

- не надо парсить заголовки и отступы трёх команд;
- `entity_id` различает одноимённые определения;
- `evidence` позволяет проверить связь до перехода по ней;
- `coverage` показывает объём parsed/indexed; учёт skipped files пока остаётся
  ограничением прототипа;
- `argv` можно передать следующему tool call без shell parsing.

## Сценарий 4. Progressive disclosure вместо eager bundle

Та же задача для версии B начинается с компактного выбора:

```bash
uv run codenav select _parse_lines_spec --root src/codenav
```

Сокращённый реальный JSON:

```json
{
  "schema": "codenav.agent/v1",
  "workflow": "progressive_select",
  "status": "ok",
  "target": {
    "id": "e:cli.py:45:_parse_lines_spec",
    "preview": "def _parse_lines_spec(spec: str) -> set[int]:"
  },
  "next_actions": [
    {
      "reason": "read_source",
      "argv": ["codenav", "read", "e:cli.py:45:_parse_lines_spec", "--root", "src/codenav"]
    },
    {
      "reason": "expand_relations",
      "argv": ["codenav", "expand", "e:cli.py:45:_parse_lines_spec", "--root", "src/codenav"]
    }
  ],
  "truncation": {"output_bytes": 696, "truncated": false}
}
```

Если задача локальная, агент читает только source:

```bash
uv run codenav read e:cli.py:45:_parse_lines_spec --root src/codenav
```

`progressive_read` вернул 964 байта. Если нужно влияние:

```bash
uv run codenav expand e:cli.py:45:_parse_lines_spec --root src/codenav
```

`progressive_expand` вернул 1058 байт с relation
`cmd_diff -> _parse_lines_spec`, evidence на строке 115 и path
`main -> cmd_diff -> _parse_lines_spec`.

Как агент выбирает глубину:

- достаточно найти определение — остановиться после `select`;
- требуется только правка тела — `select -> read`;
- нужен полный анализ — `select -> read -> expand`;
- ID уже известен из прошлого результата — сразу `read` или `expand`.

На этом компактном символе eager `context` дал 1499 байт одним вызовом, а полный
progressive-маршрут — 2718 байт тремя вызовами из-за повторяющихся envelopes.
Зато locate-only progressive ответ занял 696 байт. Это и есть проверяемый выбор:
минимум вызовов против отсутствия ненужного анализа.

## Сценарий 5. Неоднозначное имя

Задача агента: в двух файлах есть `handler`; пользователь просит изменить один из
них. Текущий `symbol handler` молча выбирает первое определение.

Вызов на репозитории с двумя определениями:

```bash
uv run codenav context handler --root src
```

Форма JSON, проверенная `test_ambiguous_name_requires_exact_id`:

```json
{
  "schema": "codenav.agent/v1",
  "workflow": "eager_context",
  "status": "ambiguous",
  "query": "handler",
  "candidates": [
    {
      "id": "e:api.py:18:handler",
      "kind": "function",
      "location": {"path": "api.py", "start_line": 18, "end_line": 31}
    },
    {
      "id": "e:worker.py:42:handler",
      "kind": "function",
      "location": {"path": "worker.py", "start_line": 42, "end_line": 67}
    }
  ],
  "next_actions": [
    {
      "reason": "select_candidate",
      "argv": ["codenav", "context", "--id", "e:api.py:18:handler"]
    },
    {
      "reason": "select_candidate",
      "argv": ["codenav", "context", "--id", "e:worker.py:42:handler"]
    }
  ]
}
```

Как агент использует результат: сопоставляет путь с задачей пользователя или
читает короткие candidate previews; ни один кандидат не выбирается скрыто.
Корректно обработанная ambiguity возвращает exit 0 — это полезный результат, а
не поломка инструмента.

## Сценарий 6. Понять контекст изменённой строки

Задача агента: «Что затронет изменение строки 12 в `outline.py`?»

### Текущая команда

```bash
uv run codenav diff src/codenav/outline.py --lines 12
```

Реальный stdout:

```text
### L12-17  [function module_name]
12  def module_name(file_path: str) -> str:
13      base = file_path
14      if base.startswith("./"):
15          base = base[2:]
16      base = os.path.splitext(base)[0]
17      return base.replace(os.sep, ".").replace("/", ".").strip(".") or "<module>"
```

Следующий агентский шаг с версией A:

```bash
uv run codenav context module_name --root src/codenav
```

То есть `diff` отвечает «какой символ изменён», а `context` — «что это за символ
и куда ведут его связи». Не нужно добавлять `--diff` в первый релиз `context`.

## Сценарий 7. Управлять шириной graph

Маленький бюджет оставляет только одну короткую цепочку:

```bash
uv run codenav graph render_outline --root src/codenav --nodes 3
```

```text
render_outline: cmd_outline -> render_outline -> Entity
```

Больший бюджет показывает несколько кандидатов:

```bash
uv run codenav graph render_outline --root src/codenav --nodes 8
```

```text
render_outline: main -> cmd_outline -> render_outline -> module_name -> path, main -> cmd_outline -> render_outline -> Entity, main -> cmd_outline -> render_outline -> end_line
```

Как агент использует опцию:

- `--nodes 3` — быстрый ориентир перед локальной правкой;
- default `--nodes 5` — обычный `context`;
- `--nodes 8` и выше — исследование, когда агент готов проверять больше
  эвристических связей по исходнику.

## Что измерять после выбора

Синтетическое A/B-сравнение уже выполнено и вынесено в
[agent workflow benchmark](agent-workflow-benchmark.md). После выбора одной
версии следующие расширения стоит принимать по реальному agent loop:

- сколько вызовов `symbol + impact + graph` заменил один `context`;
- сколько байт/токенов занял JSON по сравнению с тремя text-ответами;
- как часто `next_actions` действительно исполняются;
- как часто встречается `ambiguous`;
- сколько relations агент отбрасывает после проверки evidence;
- время одного объединённого scan.

Если будет выбрана A и реальные логи подтвердят частый запрос полного контекста,
следующий кандидат — `context --diff -`. Если будет выбрана B и главным узким
местом окажется повторное индексирование длинной сессии — stdio/MCP Adapter.
До таких измерений не нужны оба расширения сразу.
