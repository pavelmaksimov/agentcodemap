"""codenav CLI — tree-sitter navigation/search harness for LLM agents.

Commands:
    outline  PATH...                 depth-sorted symbol outline, capped
    diff     PATH --diff F|--lines S slice code around a diff
    symbol   NAME...                 print symbol source for each name
    impact   NAME...                 depends-on/dependents per symbol
    grep     PATTERN... [--root D]  symbol slices matching any pattern
    graph    NAME...                influence paths through each symbol
    info     NAME...                accumulated symbol + graph + impact
    context  NAME|--id ID            source + relations + paths for an agent

Root-indexed commands (symbol, impact, graph, context) take one or more
--root DIR arguments: only the listed directories are indexed, siblings at
the same level are ignored. Symbol commands also accept several NAME
arguments per invocation (one shared index scan).
"""

from __future__ import annotations

import argparse
import os
import sys

from codenav.agent import (
    build_context,
    encode_json,
    limit_report,
    render_text,
)
from codenav.diff import (
    added_lines_from_unified_diff,
    parse_unified_diff,
    slice_diff,
)
from codenav.index import ImpactReport, RepoIndex
from codenav.model import Entity, Slice, detect_language
from codenav.parse import parse_file
from codenav.outline import assemble_outline, render_outline


def _roots_of(args: argparse.Namespace) -> list[str]:
    """Root directories from --root (default: current directory)."""
    return args.root or ["."]


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


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _lang_or_die(path: str, lang: str | None) -> str:
    resolved = lang or detect_language(path)
    if not resolved:
        sys.exit(f"codenav: cannot detect language for {path}; pass --lang")
    return resolved


def _print_slice(sl: Slice) -> None:
    header = f"{sl.kind} {sl.name}" if sl.name != "<unknown>" else sl.kind
    print(f"### L{sl.start_line}-{sl.end_line}  [{header}]")
    for i, line in enumerate(sl.content.split("\n"), start=sl.start_line):
        print(f"{i}\t{line}")
    print()


def _collect_code_files(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [
                    d for d in dirnames if d not in RepoIndex.SKIP_DIRS and not d.startswith(".")
                ]
                files.extend(os.path.join(dirpath, fn) for fn in filenames if detect_language(fn))
        else:
            files.append(p)
    return files


def cmd_diff(args: argparse.Namespace) -> None:
    if args.diff:
        raw = sys.stdin.read() if args.diff == "-" else _read_file(args.diff)
        per_file = parse_unified_diff(raw)
        entry = next(
            (
                f
                for f in per_file.values()
                if f.path == args.path
                or args.path.endswith("/" + f.path)
                or f.path.endswith("/" + args.path)
            ),
            None,
        )
        if entry is None:
            sys.exit(f"codenav diff: {args.path} not found in the diff")
        if entry.status == "deleted":
            print(f"{args.path}: MODULE DELETED (not sliced)")
            return
        if entry.status == "added":
            print(f"{args.path}: NEW MODULE ({len(entry.added_lines)} added lines, not sliced)")
            return
        changed = entry.added_lines
    elif args.lines:
        changed = _parse_lines_spec(args.lines)
    else:
        sys.exit("codenav diff: pass --diff FILE (or - for stdin) or --lines SPEC")
    content = _read_file(args.path)
    language = _lang_or_die(args.path, args.lang)
    slices = slice_diff(args.path, content, changed, language)
    if not slices:
        print("(no slices)")
        return
    for sl in slices:
        _print_slice(sl)


def cmd_outline(args: argparse.Namespace) -> None:
    filters = [f for group in args.filter for f in group]
    modules: list[tuple[str, str]] = []
    for path in _collect_code_files(args.paths):
        content = _read_file(path)
        parsed = parse_file(path, content, _lang_or_die(path, args.lang), collect_refs=False)
        if parsed is None:
            sys.exit(f"codenav: unsupported language for {path}")
        outline = render_outline(parsed.entities, path, with_lines=args.lines)
        if outline:  # modules without symbols are skipped
            modules.append((path, outline))
    body, shown, omitted = assemble_outline(
        modules,
        roots=args.paths,
        filters=filters,
        max_chars=args.max_chars,
    )
    if not shown:
        # Exit 0 on purpose: the command ran, but nothing matched.
        if filters:
            print(f"(no modules match: {'|'.join(filters)})")
        else:
            print("(no modules found)")
        return
    print(body)
    if omitted:
        print(f"not shown: {omitted} modules (max_chars={args.max_chars})")
    else:
        print()


def _not_found_message(missing: list[str], roots: list[str]) -> str:
    where = ", ".join(roots)
    if len(missing) == 1:
        return f"codenav: symbol {missing[0]!r} not found under {where}"
    quoted = ", ".join(repr(name) for name in missing)
    return f"codenav: symbols not found under {where}: {quoted}"


def cmd_symbol(args: argparse.Namespace) -> None:
    index = RepoIndex(_roots_of(args))
    resolved = {name: index.find_symbol(name) for name in args.names}
    missing = [name for name in args.names if not resolved[name]]
    if missing:
        sys.exit(_not_found_message(missing, index.roots))
    for name in args.names:
        _print_symbol_source(resolved[name][0])


def cmd_impact(args: argparse.Namespace) -> None:
    index = RepoIndex(_roots_of(args))
    resolved = {name: index.find_symbol(name) for name in args.names}
    missing = [name for name in args.names if not resolved[name]]
    if missing:
        sys.exit(_not_found_message(missing, index.roots))
    for position, name in enumerate(args.names):
        if position:
            print()
        report = index.impact_entity(resolved[name][0])
        _print_impact(report, index, detailed=args.detailed)


def _print_symbol_source(e: Entity) -> None:
    print(f"### {e.qualified_name}  ({e.file}:{e.start_line}-{e.end_line}, {e.kind})")
    lines = _read_file(e.file).split("\n")
    for ln in range(e.start_line, min(e.end_line, len(lines)) + 1):
        print(f"{ln}\t{lines[ln - 1]}")
    print()


def _print_impact(report: ImpactReport, index: RepoIndex, detailed: bool = False) -> None:
    def path(entity: Entity) -> str:
        return index.display_path(entity.file)

    def print_entities(entities: list[Entity]) -> None:
        if detailed:
            for entity in sorted(
                entities,
                key=lambda e: (path(e), e.start_line, e.end_line, e.qualified_name),
            ):
                print(
                    f"{path(entity)}:{entity.start_line}-{entity.end_line}::"
                    f"{entity.qualified_name} {entity.kind}"
                )
            return

        names = sorted({entity.qualified_name for entity in entities})
        for name in names:
            print(name)

    t = report.target
    if detailed:
        print(f"impact chain for {t.qualified_name} ({path(t)}:{t.start_line}-{t.end_line}):")
    else:
        print(f"impact chain for {t.qualified_name}:")
    print("- depends-on:")
    if report.depends_on:
        print_entities(report.depends_on)
    else:
        print("(none found)")
    print("- dependents:")
    if report.dependents:
        print_entities(report.dependents)
    else:
        print("(none found)")


def cmd_grep(args: argparse.Namespace) -> None:
    files = _collect_code_files(_roots_of(args))
    if not files:
        print("(no code files found)")
        return
    first_block = True

    def emit_block(path: str, lines: list[tuple[int, str]]) -> None:
        nonlocal first_block
        if not first_block:
            print("---")
        first_block = False
        print(path)
        for ln, text in lines:
            print(f"{ln}\t{text}")

    for path in files:
        language = _lang_or_die(path, args.lang)
        parsed = parse_file(path, _read_file(path), language, collect_refs=False)
        if parsed is None:
            continue
        # Union of patterns, grouped by smallest enclosing symbol: a symbol
        # matched by several patterns is printed once (full mode), and in
        # --match-only mode its matched lines from all patterns are merged.
        blocks: dict[object, tuple[Entity | None, dict[int, str]]] = {}
        order: list[object] = []
        for pattern in args.patterns:
            for entity, matched in parsed.grep_symbols(pattern):
                key: object = entity if entity is not None else None
                if key not in blocks:
                    blocks[key] = (entity, {})
                    order.append(key)
                lines = blocks[key][1]
                for ln, text in matched:
                    lines.setdefault(ln, text)
        for key in order:
            entity, matched_lines = blocks[key]
            if args.match_only or entity is None:
                lines = sorted(matched_lines.items())
            else:
                # full symbol source sliced by its boundaries
                end = min(entity.end_line, len(parsed.content_lines))
                lines = [(ln, parsed.content_lines[ln - 1]) for ln in range(entity.start_line, end + 1)]
            emit_block(path, lines)
    if first_block:
        # Exit 0 on purpose: the command ran, but nothing matched.
        quoted = ", ".join(repr(pattern) for pattern in args.patterns)
        print(f"(no matches for: {quoted})")


def _print_graph(label: str, entity: Entity, index: RepoIndex, nodes: int, max_paths: int) -> None:
    """Influence paths through an exact definition (cmd_graph/info body).

    ``label`` is the name as requested (cmd_graph echoes it verbatim).
    """
    paths, total_paths = index.influence_paths_entity_with_total(
        entity,
        max_nodes=nodes,
        max_paths=max_paths,
    )
    if not paths:
        print(f"{label}: no influence data (0 paths)")
        return
    chains = [" -> ".join(e.name for e in path) for path in paths]
    print(f"{label}:")
    print("\n".join(chains))
    omitted = total_paths - len(paths)
    if omitted > 0:
        print(f"not shown: {omitted} paths (max_paths={max_paths})")


def cmd_graph(args: argparse.Namespace) -> None:
    index = RepoIndex(_roots_of(args))
    resolved = {name: index.find_symbol(name) for name in args.names}
    missing = [name for name in args.names if not resolved[name]]
    if missing:
        sys.exit(_not_found_message(missing, index.roots))
    for position, name in enumerate(args.names):
        if position:
            print()
        _print_graph(name, resolved[name][0], index, nodes=args.nodes, max_paths=args.max_paths)


def cmd_info(args: argparse.Namespace) -> None:
    """Accumulate symbol source + influence paths + impact chain in one scan.

    Graph part defaults to nodes=20/max_paths=50. Empty parts keep their
    per-command markers (``(none found)``, ``no influence data (0 paths)``,
    ``not shown: N paths``) so a missing piece of information is visible
    instead of looking like a truncated run. A name that resolves nowhere
    aborts with the same not-found error as symbol/impact/graph.
    """
    index = RepoIndex(_roots_of(args))
    resolved = {name: index.find_symbol(name) for name in args.names}
    missing = [name for name in args.names if not resolved[name]]
    if missing:
        sys.exit(_not_found_message(missing, index.roots))
    for position, name in enumerate(args.names):
        if position:
            print()
        target = resolved[name][0]
        _print_symbol_source(target)
        _print_graph(name, target, index, nodes=args.nodes, max_paths=args.max_paths)
        print()
        _print_impact(index.impact_entity(target), index)


def _agent_output(report: dict, args: argparse.Namespace) -> None:
    limit_report(report, args.max_output_bytes)
    if args.format == "json":
        sys.stdout.write(encode_json(report))
    else:
        print(render_text(report))


def cmd_context(args: argparse.Namespace) -> None:
    _agent_output(
        build_context(_roots_of(args), name=args.name, exact_id=args.entity_id, nodes=args.nodes),
        args,
    )


def _at_least_1024(value: str) -> int:
    parsed = int(value)
    if parsed < 1024:
        raise argparse.ArgumentTypeError("must be at least 1024")
    return parsed


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _add_root(p: argparse.ArgumentParser, what: str = "index") -> None:
    p.add_argument(
        "--root",
        nargs="+",
        default=None,
        metavar="DIR",
        help=f"file(s)/dir(s) to {what}; several allowed; default: current directory",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codenav", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "outline",
        help="compact symbol outline of file(s), modules root-first and capped",
    )
    p.add_argument("paths", nargs="+")
    p.add_argument("--lang", help="override language detection")
    p.add_argument("--lines", action="store_true", help="append L<start>-<end> to each entry")
    p.add_argument(
        "--filter",
        action="append",
        nargs="+",
        default=[],
        metavar="REGEX",
        help="keep only modules whose path matches REGEX (OR'd; several values and repeats allowed)",
    )
    p.add_argument(
        "--max-chars",
        type=_positive,
        default=10_000,
        help="cap total outline length in chars; shallow modules print first (default: 10000)",
    )
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser("diff", help="slice code around diff-changed lines")
    p.add_argument("path")
    p.add_argument("--diff", help="unified diff file, or '-' for stdin")
    p.add_argument("--lines", help="explicit changed lines spec, e.g. '10,15-20'")
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("symbol", help="print symbol source for each NAME")
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser("impact", help="depends-on/dependents influence chain per NAME")
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.add_argument("--detailed", action="store_true", help="include paths, lines, and entity kinds")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("grep", help="symbol slices whose body matches any PATTERN")
    p.add_argument(
        "patterns",
        nargs="+",
        metavar="PATTERN",
        help="regex patterns; a symbol matching any of them is reported once",
    )
    _add_root(p, what="search")
    p.add_argument(
        "--match-only",
        action="store_true",
        help="print only matched lines (default: full source of each matched symbol)",
    )
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("graph", help="influence chains through each NAME")
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.add_argument("--nodes", type=_positive, default=3, help="max nodes per path")
    p.add_argument("--max-paths", type=_positive, default=100, help="max paths to show")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser(
        "info",
        help="accumulated symbol source + influence paths + impact chain per NAME",
    )
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.add_argument(
        "--nodes", type=_positive, default=20, help="max nodes per path in the graph part (default: 20)"
    )
    p.add_argument(
        "--max-paths",
        type=_positive,
        default=50,
        help="max graph paths to show (default: 50)",
    )
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("context", help="agent context for a symbol")
    p.add_argument("name", nargs="?")
    p.add_argument("--id", dest="entity_id", help="exact entity ID from a prior result")
    _add_root(p)
    p.add_argument("--nodes", type=_positive, default=5, help="max nodes per path")
    p.add_argument(
        "--max-output-bytes",
        type=_at_least_1024,
        default=16384,
        help="JSON-oriented output budget (default: 16384)",
    )
    p.add_argument("--format", choices=("json", "text"), default="json")
    p.set_defaults(func=cmd_context)

    args = parser.parse_args(argv)
    if args.command == "context" and (args.name is None) == (args.entity_id is None):
        parser.error("context requires exactly one NAME or --id ENTITY_ID")
    args.func(args)


if __name__ == "__main__":
    main()
