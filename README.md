# codenav

Tree-sitter harness для навигации и поиска по коду, рассчитанный на LLM-агентов.
Возвращает компактные, машиночитаемые срезы кода вместо целых файлов.

## Команды

### `codenav outline PATH... [--lines]`

Компактная карта символов файла:

```
$ codenav outline project/mymodule.py
project.mymodule
A MY_MODULE_ATTR

F my_func

C MyClass
    A my_attr
    M my_method
```

Буквы: `C` класс, `M` метод, `F` функция, `A` атрибут/константа/тип.
`--lines` добавляет `L<start>-<end>` к каждой записи.

### `codenav diff PATH --diff FILE|- | --lines SPEC [--lang LANG]`

Нарезка кода по диффу: изменённые строки разворачиваются до содержащих их символов,
соседние символы склеиваются (зазор ≤ 5 строк). Принимает unified diff из файла или stdin,
либо явный список строк (`--lines '10,15-20'`).

### `codenav symbol NAME [PATH...] [--impact] [--root DIR]`

Исходник символа по имени (простому или квалифицированному, например `MyClass.my_method`).
С `--impact` — цепочка влияния:

* **depends-on** — пользовательские символы, на которые ссылается цель;
* **dependents** — символы, чьи тела ссылаются на цель.

Поиск ссылок именной (последний сегмент qualified name) — эвристика, достаточная
для агентных подсказок. Без `PATH` ищет по всему `--root`.

### `codenav graph NAME [--root DIR] [--nodes N] [--max-paths K]`

Цепочки влияния через символ в виде текстового графа; ребро `A -> B` означает
«A ссылается на B». Объединяет upstream-зависимых, сам символ и downstream-зависимости;
каждый путь — не длиннее `--nodes` узлов (по умолчанию 5), выдача ограничена `--max-paths`.

```
$ codenav graph my_func --root src
my_func: side -> my_func -> mid -> base, top -> my_func -> mid -> base, my_func -> mid, side -> my_func, top -> my_func
```
Исходник символа по имени (простому или квалифицированному, например `MyClass.my_method`).
С `--impact` — цепочка влияния:

* **depends-on** — пользовательские символы, на которые ссылается цель;
* **dependents** — символы, чьи тела ссылаются на цель.

Поиск ссылок именной (последний сегмент qualified name) — эвристика, достаточная
для агентных подсказок. Без `PATH` ищет по всему `--root`.

### `codenav grep PATTERN [PATH...] [--lang LANG]`

grep-ast-стиль: regex по телам символов, хиты группируются по наименьшей объемлющей
сущности; строки вне символов помечаются `<module level>`.

## Языки

Python, JavaScript/TypeScript/TSX, Go, Rust, Java, Scala, Ruby, PHP, C#, C/C++.
Язык определяется по расширению; переопределяется флагом `--lang`.

## Разработка

```bash
uv sync --group dev
uv run pytest
```
