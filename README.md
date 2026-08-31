# codenav

Tree-sitter harness для навигации и поиска по коду, рассчитанный на LLM-агентов.
Возвращает компактные, машиночитаемые срезы кода вместо целых файлов.

Проверенное фактическое поведение и ограничения: [аудит CLI](docs/current-cli-audit.md).
Проект единого агентского интерфейса: [agent interface design](docs/agent-interface-design.md).
Два экспериментальных agent workflow, реальные stdout и сценарии сравнения:
[agent command scenarios](docs/agent-command-scenarios.md).
Числовые результаты независимых A/B-прогонов:
[agent workflow benchmark](docs/agent-workflow-benchmark.md).

## Команды

### `codenav outline PATH... [--lines]`

PATH — файл или каталог (каталог обходится рекурсивно, outline каждого модуля).
Компактная карта символов файла:

```
$ codenav outline project/mymodule.py
project.mymodule:
A MY_MODULE_ATTR

F my_func

C MyClass
    A my_attr
    M my_method
```

Модули без символов не выводятся.

Буквы: `C` класс, `M` метод, `F` функция, `A` атрибут/константа/тип.
`--lines` добавляет `L<start>-<end>` к каждой записи.

### `codenav diff PATH --diff FILE|- | --lines SPEC [--lang LANG]`

Нарезка кода по диффу: изменённые строки разворачиваются до содержащих их символов,
соседние символы склеиваются (зазор ≤ 5 строк). Принимает unified diff из файла или stdin,
либо явный список строк (`--lines '10,15-20'`). Полностью удалённые и новые модули
не нарезаются — выводится короткая пометка `MODULE DELETED` / `NEW MODULE`.

### `codenav symbol NAME [--root DIR]`

Исходник символа по имени (простому или квалифицированному, например `MyClass.my_method`).
Поиск идёт по всему `--root` (по умолчанию текущий каталог).

При нескольких совпадениях текущая реализация печатает первое в порядке обхода;
для точного выбора используйте квалифицированное имя, когда оно однозначно.

Поиск ссылок именной; строки-литералы тоже сканируются (DI-регистрации вида
`"pkg.mod:Symbol"`, forward-аннотации), docstring исключены.

### `codenav impact NAME [--root DIR]`

Цепочка влияния символа:

* **depends-on** — пользовательские символы, на которые ссылается цель
  (включая всё её поддерево: методы и атрибуты класса);
* **dependents** — символы, чьи тела ссылаются на цель.

```
$ codenav impact CodeReviewService --root project
impact chain for CodeReviewService (project/.../service.py:48-521):
  depends-on:
    Constants  (project/settings.py:18-47, class)
    ...
  dependents:
    Services  (project/container.py:41-157, class)
```

### `codenav graph NAME [--root DIR] [--nodes N] [--max-paths K]`

Цепочки влияния через символ в виде текстового графа; ребро `A -> B` означает
«A ссылается на B». `--nodes` — лимит **различных символов всего графа**
(по умолчанию 5): кандидаты-цепочки перечисляются по полной матрице ссылок
и включаются жадно, самые длинные первыми, пока не исчерпан бюджет узлов;
цепочки по уже выбранным узлам добавляются бесплатно; цепочка, целиком содержащаяся
в более длинной, убирается как дубликат. Одноимённые функции
в разных файлах — разные узлы графа. `--max-paths` — страховочный потолок
количества цепочек.

```
$ codenav graph my_func --root src
my_func: side -> my_func -> mid -> base, top -> my_func -> mid -> base
```

### `codenav grep PATTERN [PATH...] [--full] [--lang LANG]`

Regex-поиск по строкам исходника. Совпадения группируются по наименьшей
объемлющей сущности; без `PATH` сканируется текущий каталог.

Формат вывода: путь файла, затем `номер_строки<TAB>код`; блоки разделяются `---`.
Хиты группируются по наименьшей объемлющей сущности (метод, а не весь класс);
строки вне символов идут как есть. С `--full` вместо совпавших строк печатается
полный исходник символа по его границам.

### Эксперимент A: `codenav context`

```text
codenav context NAME|--id ENTITY_ID [--root DIR] [--nodes N]
                [--max-output-bytes N] [--format json|text]
```

Eager workflow: одним вызовом возвращает source, direct relations, evidence и
bounded paths. JSON является default.

### Эксперимент B: `select -> read/expand`

```text
codenav select NAME [--root DIR] [--format json|text]
codenav read ENTITY_ID [--root DIR] [--format json|text]
codenav expand ENTITY_ID [--root DIR] [--nodes N] [--format json|text]
```

Progressive workflow: `select` возвращает точный ID и следующие команды; агент
сам решает, читать source, раскрывать relations или запрашивать обе части. После
сравнительного теста один из двух экспериментов будет удалён.

## Языки

Python, JavaScript/TypeScript/TSX, Go, Rust, Java, Scala, Ruby, PHP, C#, C/C++.
Язык определяется по расширению; переопределяется флагом `--lang`.

Грамматики всех перечисленных языков загружаются, но полнота извлечения символов
пока различается. Например, smoke-тест выявил `<unknown>` для части объявлений
Go/C/C++ и пропущенный метод PHP; подробности есть в аудите CLI.

## Разработка

```bash
uv sync --group dev
uv run pytest
```
