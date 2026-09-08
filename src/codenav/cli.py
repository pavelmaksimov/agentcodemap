"""codenav CLI — tree-sitter navigation/search harness for LLM agents.

Commands:
    outline  PATH...                 compact symbol outline
    diff     PATH --diff F|--lines S slice code around a diff
    symbol   NAME                    print symbol source
    impact   NAME                    depends-on/dependents for a symbol
    grep     PATTERN [PATH...]       slices of symbols whose body matches pattern
    graph    NAME                    influence paths through a symbol
    context  NAME|--id ID            source + relations + paths for an agent
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
from codenav.core import (
    Entity,
    ImpactReport,
    RepoIndex,
    Slice,
    added_lines_from_unified_diff,
    parse_unified_diff,
    detect_language,
    parse_file,
    slice_diff,
)
from codenav.outline import render_outline


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
    for path in _collect_code_files(args.paths):
        content = _read_file(path)
        parsed = parse_file(path, content, _lang_or_die(path, args.lang))
        if parsed is None:
            sys.exit(f"codenav: unsupported language for {path}")
        outline = render_outline(parsed.entities, path, with_lines=args.lines)
        if outline:  # modules without symbols are skipped
            print(outline)
            print()


def cmd_symbol(args: argparse.Namespace) -> None:
    index = RepoIndex(args.root)
    found = index.find_symbol(args.name)
    if not found:
        sys.exit(f"codenav: symbol {args.name!r} not found under {args.root}")
    _print_symbol_source(found[0])


def cmd_impact(args: argparse.Namespace) -> None:
    index = RepoIndex(args.root)
    if not index.find_symbol(args.name):
        sys.exit(f"codenav: symbol {args.name!r} not found under {args.root}")
    report = index.impact(args.name)
    if report:
        _print_impact(report, args.root, detailed=args.detailed)


def _print_symbol_source(e: Entity) -> None:
    print(f"### {e.qualified_name}  ({e.file}:{e.start_line}-{e.end_line}, {e.kind})")
    lines = _read_file(e.file).split("\n")
    for ln in range(e.start_line, min(e.end_line, len(lines)) + 1):
        print(f"{ln}\t{lines[ln - 1]}")
    print()


def _print_impact(report: ImpactReport, root: str = ".", detailed: bool = False) -> None:
    root_name = os.path.basename(os.path.normpath(os.path.abspath(root)))

    def path(entity: Entity) -> str:
        relative = os.path.relpath(entity.file, root)
        return os.path.join(root_name, relative)

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
    files = _collect_code_files(args.paths or ["."])
    first_block = True
    for path in files:
        language = _lang_or_die(path, args.lang)
        parsed = parse_file(path, _read_file(path), language)
        if parsed is None:
            continue
        for entity, matched in parsed.grep_symbols(args.pattern):
            if not first_block:
                print("---")
            first_block = False
            print(path)
            if args.full and entity is not None:
                # full symbol source sliced by its boundaries
                for ln in range(entity.start_line, min(entity.end_line, len(parsed.content_lines)) + 1):
                    print(f"{ln}\t{parsed.content_lines[ln - 1]}")
            else:
                for ln, line in matched:
                    print(f"{ln}\t{line}")


def cmd_graph(args: argparse.Namespace) -> None:
    index = RepoIndex(args.root)
    if not index.find_symbol(args.name):
        sys.exit(f"codenav: symbol {args.name!r} not found under {args.root}")
    paths = index.influence_paths(args.name, max_nodes=args.nodes, max_paths=args.max_paths)
    if not paths:
        print(f"{args.name}: (no influence edges)")
        return
    chains: list[str] = []
    seen: set[str] = set()
    for path in paths:
        chain = " -> ".join(e.name for e in path)
        if chain not in seen:
            seen.add(chain)
            chains.append(chain)
    print(f"{args.name}: {', '.join(chains)}")


def _agent_output(report: dict, args: argparse.Namespace) -> None:
    limit_report(report, args.max_output_bytes)
    if args.format == "json":
        sys.stdout.write(encode_json(report))
    else:
        print(render_text(report))


def cmd_context(args: argparse.Namespace) -> None:
    _agent_output(
        build_context(args.root, name=args.name, exact_id=args.entity_id, nodes=args.nodes),
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codenav", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("outline", help="compact symbol outline of file(s)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--lang", help="override language detection")
    p.add_argument("--lines", action="store_true", help="append L<start>-<end> to each entry")
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser("diff", help="slice code around diff-changed lines")
    p.add_argument("path")
    p.add_argument("--diff", help="unified diff file, or '-' for stdin")
    p.add_argument("--lines", help="explicit changed lines spec, e.g. '10,15-20'")
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("symbol", help="print symbol source by name (searched under --root)")
    p.add_argument("name")
    p.add_argument("--root", default=".", help="repository root to index")
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser("impact", help="depends-on/dependents influence chain of a symbol")
    p.add_argument("name")
    p.add_argument("--root", default=".", help="repository root to index")
    p.add_argument("--detailed", action="store_true", help="include paths, lines, and entity kinds")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("grep", help="slices of symbols whose body matches regex")
    p.add_argument("pattern")
    p.add_argument("paths", nargs="*", help="files/dirs; default '.'")
    p.add_argument("--full", action="store_true", help="also print the full source of each matched symbol")
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("graph", help="influence chains through a symbol within a node budget")
    p.add_argument("name")
    p.add_argument("--root", default=".", help="repository root to index")
    p.add_argument("--nodes", type=int, default=5, help="max DISTINCT nodes in the graph (longest chains first)")
    p.add_argument("--max-paths", type=int, default=100, help="cap on number of paths")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("context", help="agent context for a symbol")
    p.add_argument("name", nargs="?")
    p.add_argument("--id", dest="entity_id", help="exact entity ID from a prior result")
    p.add_argument("--root", default=".", help="repository root to index")
    p.add_argument("--nodes", type=_positive, default=5, help="max DISTINCT path nodes")
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
