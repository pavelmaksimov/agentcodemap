# A/B benchmark агентских workflow

Проверено 2026-08-31 на рабочем дереве поверх commit `06f3cf3`. Сравниваются
две экспериментальные версии, использующие один и тот же Module фактов:

- **A, eager:** `codenav context NAME` — source, relations и paths одним вызовом;
- **B, progressive:** `select NAME`, затем `read ID` и/или `expand ID`.

## Методика

Пять символов разного размера и связности:
`_parse_lines_spec`, `render_outline`, `slice_diff`, `RepoIndex`, `cmd_diff`.

Для latency каждый сабагент делал 2 прогрева и 10 измерений на символ. Каждый
CLI step запускался отдельным реальным процессом через `uv run codenav`.
Progressive full измерялся и последовательно, и с параллельными `read`/`expand`
после `select`. Размер — фактический compact JSON в байтах. Время зависит от
машины; отношение между workflow важнее абсолютных миллисекунд.

## Прогоны сабагентов

| Сабагент | Независимый прогон | Результат |
|---|---:|---|
| `eager_metrics` | 10 warmup + 50 timed CLI processes | 60/60 JSON valid; pooled median 164,5 ms |
| `progressive_metrics` | 50 sequential full workflows; отдельные серии `select -> read` и parallel full | все ответы valid; 152,8 / 376,4 / 412,8 ms по трём глубинам |
| `workflow_parity` | 5 символов, untruncated A/B + контрактные edge cases | 60/60 semantic checks и 21/21 edge checks |
| `workflow_usability` | 2 реальные задачи × 2 workflow | 4/4 success; A: 2 calls, B: 5 calls |

Сабагенты получили разные роли: измеритель одной версии не оценивал её
удобство, а parity-аудитор сравнивал факты, не latency. Это уменьшает риск
подогнать вывод под заранее выбранный интерфейс.

## Версия A: eager context

Текущие размеры ответов:

- `_parse_lines_spec`: 1 499 B; 10 строк source; 1 incoming; 1 path;
- `render_outline`: 8 238 B; 43 строки; 1 incoming, 13 outgoing; 1 path;
- `slice_diff`: 12 144 B; 75 строк; 1 incoming, 17 outgoing; 3 paths;
- `RepoIndex`: 16 171 B, `status=partial`; сохранено 14 incoming, 2 outgoing,
  0 paths; omitted 32 outgoing и 4 paths;
- `cmd_diff`: 7 132 B; 35 строк; 1 incoming, 11 outgoing; 3 paths.

Медианный ответ — 8 238 B, примерно 2 060 model tokens при грубой оценке
`bytes / 4`. Сумма пяти ответов — 45 184 B.

Pooled median latency по 50 измерениям — 164,5 ms. Median медиан пяти символов —
165,3 ms. Все измеряемые ответы были детерминированы по размеру и валидны.

## Версия B: progressive navigation

Текущие размеры `select / read / expand`:

- `_parse_lines_spec`: 696 / 964 / 1 058 B; full 2 718 B;
- `render_outline`: 744 / 1 970 / 6 664 B; full 9 378 B;
- `slice_diff`: 725 / 3 530 / 8 989 B; full 13 244 B;
- `RepoIndex`: 633 / 10 389 / 15 961 B; full 26 983 B;
- `cmd_diff`: 654 / 1 945 / 5 543 B; full 8 142 B.

Медианы по сценариям:

- locate-only: 696 B, около 174 model tokens;
- locate + source: 2 714 B, около 679 model tokens;
- full `select + read + expand`: 9 378 B, около 2 345 model tokens.

Latency:

- `select`: pooled median 152,8 ms, p95 213,8 ms;
- `select -> read`: pooled median 376,4 ms, p95 425,9 ms;
- последовательный full: pooled median 472,0 ms, p95 624,1 ms;
- `select -> (read || expand)`: pooled median 412,8 ms, p95 462,5 ms.

Параллельный `expand` почти скрывается за `read`, но повторная индексация всё
равно оставляет full workflow медленнее eager.

## Прямое сравнение

### Агенту нужны source, relations и paths

- A: 1 process; median 8 238 B; около 164,5 ms.
- B: 3 processes; median 9 378 B; около 412,8 ms при параллельных
  `read`/`expand`.
- B больше примерно на 13,8% по median payload и медленнее примерно в 2,5 раза.

При снятом byte cap факты совпали полностью. На всех пяти символах eager занял
60 716 B, полный progressive — 66 459 B, то есть на 9,5% больше из-за повторных
envelopes.

### Агенту нужно только найти определение

- A всё равно строит полный context: median 8 238 B.
- B останавливается после `select`: median 696 B и 152,8 ms.
- Progressive экономит около 91,6% median payload и немного времени.

### Агенту нужно найти и прочитать source

- A: median 8 238 B и один process.
- B: median 2 714 B, но два process и 376,4 ms.
- Progressive экономит около 67,1% payload, но медленнее примерно в 2,3 раза.

### Большой символ и byte cap

Для `RepoIndex` обе версии корректно возвращают `status=partial`, но усечение
разное:

- A делит один лимит между source и graph: 14 incoming, 2 outgoing, 0 paths;
- B даёт отдельный лимит `expand`: 14 incoming, 24 outgoing, 0 paths.

Progressive сохраняет больше graph-фактов, зато суммарно отправляет 26 983 B
против 16 171 B eager.

## Практический прогон двух задач

Независимый сабагент решил обе задачи обеими версиями. В этом usability-прогоне
использовался `--root .`, поэтому абсолютные размеры отличаются от основной
таблицы с `--root src/codenav`; сравнение A/B внутри каждой задачи корректно.

Глубокая задача `render_outline` — definition/source, caller evidence, unique
function dependency и path:

- A: 1 call, 10 235 B, success;
- B: 3 calls, 11 423 B, success;
- A меньше на 1 188 B и не требует промежуточного решения.

Локальная задача `_parse_lines_spec` — только definition и source:

- A: 1 call, 2 050 B, success;
- B: 2 calls, 1 731 B, success;
- B экономит 319 B, но требует второй process и решение после `select`.

Суммарно по двум задачам A использовал 2 calls и 12 285 B; B — 5 calls и
13 154 B. Оба дали правильные ответы.

## Корректность

- semantic parity без усечения: 60/60 сравнений;
- совпали target ID/location, полный source, relations, resolution, evidence и
  paths для всех пяти символов;
- после исправления найденных контрактных дефектов: 21/21 edge checks;
- JSON cap учитывает весь stdout и проверен ровно на 1 024 B;
- missing/double selector дают usage exit 2;
- ambiguity, not-found, exact ID roundtrip и text format проверены;
- полный проект: `39 passed`.

## Как выбрать одну версию

Выбирать **A**, если типичный первый вопрос агента звучит «покажи символ и всё,
что нужно знать перед изменением». Это меньше calls, меньше latency и меньше
суммарный payload при полном анализе.

Выбирать **B**, если типичный первый вопрос — «найди определение», а relations
нужны редко. Это резко уменьшает locate/read payload и позволяет остановиться
раньше, но повторно индексирует root.

Не стоит сохранять обе версии как постоянный Interface: общий Module уже доказал
parity, поэтому после выбора проигравшие CLI adapters можно удалить без потери
аналитической логики.
