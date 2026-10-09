---
name: codenav-research
description: >-
  Answer questions about an existing codebase with the `codenav` CLI: find
  definitions, callers, dependency chains and change impact, reading only the
  symbol slices you need. Use for research on code you did not write — where is
  X defined, who calls X, what does X reach, what breaks if X changes, how does
  a request flow end to end — not for editing.
---

# codenav research

`codenav` is a tree-sitter indexer: it returns symbol maps and source slices
instead of whole files. Every command is read-only. Every claim in your answer
must come from output you actually ran — never from a plausible guess.

## Answer loop

1. **Locate** — `outline` to map a module, `grep` when the name is unknown.
2. **Verify** — `symbol` on the one symbol in question; never read a whole file to answer one question.
3. **Relate** — `impact` for one hop, `trace` for chains (`--direction` for one side), always with `--kind` and depth caps.
4. **Report** — symbol + `path:lines` + the relation proving each step.

Stop as soon as the question is answered. Do not walk the whole command list.

## Replace, don't augment

`codenav` replaces the harness `read`/`grep` tools here; it is not another step on
top of them:

- the harness `grep` tool prints whole matching files — `codenav grep P` prints the matching lines, headed by the matched symbol's `path:start-end::name` span;
- the harness `read` tool prints whole files — `codenav symbol NAME` prints the one symbol;
- `codenav astgrep P` is the same search with the full source of each matched symbol: use it when the matched lines alone are not enough, not for discovery;
- use `read` only for what `symbol` cannot reach: a docstring, a config/value table, a test body `grep` already located.

When both were available, agents that ran codenav *and* the harness grep/read spent
more than double the characters of agents that ran codenav instead of them.

## Turn discipline

Every tool call is an LLM round-trip and the whole prompt is re-sent with it, so a
turn costs far more than the bytes it fetches.

- Put every independent command of one step into one assistant message — several
  parallel tool calls, or one `bash` call chaining commands with `&&`.
- Never re-read a file you already sliced; never repeat a command you already ran.
- Extra "just to be sure" verification turns are the most expensive thing in a session.

## Commands

```
codenav outline [PATH...] [--top-level] [--lines] [--deps] [--filter REGEX...] [--pages SPEC] [--max-chars N]
codenav symbol  NAME...              [--root DIR...]
codenav impact  NAME...              [--detailed] [--kind KIND...] [--root DIR...]
codenav trace   NAME...              [--direction both|up|down] [--depth N] [--max-paths K] [--kind KIND...] [--root DIR...]
codenav info    NAME...              [--depth N] [--max-paths K] [--kind KIND...] [--root DIR...]
codenav grep    PATTERN...           [-i] [--lang LANG] [--root DIR...]
codenav astgrep PATTERN...           [-i] [--lang LANG] [--root DIR...]
codenav doctor                       [--root DIR...] [--verbose]
codenav diff    [PATH]               [--lines SPEC]
```

`--root DIR...` indexes **only** the listed directories — pass every root you
need in one call (`--root project tests`). Name-taking commands accept several
names and build one index per invocation; if any name is missing the command
prints nothing and exits with the missing names listed, so use a qualified name
(`Class.method`) when a bare name is ambiguous.

## Common mistakes

Each of these used to return an empty or partial answer without an error:

- **The pattern is a Python regex.** "Or" is `a|b`; grep's `a\|b` is a literal
  pipe here (codenav retries it as `a|b` once and says so on the first line).
  Ignore case with `-i` or a leading `(?i)`. There is no `-n`, `-l` or `--limit`:
  line numbers are always printed; size the answer with `--max-chars` / `--pages`.
- **Names first, `--root` last.** `--root` takes every following word; codenav
  takes trailing non-paths back as names, but say it right the first time.
  Repeating `--root` adds roots.
- **`symbol` takes names, not paths.** A file's symbols: `outline <file>`.
- **A name defined twice shows the first match** with a `note:` naming the
  others. In a monorepo query one service per call.
- **`| head` cuts the page hint** (`page 1 of N`); prefer `--max-chars`.
- **`uvx codenav` is an unrelated PyPI package**; the CLI comes from `agentcodemap`.
- **`impact` follows references by name**: dependencies resolved through a DI
  container may not show up as dependents — confirm "no dependents" with `grep`.

## Which command answers which question

| Question | Command |
|---|---|
| What is in this repo / module? | `outline <dir> --top-level` (narrow with `--filter`) |
| Where is X defined? | `grep 'X' --root project`, then `symbol X` |
| What does X do? | `symbol X` — a method, not the whole class |
| Who calls / uses X? | `impact X --root project tests --detailed --kind call` |
| What does X depend on? | `trace X --root project --direction down --depth 3 --kind call` |
| What breaks if X changes? | `impact X --detailed` (dependents), then `trace X --depth 3` for the second hop |
| How does a request flow end to end? | `trace <entrypoint> --direction down --kind call`, then `symbol` each hop |
| Which symbols did the diff touch? | `codenav diff`, then `impact` those symbols |
| Which tests cover X? | `impact X --root project tests --detailed`, or `grep 'X' --root tests` |
| Search came up empty — is it absent or unindexed? | `doctor --root project` — names missing roots, skipped files with reasons, `<unknown>` and syntax-error counts |

## Token discipline

Measured cost on a ~220-module Python repo (chars per call — this is context you
pay for on every later turn):

| Call | chars | Note |
|---|---:|---|
| `grep P` | ~200 | cheapest discovery: matched lines + symbol spans |
| `doctor` | ~400 | indexing diagnosis: roots, file/language counts, skip reasons |
| `outline <dir> --filter X` | ~500 | narrow the map before printing it |
| `outline <dir> --top-level` | ~10 000 | one page; more pages exist |
| `trace X` | ~1 400 | default depth 3, both sides |
| `impact X --detailed --kind call` | ~2 400 | |
| `impact X --detailed` | ~4 300 | |
| `symbol Class.method` | ~4 700 | |
| `astgrep P` | ~`symbol` × matches | full source of every matched symbol — not for discovery |
| `trace X --direction down` | ~5 000 | one side only |
| `symbol Class` | ~24 000 | whole class body — avoid |
| `info X` | ~34 000 | symbol + chains + impact — avoid unless all three are needed |

Rules that follow from the table:

- Keep each result under ~8 000 chars. Bigger means you under-specified: add
  `--kind call`, `--filter`, lower `--depth`, or `--max-paths`.
- `grep` is the discovery form: matched lines, each block headed by the symbol's
  `path:start-end::name` span. `astgrep` is the same search with the **full
  source** of every matched symbol — reach for it only when the matched lines are
  not enough to decide.
- `symbol` on a class dumps every method. Ask for `Class.method`.
- `--kind call` keeps only call sites from `impact`/`trace`;
  `--kind ret` follows only producer edges of a data object.
- `outline --top-level` prints page 1 and says how many pages remain; fetch the
  rest in one call with `--pages 2-4` instead of re-running per page.
- Read a file with `read` only for a range `symbol` cannot give you (a docstring,
  a config constant, a test body already located by `grep`).
- An empty result is not evidence of absence. Before reporting "not found", run
  `doctor --root <the same roots>`: it names roots that do not exist, files the
  index skipped and why, directories pruned from the walk, and files whose
  extraction is suspect (`<unknown>` names, tree-sitter syntax errors, zero
  symbols). A clean parse there is still not proof that extraction is complete.

## Relation kinds

Edges carry the kind of reference site that produced them; `--kind` filters both
the output and the traversal.

| Label | Meaning |
|---|---|
| `call` | name is invoked (`helper()`, `obj.method()`) |
| `inh` | name in a base-class list |
| `par` | name in a type annotation outside a return position: parameter, field, local — the consumer side |
| `ref` | any other mention (read, value, registration) |
| `ret` | name in a producer position: `-> T` annotation or directly returned (`return x`, `return build()`) |
| `str` | word from a DI string (`"pkg.mod:Symbol"`) — textual candidate, not confirmed by syntax |

`trace` edges are structural: `A -[call]-> B` means A's body references
B, not that B runs first at runtime. Say so in the report when order matters.

## Report format

For each fact: `symbol` — `path:line` — relation (`kind`) that links it to the
previous step. Separate verified body evidence (`symbol` output) from structural
edges (`impact`/`trace`). Name exact identifiers, never paraphrase them;
the consumer of the report must be able to jump straight to the code.
