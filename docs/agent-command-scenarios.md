# codenav: команды в сценариях AI-агента

Этот документ нужен для выбора следующей доработки. Примеры в разделе
«Текущие команды» получены реальными запусками на commit `c0b2f11`. Примеры
`context` — предлагаемый контракт: такой команды пока нет.

## Решение для первого релиза

Не объединять сразу `outline`, `grep`, `diff` и все будущие способы поиска.
Сначала добавить одну глубокую команду для самого частого сценария:

```text
codenav context NAME [--root DIR] [--nodes N] [--max-output-bytes N] [--format json|text]
codenav context --id ENTITY_ID [--root DIR] [--nodes N] [--max-output-bytes N] [--format json|text]
```

Она заменяет последовательность:

```text
symbol NAME -> impact NAME -> graph NAME
```

Решения v1:

- `--format json` — default, потому что команда предназначена агенту;
- `--format text` — представление того же результата для человека;
- `--nodes 5` — существующий default для числа уникальных узлов paths;
- `--max-output-bytes 16384` — общий default; низкоприоритетные элементы
  исключаются целиком, а результат остаётся валидным JSON и сообщает omissions;
- один `RepoIndex` и одно разрешение символа на весь вызов;
- при нескольких определениях результат `ambiguous`, а не молчаливый `found[0]`;
- каждое определение получает `ENTITY_ID` вида
  `src/codenav/outline.py:20:render_outline`;
- связи явно помечены как `name_based_heuristic`;
- stdout содержит только результат, traceback появляется только с `--debug`.

В v1 **не добавлять** `--diff`, `--grep`, `--at`, общий query DSL, cache, daemon,
MCP, snapshot или overlays. Для discovery остаются `outline`/`grep`, для изменений
— `diff`. Расширять `context` стоит после проверки, что объединение трёх команд
реально экономит вызовы и токены.

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
121         outline = render_outline(parsed.entities, path, with_lines=args.lines)
---
src/codenav/cli.py
29  from codenav.outline import render_outline
---
src/codenav/outline.py
20  def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
```

Как агент использует результат:

- строка 20 — определение;
- строка 121 — реальный caller, который надо учитывать при изменении Interface;
- строка 29 — только import, поэтому она менее важна, чем call site.

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
    cmd_outline  (src/codenav/cli.py:115-124, function)
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

### После v1: один вызов

```bash
uv run codenav context render_outline --root src/codenav --format text
```

Предлагаемый text stdout (исходник сокращён только в документации):

```text
TARGET function render_outline
  id: src/codenav/outline.py:20:render_outline
  location: src/codenav/outline.py:20-62

SOURCE complete=true
20  def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
...
62      return "\n".join(lines)

RELATIONS analysis=name_based_heuristic
  incoming:
    cmd_outline  src/codenav/cli.py:115-124
      evidence: src/codenav/cli.py:121  render_outline(...)
  outgoing:
    module_name  src/codenav/outline.py:12-17
      evidence: src/codenav/outline.py:48  module_name(file_path)
    Entity  src/codenav/core.py:210-233
      evidence: src/codenav/outline.py:20  list[Entity]

PATHS nodes=5
  main -> cmd_outline -> render_outline -> module_name -> path

COVERAGE files_seen=4 files_parsed=4 files_skipped=0
OUTPUT bytes=4210 limit=16384 truncated=false

NEXT
  codenav context cmd_outline --root src/codenav --format json
  codenav context module_name --root src/codenav --format json
```

Как агент использует результат:

1. читает target source;
2. проверяет `analysis=name_based_heuristic` и не переоценивает graph;
3. смотрит evidence прямого caller на строке 121;
4. перед правкой при необходимости выполняет один готовый `NEXT`.

## Сценарий 3. Машиночитаемый результат для tool loop

```bash
uv run codenav context render_outline --root src/codenav
```

Предлагаемый default JSON:

```json
{
  "schema": "codenav.context/v1",
  "status": "ok",
  "target": {
    "id": "src/codenav/outline.py:20:render_outline",
    "qualified_name": "render_outline",
    "kind": "function",
    "location": {
      "path": "src/codenav/outline.py",
      "start_line": 20,
      "end_line": 62
    }
  },
  "source": {
    "complete": true,
    "text": "def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:\n..."
  },
  "relations": {
    "analysis": "name_based_heuristic",
    "incoming": [
      {
        "entity_id": "src/codenav/cli.py:115:cmd_outline",
        "resolution": "unique_by_name",
        "evidence": [
          {
            "path": "src/codenav/cli.py",
            "line": 121,
            "text": "outline = render_outline(parsed.entities, path, with_lines=args.lines)"
          }
        ]
      }
    ],
    "outgoing": [
      {
        "entity_id": "src/codenav/outline.py:12:module_name",
        "resolution": "unique_by_name",
        "evidence": [
          {
            "path": "src/codenav/outline.py",
            "line": 48,
            "text": "lines: list[str] = [f\"{module_name(file_path)}:\"]"
          }
        ]
      }
    ]
  },
  "paths": [
    [
      "src/codenav/cli.py:209:main",
      "src/codenav/cli.py:115:cmd_outline",
      "src/codenav/outline.py:20:render_outline",
      "src/codenav/outline.py:12:module_name",
      "src/codenav/core.py:240:ParsedFile.path"
    ]
  ],
  "coverage": {
    "files_seen": 4,
    "files_parsed": 4,
    "files_skipped": []
  },
  "truncation": {
    "truncated": false,
    "limit_bytes": 16384,
    "omitted": {}
  },
  "next_actions": [
    {
      "reason": "inspect_direct_dependent",
      "argv": [
        "codenav", "context", "cmd_outline",
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
- `coverage` показывает, насколько результат полон;
- `argv` можно передать следующему tool call без shell parsing.

## Сценарий 4. Неоднозначное имя

Задача агента: в двух файлах есть `handler`; пользователь просит изменить один из
них. Текущий `symbol handler` молча выбирает первое определение.

Предлагаемый вызов:

```bash
uv run codenav context handler --root src
```

Предлагаемый JSON:

```json
{
  "schema": "codenav.context/v1",
  "status": "ambiguous",
  "query": "handler",
  "candidates": [
    {
      "id": "src/api.py:18:handler",
      "kind": "function",
      "location": {"path": "src/api.py", "start_line": 18, "end_line": 31}
    },
    {
      "id": "src/worker.py:42:handler",
      "kind": "function",
      "location": {"path": "src/worker.py", "start_line": 42, "end_line": 67}
    }
  ],
  "next_actions": [
    {
      "reason": "select_candidate",
      "argv": ["codenav", "context", "--id", "src/api.py:18:handler"]
    },
    {
      "reason": "select_candidate",
      "argv": ["codenav", "context", "--id", "src/worker.py:42:handler"]
    }
  ]
}
```

Как агент использует результат: сопоставляет путь с задачей пользователя или
читает короткие candidate previews; ни один кандидат не выбирается скрыто.
Корректно обработанная ambiguity возвращает exit 0 — это полезный результат, а
не поломка инструмента.

## Сценарий 5. Понять контекст изменённой строки

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

Следующий агентский шаг в v1:

```bash
uv run codenav context module_name --root src/codenav
```

То есть `diff` отвечает «какой символ изменён», а `context` — «что это за символ
и куда ведут его связи». Не нужно добавлять `--diff` в первый релиз `context`.

## Сценарий 6. Управлять шириной graph

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

## Что измерить после реализации

Решение о следующем расширении принимать по реальному agent loop:

- сколько вызовов `symbol + impact + graph` заменил один `context`;
- сколько байт/токенов занял JSON по сравнению с тремя text-ответами;
- как часто `next_actions` действительно исполняются;
- как часто встречается `ambiguous`;
- сколько relations агент отбрасывает после проверки evidence;
- время одного объединённого scan.

Если основной выигрыш подтвердится, следующий кандидат — `context --diff -`.
Если главным узким местом окажется повторное индексирование длинной сессии —
stdio/MCP Adapter. До измерений не нужны оба сразу.
