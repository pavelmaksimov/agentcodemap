"""codenav CLI — tree-sitter navigation/search harness for LLM agents.

Commands:
    outline  PATH...                 compact symbol outline
    diff     PATH --diff F|--lines S slice code around a diff
    symbol   NAME [PATH...]          print symbol source; --impact adds influence chain
    grep     PATTERN [PATH...]       slices of symbols whose body matches pattern
"""

from __future__ import annotations

import argparse
import os
import sys

from codenav.core import (
    Entity,
    ImpactReport,
    RepoIndex,
    Slice,
    added_lines_from_unified_diff,
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


def cmd_outline(args: argparse.Namespace) -> None:
    for path in args.paths:
        content = _read_file(path)
        parsed = parse_file(path, content, _lang_or_die(path, args.lang))
        if parsed is None:
            sys.exit(f"codenav: unsupported language for {path}")
        print(render_outline(parsed.entities, path, with_lines=args.lines))
        print()


def cmd_diff(args: argparse.Namespace) -> None:
    content = _read_file(args.path)
    language = _lang_or_die(args.path, args.lang)
    if args.diff:
        raw = sys.stdin.read() if args.diff == "-" else _read_file(args.diff)
        per_file = added_lines_from_unified_diff(raw)
        key = next(
            (
                f
                for f in per_file
                if f == args.path or args.path.endswith("/" + f) or f.endswith("/" + args.path)
            ),
            args.path,
        )
        changed = per_file.get(key, set())
    elif args.lines:
        changed = _parse_lines_spec(args.lines)
    else:
        sys.exit("codenav diff: pass --diff FILE (or - for stdin) or --lines SPEC")
    slices = slice_diff(args.path, content, changed, language)
    if not slices:
        print("(no slices)")
        return
    for sl in slices:
        _print_slice(sl)


def cmd_symbol(args: argparse.Namespace) -> None:
    if args.impact or not args.paths:
        index = RepoIndex(args.root)
        found = index.find_symbol(args.name)
        if not found:
            sys.exit(f"codenav: symbol {args.name!r} not found under {args.root}")
        target = found[0]
        _print_symbol_source(target)
        if args.impact:
            report = index.impact(args.name)
            if report:
                _print_impact(report)
        return

    for path in args.paths:
        parsed = parse_file(path, _read_file(path), _lang_or_die(path, args.lang))
        if parsed is None:
            sys.exit(f"codenav: unsupported language for {path}")
        matches = parsed.find_symbol(args.name)
        if not matches:
            print(f"(no symbol {args.name!r} in {path})")
            continue
        _print_symbol_source(matches[0])


def _print_symbol_source(e: Entity) -> None:
    print(f"### {e.qualified_name}  ({e.file}:{e.start_line}-{e.end_line}, {e.kind})")
    lines = _read_file(e.file).split("\n")
    for ln in range(e.start_line, min(e.end_line, len(lines)) + 1):
        print(f"{ln}\t{lines[ln - 1]}")
    print()


def _print_impact(report: ImpactReport) -> None:
    t = report.target
    print(f"impact chain for {t.qualified_name} ({t.location}):")
    print("  depends-on:")
    if report.depends_on:
        for e in sorted(report.depends_on, key=lambda x: x.location):
            print(f"    {e.qualified_name}  ({e.location}, {e.kind})")
    else:
        print("    (none found)")
    print("  dependents:")
    if report.dependents:
        for e in sorted(report.dependents, key=lambda x: x.location):
            print(f"    {e.qualified_name}  ({e.location}, {e.kind})")
    else:
        print("    (none found)")


def cmd_grep(args: argparse.Namespace) -> None:
    paths = args.paths or ["."]
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in RepoIndex.SKIP_DIRS and not d.startswith(".")
                ]
                files.extend(os.path.join(dirpath, fn) for fn in filenames if detect_language(fn))
        else:
            files.append(p)

    total_hits = 0
    for path in sorted(files):
        language = _lang_or_die(path, args.lang)
        parsed = parse_file(path, _read_file(path), language)
        if parsed is None:
            continue
        hits = parsed.grep_symbols(args.pattern)
        if not hits:
            continue
        total_hits += len(hits)
        print(f"== {path}")
        for entity, matched in hits:
            label = f"{entity.kind} {entity.qualified_name}" if entity else "<module level>"
            span = f"L{entity.start_line}-{entity.end_line} " if entity else ""
            print(f"  [{label}] {span}")
            for line in matched:
                print(f"    | {line.strip()}")
    print(f"\n{total_hits} symbol(s) matched")


def cmd_graph(args: argparse.Namespace) -> None:
    index = RepoIndex(args.root)
    if not index.find_symbol(args.name):
        sys.exit(f"codenav: symbol {args.name!r} not found under {args.root}")
    paths = index.influence_paths(args.name, max_nodes=args.nodes, max_paths=args.max_paths)
    if not paths:
        print(f"{args.name}: (no influence edges)")
        return
    rendered = ", ".join(" -> ".join(e.name for e in path) for path in paths)
    print(f"{args.name}: {rendered}")


def main() -> None:
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

    p = sub.add_parser("symbol", help="print symbol source by name; --impact shows influence chain")
    p.add_argument("name")
    p.add_argument("paths", nargs="*", help="files to search; omit to search whole --root")
    p.add_argument("--impact", action="store_true", help="also show depends-on/dependents")
    p.add_argument("--root", default=".", help="root directory for whole-repo search/impact")
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser("grep", help="slices of symbols whose body matches regex")
    p.add_argument("pattern")
    p.add_argument("paths", nargs="*", help="files/dirs; default '.'")
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("graph", help="influence chains through a symbol, up to --nodes per path")
    p.add_argument("name")
    p.add_argument("--root", default=".", help="repository root to index")
    p.add_argument("--nodes", type=int, default=5, help="max nodes per path")
    p.add_argument("--max-paths", type=int, default=100, help="cap on number of paths")
    p.set_defaults(func=cmd_graph)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
