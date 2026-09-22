"""codenav CLI — tree-sitter navigation/search harness for LLM agents.

Root-indexed commands (symbol, impact, trace, path, info, grep, astgrep)
take one or more --root DIR arguments: only the listed directories are
indexed, siblings at the same level are ignored. Commands that accept
several NAME arguments build one shared index per invocation. `doctor`
takes the same --root list and reports what the indexing of it kept and
skipped, so a "not found" can be told from a file the index never read.

`codenav --help` lists every command with its full option set; run
`codenav CMD --help` for the detail of one command.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence

from codenav.diff import (
    DiffFile,
    parse_unified_diff,
    slice_diff,
)
from codenav.doctor import diagnose, render_json, render_text
from codenav.index import ImpactReport, RepoIndex
from codenav.model import (
    KIND_CHOICES,
    KIND_LABELS,
    REF_KINDS,
    Entity,
    ParsedFile,
    Relation,
    Slice,
    detect_language,
    kind_label,
    kind_labels,
    resolve_kinds,
)
from codenav.parse import parse_file
from codenav.outline import assemble_outline, render_outline


def _roots_of(args: argparse.Namespace) -> list[str]:
    """Root directories from --root (default: current directory)."""
    return args.root or ["."]


def _parse_int_spec(spec: str) -> set[int]:
    """'10,15-20' -> {10, 15..20} (diff --lines / outline --pages grammar)."""
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
    """Slice around changed lines; the change source is a flag or the legacy auto mode.

    The source flags are mutually exclusive (argparse rejects combinations).
    With an explicit source the result does not depend on stdin being a
    terminal: --working-tree/--staged/--base run git, --stdin always reads
    stdin. Without a flag the legacy behavior stays: terminal -> git
    working-tree diff, pipe -> stdin.
    """
    if args.lines:
        if not args.path:
            sys.exit("codenav diff: --lines requires a PATH")
        _diff_changed_path(
            args.path, _parse_int_spec(args.lines), args.lang, _disk_reader(args.repo)
        )
        return
    git_source = args.working_tree or args.staged or args.base
    if args.stdin or (not git_source and not sys.stdin.isatty()):
        _diff_from_stdin(args)
        return
    top = _git_toplevel(args.repo)
    if top is None:
        if git_source:
            label = (
                f"--base {args.base}"
                if args.base
                else "--staged" if args.staged else "--working-tree"
            )
            sys.exit(
                f"codenav diff: {label} needs a git checkout (git unavailable or not a "
                "git repository; pass --repo DIR or pipe a diff with --stdin)"
            )
        sys.exit(
            "codenav diff: pipe a unified diff on stdin (git diff | codenav diff [PATH]) "
            "or run inside a git checkout"
        )
    paths = [] if args.path is None else [args.path]
    if args.base:
        # REF...HEAD: the new side is HEAD, so source files come from that revision.
        diff_text, error = _git_diff(args.repo, [["diff", f"{args.base}...HEAD"]], paths)
        if diff_text is None:
            sys.exit(f"codenav diff: git diff {args.base}...HEAD failed: {error}")
        read = _git_reader(args.repo, "HEAD:", "HEAD")
    elif args.staged:
        # Index vs HEAD; a checkout without HEAD compares the index with the tree.
        diff_text, error = _git_diff(
            args.repo, [["diff", "--cached", "HEAD"], ["diff", "--cached"]], paths
        )
        read = _git_reader(args.repo, ":", "index")
    else:
        diff_text, error = _git_diff(args.repo, [["diff", "HEAD"], ["diff"]], paths)
        read = _disk_reader(top)
    if diff_text is None:
        sys.exit(f"codenav diff: git diff failed: {error}")
    if not diff_text:
        print("(no changes)")
        return
    _diff_all_files(parse_unified_diff(diff_text), read)


def _diff_from_stdin(args: argparse.Namespace) -> None:
    """Explicit --stdin and the legacy piped mode: slice the unified diff as given."""
    per_file = parse_unified_diff(sys.stdin.read())
    read = _disk_reader(args.repo)
    if not args.path:
        # Whole-diff mode: slice every changed code file.
        if not per_file:
            sys.exit("codenav diff: no unified diff on stdin")
        _diff_all_files(per_file, read)
        return
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
        if not per_file:
            sys.exit(f"codenav diff: no unified diff on stdin for {args.path}")
        sys.exit(f"codenav diff: {args.path} not found in the diff")
    if entry.status == "deleted":
        print(f"{args.path}: MODULE DELETED (not sliced)")
        return
    if entry.status == "added":
        print(f"{args.path}: NEW MODULE ({len(entry.added_lines)} added lines, not sliced)")
        return
    _diff_changed_path(args.path, entry.added_lines, args.lang, read)


def _diff_changed_path(
    path: str, changed_lines: set[int], lang: str | None, read: Callable[[str], str]
) -> None:
    """Slice one source file (read via ``read``) by explicit changed-line numbers."""
    content = read(path)
    language = _lang_or_die(path, lang)
    slices = slice_diff(path, content, changed_lines, language)
    if not slices:
        print("(no slices)")
        return
    for sl in slices:
        _print_slice(sl)


def _disk_reader(base: str | None) -> Callable[[str], str]:
    """Content reader for on-disk sources: relative paths resolve against base."""

    def read(path: str) -> str:
        if base is None or os.path.isabs(path):
            return _read_file(path)
        return _read_file(os.path.join(base, path))

    return read


def _git_reader(repo: str | None, spec: str, label: str) -> Callable[[str], str]:
    """Content reader for the diff's new side from git: ':' (index) or 'HEAD:' (revision)."""

    def read(path: str) -> str:
        try:
            proc = subprocess.run(
                ["git", "show", f"{spec}{path}"], cwd=repo, capture_output=True, text=True
            )
        except OSError as exc:
            sys.exit(f"codenav diff: cannot read {path} from {label}: {exc}")
        if proc.returncode != 0:
            sys.exit(
                f"codenav diff: cannot read {path} from {label}: {proc.stderr.strip()}"
            )
        return proc.stdout

    return read


def _git_toplevel(repo: str | None) -> str | None:
    """Repository root (git diff paths are root-relative); None outside a checkout."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _git_diff(
    repo: str | None, variants: list[list[str]], paths: list[str]
) -> tuple[str | None, str]:
    """First succeeding `git <variant> [-- PATHS]` run in repo, else (None, last stderr).

    Several variants model fallbacks (e.g. a checkout without HEAD); a failure
    of every variant means git is missing, the ref is bad, or not a checkout.
    """
    tail = ["--", *paths] if paths else []
    error = ""
    for variant in variants:
        try:
            proc = subprocess.run(
                ["git", *variant, *tail], cwd=repo, capture_output=True, text=True
            )
        except OSError as exc:
            return None, str(exc)
        if proc.returncode == 0:
            return proc.stdout, ""
        error = proc.stderr.strip()
    return None, error


def _diff_all_files(per_file: dict[str, DiffFile], read: Callable[[str], str]) -> None:
    """No PATH given: slice every changed code file of a unified diff.

    Blocks are separated by '---' (grep style). Deleted/added modules reuse the
    single-file markers; changed files with no sliceable lines print
    '<path>: (no slices)'. Non-code files in the diff are skipped silently.
    """
    emitted = False
    for path, entry in per_file.items():
        if entry.status == "deleted":
            header: str = f"{path}: MODULE DELETED (not sliced)"
            body: list[Slice] = []
        elif entry.status == "added":
            header = f"{path}: NEW MODULE ({len(entry.added_lines)} added lines, not sliced)"
            body = []
        else:
            language = detect_language(path)
            if language is None:
                continue
            slices = slice_diff(path, read(path), entry.added_lines, language)
            if slices:
                header, body = path, slices
            else:
                header, body = f"{path}: (no slices)", []
        if emitted:
            print("---")
        emitted = True
        print(header)
        for sl in body:
            _print_slice(sl)


def _outline_roots(paths: Sequence[str]) -> list[str]:
    """Index roots for outline --deps: directories as given, files via their dir."""
    return [p if os.path.isdir(p) else os.path.dirname(p) or "." for p in paths]


def _outline_deps(index: RepoIndex, parsed: ParsedFile) -> dict[Entity, list[str]]:
    """Per-symbol dependency labels ('name [kinds]'), sorted; empty when none."""
    deps: dict[Entity, list[str]] = {}
    for entity in parsed.entities:
        labels = sorted(
            f"{relation.entity.qualified_name} [{kind_labels(relation.kinds)}]"
            for relation in index.direct_dependencies(entity)
        )
        if labels:
            deps[entity] = labels
    return deps


def cmd_outline(args: argparse.Namespace) -> None:
    filters = [f for group in args.filter for f in group]
    index = RepoIndex(_outline_roots(args.paths)) if args.deps else None
    indexed = (
        {os.path.normpath(path): parsed for path, parsed in index.files.items()}
        if index is not None
        else {}
    )
    modules: list[tuple[str, str]] = []
    for path in _collect_code_files(args.paths):
        parsed = indexed.get(os.path.normpath(path)) if index is not None else None
        if parsed is None:
            parsed = parse_file(path, _read_file(path), _lang_or_die(path, args.lang), collect_refs=False)
            if parsed is None:
                sys.exit(f"codenav: unsupported language for {path}")
        deps = _outline_deps(index, parsed) if index is not None else None
        outline = render_outline(
            parsed.entities, path, with_lines=args.lines, top_level=args.top_level, deps=deps
        )
        if outline:  # modules without symbols are skipped
            modules.append((path, outline))
    pages, oversized = assemble_outline(
        modules,
        roots=args.paths,
        filters=filters,
        max_chars=args.max_chars,
    )
    if not pages:
        # Exit 0 on purpose: the command ran, but nothing matched.
        if filters:
            print(f"(no modules match: {'|'.join(filters)})")
        else:
            print("(no modules found)")
        return
    total = len(pages)
    _print_pages(pages, _selected_pages(args.pages, total, "outline"))
    if oversized:
        print(
            "not fully shown: "
            + ", ".join(oversized)
            + f" (module(s) larger than page size {args.max_chars}; "
            "raise --max-chars to print them whole)"
        )
    elif total == 1:
        print()


def _print_pages(pages: list[str], wanted: list[int]) -> None:
    """Print the selected page texts, each with its navigation note.

    The note names the pages left over so one follow-up call fetches them
    ('page 1 of 4; 3 more: --pages 2-4'); a single-page run needs no note.
    """
    total = len(pages)
    for k in wanted:
        print(pages[k - 1])
        if total > 1:
            remaining = total - k
            if remaining:
                tail = f"{k + 1}-{total}" if remaining > 1 else str(total)
                print(f"(page {k} of {total}; {remaining} more: --pages {tail})")
            else:
                print(f"(page {k} of {total})")


def _selected_pages(specs: list[str] | None, total: int, cmd: str) -> list[int]:
    """Selected page numbers for cmd's paginated output; page 1 by default.

    Each spec uses the '2', '2-4', '1,3' grammar (repeatable, unioned). Page
    numbers are validated against the actual page count so an agent asking for
    a page that does not exist gets a range hint instead of empty output.
    """
    if not specs:
        return [1]
    wanted: set[int] = set()
    for spec in specs:
        try:
            parsed = _parse_int_spec(spec)
        except ValueError:
            sys.exit(
                f"codenav {cmd}: invalid --pages spec {spec!r} "
                "(use e.g. '2', '2-4', '1,3')"
            )
        if not parsed:
            sys.exit(f"codenav {cmd}: --pages spec {spec!r} selects no pages")
        wanted.update(parsed)
    out_of_range = sorted(p for p in wanted if not 1 <= p <= total)
    if out_of_range:
        shown = ", ".join(map(str, out_of_range))
        sys.exit(
            f"codenav {cmd}: page(s) {shown} out of range: {cmd} has {total} page(s)"
        )
    return sorted(wanted)


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
        report = index.impact_entity(resolved[name][0], kinds=args.kind)
        _print_impact(report, index, detailed=args.detailed)


def _print_symbol_source(e: Entity) -> None:
    print(f"### {e.qualified_name}  ({e.file}:{e.start_line}-{e.end_line}, {e.kind})")
    lines = _read_file(e.file).split("\n")
    for ln in range(e.start_line, min(e.end_line, len(lines)) + 1):
        print(f"{ln}\t{lines[ln - 1]}")
    print()


def _print_impact(report: ImpactReport, index: RepoIndex, detailed: bool = False) -> None:
    """Render depends-on/dependents with the kind of every relation.

    Kinds (see ``model.REF_KINDS``) always accompany a relation, printed as
    their short labels (``model.KIND_LABELS``); ``--detailed`` adds the
    reference sites as ``label@line``. Lines belong to the referencing
    side: the target for depends-on (its file is in the header), the listed
    entity itself for dependents.
    """

    def path(entity: Entity) -> str:
        return index.display_path(entity.file)

    def sites(relation: Relation) -> str:
        return ",".join(f"{kind_label(o.kind)}@{o.line}" for o in relation.observations)

    def print_relations(relations: list[Relation]) -> None:
        if detailed:
            for relation in sorted(
                relations,
                key=lambda r: (
                    path(r.entity),
                    r.entity.start_line,
                    r.entity.end_line,
                    r.entity.qualified_name,
                ),
            ):
                entity = relation.entity
                location = (
                    f"{path(entity)}:{entity.start_line}-{entity.end_line}::"
                    f"{entity.qualified_name} {entity.kind}"
                )
                evidence = sites(relation)
                print(f"{location}  [{evidence}]" if evidence else location)
            return

        kinds: dict[str, set[str]] = {}
        for relation in relations:
            kinds.setdefault(relation.entity.qualified_name, set()).update(relation.kinds)
        for name in sorted(kinds):
            labels = kind_labels(kinds[name])
            print(f"{name} [{labels}]" if labels else name)

    t = report.target
    if detailed:
        print(f"impact chain for {t.qualified_name} ({path(t)}:{t.start_line}-{t.end_line}):")
    else:
        print(f"impact chain for {t.qualified_name}:")
    print("- depends-on:")
    if report.depends_on:
        print_relations(report.depends_on)
    else:
        print("(none found)")
    print("- dependents:")
    if report.dependents:
        print_relations(report.dependents)
    else:
        print("(none found)")


def _grep_blocks(files: list[str], patterns: Sequence[str], lang: str | None):
    """Regex hits grouped by smallest enclosing symbol, in file order.

    Yields ``(path, entity, matched_lines, content_lines)``; ``entity`` is None
    for hits outside any symbol. Patterns are a union: a symbol matched by
    several of them is yielded once, with the matched lines merged.
    """
    for file in files:
        parsed = parse_file(file, _read_file(file), _lang_or_die(file, lang), collect_refs=False)
        if parsed is None:
            continue
        blocks: dict[object, tuple[Entity | None, dict[int, str]]] = {}
        order: list[object] = []
        for pattern in patterns:
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
            yield file, entity, matched_lines, parsed.content_lines


def _grep_block(
    path: str,
    entity: Entity | None,
    matched: dict[int, str],
    content: Sequence[str],
    full: bool,
) -> str:
    """Render one match block as text: header line, then numbered lines.

    Short form heads the block with the symbol's span; full form prints the
    symbol's whole source, so the path is all the header there is to say.
    """
    if entity is not None and full:
        header = path
        end = min(entity.end_line, len(content))
        lines = [(ln, content[ln - 1]) for ln in range(entity.start_line, end + 1)]
    elif entity is not None:
        header = (
            f"{path}:{entity.start_line}-{entity.end_line}::"
            f"{entity.qualified_name} {entity.kind}"
        )
        lines = sorted(matched.items())
    else:
        header = path
        lines = sorted(matched.items())
    return "\n".join([header, *(f"{ln}\t{text}" for ln, text in lines)])


def _grep_pages(blocks: list[str], limit: int) -> list[str]:
    """Group whole match blocks into pages of at most ``limit`` chars.

    Blocks keep their '---' separators and never split across pages. A block
    larger than the limit owns a page of its own and stays whole: matched
    lines are the result of the search, so cutting one would hide hits.
    """
    pages: list[str] = []
    page: list[str] = []
    used = 0
    for text in blocks:
        extra = len(text) + (5 if page else 0)  # "\n---\n" between blocks
        if page and used + extra > limit:
            pages.append("\n---\n".join(page))
            page, used = [], 0
            extra = len(text)
        page.append(text)
        used += extra
    if page:
        pages.append("\n---\n".join(page))
    return pages


def _print_grep(args: argparse.Namespace, full: bool) -> None:
    """Both forms of the search, one block per matched symbol.

    Short form (``full`` false): matched lines only, so the header carries the
    symbol's span (``path:start-end::qualified_name kind``, as in
    ``impact --detailed``). Full form: the symbol's whole source, whose own line
    numbers delimit it. Hits outside every symbol have no span to name and print
    as matched lines in both forms.

    Blocks paginate like outline pages: ``--max-chars`` sets the page size and
    ``--pages`` picks which pages print (page 1 otherwise).
    """
    files = _collect_code_files(_roots_of(args))
    if not files:
        print("(no code files found)")
        return
    blocks = [
        _grep_block(path, entity, matched, content, full)
        for path, entity, matched, content in _grep_blocks(
            files, args.patterns, args.lang
        )
    ]
    if not blocks:
        # Exit 0 on purpose: the command ran, but nothing matched.
        quoted = ", ".join(repr(pattern) for pattern in args.patterns)
        print(f"(no matches for: {quoted})")
        return
    pages = _grep_pages(blocks, args.max_chars)
    _print_pages(pages, _selected_pages(args.pages, len(pages), "grep"))
    if len(pages) == 1:
        print()


def cmd_grep(args: argparse.Namespace) -> None:
    """Short form: matched lines, headed by the enclosing symbol's span."""
    _print_grep(args, full=False)


def cmd_astgrep(args: argparse.Namespace) -> None:
    """Extended form: the full source of every symbol whose body matches."""
    _print_grep(args, full=True)


def _render_chain(
    path: list[Entity], index: RepoIndex, kinds: Sequence[str] | None = None
) -> str:
    """'a -[call]-> b': every edge is labelled with the source's kinds (short).

    With ``kinds`` set, an edge shows only the selected kinds (the walk already
    dropped edges that have none of them).
    """
    parts = [path[0].name]
    for source, target in zip(path, path[1:]):
        found = sorted(
            {o.kind for o in index.relation_observations(source, target, kinds)}
        )
        parts.append(f"-[{kind_labels(found)}]->" if found else "->")
        parts.append(target.name)
    return " ".join(parts)


def _print_chains(
    label: str,
    paths: list[list[Entity]],
    total_paths: int,
    max_paths: int,
    index: RepoIndex,
    kinds: Sequence[str] | None = None,
    no_data: str = "no influence data (0 paths)",
) -> None:
    """Render labelled chains under ``label`` (trace/path/info body)."""
    if not paths:
        print(f"{label}: {no_data}")
        return
    print(f"{label}:")
    for path in paths:
        print(_render_chain(path, index, kinds))
    omitted = total_paths - len(paths)
    if omitted > 0:
        print(f"not shown: {omitted} paths (max_paths={max_paths})")


def _print_graph(
    label: str,
    entity: Entity,
    index: RepoIndex,
    depth: int,
    max_paths: int,
    kinds: Sequence[str] | None = None,
) -> None:
    """Influence paths through an exact definition (cmd_trace default / cmd_info body).

    ``label`` is the name as requested (the caller echoes it verbatim).
    ``depth`` is the per-chain symbol budget handed to the index as max_nodes.
    Every edge is printed as ``-[kind]->`` (see ``_render_chain``); ``kinds``
    filters which edges the walk may use and which labels they show.
    """
    paths, total_paths = index.influence_paths_entity_with_total(
        entity,
        max_nodes=depth,
        max_paths=max_paths,
        kinds=kinds,
    )
    _print_chains(label, paths, total_paths, max_paths, index, kinds)


def cmd_trace(args: argparse.Namespace) -> None:
    """Influence chains through each NAME; --direction picks the sides walked.

    Default ``both`` walks both sides through the target with the shared
    --depth budget. ``down`` walks only depends_on and ``up`` only dependents,
    so the whole budget goes into one direction; ``up`` chains print in the
    arrow order (referrer -> NAME), like the merged walk does.
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
        if args.direction == "both":
            _print_graph(
                name,
                target,
                index,
                depth=args.depth,
                max_paths=args.max_paths,
                kinds=args.kind,
            )
            continue
        paths, total = index.direction_paths_entity_with_total(
            target,
            args.direction,
            max_nodes=args.depth,
            max_paths=args.max_paths,
            kinds=args.kind,
        )
        if args.direction == "up":
            # side paths start at the target; print referrer-first for arrow order
            paths = [path[::-1] for path in paths]
        _print_chains(
            name,
            paths,
            total,
            args.max_paths,
            index,
            args.kind,
            no_data=(
                "no dependency chains (0 paths)"
                if args.direction == "down"
                else "no influence data (0 paths)"
            ),
        )


def cmd_path(args: argparse.Namespace) -> None:
    """Shortest chains from SOURCE to TARGET through depends_on relations.

    Both names resolve like trace (first match wins); a name that
    resolves nowhere aborts with the usual not-found error before any
    output. An empty answer — no route within --depth — stays a successful
    result with the explicit ``no chains`` marker.
    """
    index = RepoIndex(_roots_of(args))
    resolved = {name: index.find_symbol(name) for name in (args.source, args.target)}
    missing = [name for name in (args.source, args.target) if not resolved[name]]
    if missing:
        sys.exit(_not_found_message(missing, index.roots))
    paths, total = index.paths_between_entities_with_total(
        resolved[args.source][0],
        resolved[args.target][0],
        max_nodes=args.depth,
        max_paths=args.max_paths,
        kinds=args.kind,
    )
    _print_chains(
        f"{args.source} -> {args.target}",
        paths,
        total,
        args.max_paths,
        index,
        args.kind,
        no_data="no chains (0 paths)",
    )


def cmd_info(args: argparse.Namespace) -> None:
    """Accumulate symbol source + influence paths + impact chain in one scan.

    Chain part defaults to depth=20/max_paths=50. Empty parts keep their
    per-command markers (``(none found)``, ``no influence data (0 paths)``,
    ``not shown: N paths``) so a missing piece of information is visible
    instead of looking like a truncated run. A name that resolves nowhere
    aborts with the same not-found error as symbol/impact/trace.
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
        _print_graph(
            name,
            target,
            index,
            depth=args.depth,
            max_paths=args.max_paths,
            kinds=args.kind,
        )
        print()
        _print_impact(index.impact_entity(target, kinds=args.kind), index)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Report what indexing did with the roots: kept files, skipped ones with
    their reasons, and extraction warnings.

    Root problems (missing path, a file instead of a directory) are part of
    the report and make the command exit non-zero, so an empty index over a
    mistyped root cannot pass for an empty project.
    """
    report = diagnose(_roots_of(args))
    render = render_json if args.format == "json" else render_text
    print(render(report, args.verbose))
    if report.blocking_roots:
        sys.exit(1)


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _add_root(p: argparse.ArgumentParser, what: str = "index", dirs_only: bool = False) -> None:
    p.add_argument(
        "--root",
        nargs="+",
        default=None,
        metavar="DIR",
        help=f"{'dir(s)' if dirs_only else 'file(s)/dir(s)'} to {what}; "
        "several allowed; default: current directory",
    )


def _add_kind(p: argparse.ArgumentParser) -> None:
    """--kind for commands that print relations (repeatable, several per flag)."""
    p.add_argument(
        "--kind",
        nargs="+",
        action="extend",
        default=None,
        choices=KIND_CHOICES,
        metavar="KIND",
        help=(
            "keep only relations whose reference sites include one of these kinds; "
            "several allowed, repeatable "
            f"({', '.join(REF_KINDS)}; short forms: {', '.join(KIND_LABELS.values())})"
        ),
    )


def _usage_options(sp: argparse.ArgumentParser) -> str:
    """Collapsed usage of a subparser, without the prog prefix and -h.

    Powers the generated per-command option reference in `codenav --help`, so
    the reference can never drift from the real parser definitions.
    """
    usage = " ".join(sp.format_usage().split())
    parts = usage.split(" ", 3)
    tail = parts[3] if len(parts) == 4 else ""
    return tail.removeprefix("[-h] ").strip()


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse argv and run the selected command.

    Restores the default SIGPIPE handler first. Python ignores SIGPIPE and turns
    a write to a closed stdout into BrokenPipeError, so `codenav grep … | head`
    would end in a traceback (and `--help | head` in "Exception ignored") once
    the reader exits. Dying on SIGPIPE is what a Unix filter does: silent, and
    the shell reports the usual status 141.
    """
    if hasattr(signal, "SIGPIPE"):  # absent on Windows
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    parser = argparse.ArgumentParser(prog="codenav", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "outline",
        help="compact symbol outline of file(s), modules root-first and paginated",
    )
    p.add_argument("paths", nargs="+")
    p.add_argument("--lang", help="override language detection")
    p.add_argument("--lines", action="store_true", help="append L<start>-<end> to each entry")
    p.add_argument(
        "--top-level",
        action="store_true",
        help="print only top-level symbols, omit nested members (attrs, methods)",
    )
    p.add_argument(
        "--deps",
        action="store_true",
        help="under each symbol, list what its own body references (name [kinds]); off by default",
    )
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
        help="page size in chars; pages never split a module or a line (default: 10000)",
    )
    p.add_argument(
        "--pages",
        action="append",
        default=None,
        metavar="SPEC",
        help="page numbers to print, e.g. '2', '2-4', '1,3' (repeatable, unioned; default: page 1)",
    )
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser(
        "diff",
        help="slice code around diff-changed lines; change source: --working-tree/--staged/"
        "--base/--stdin (default: stdin when piped, git working tree on a terminal)",
    )
    p.add_argument(
        "path",
        nargs="?",
        help="file or directory to restrict the diff to (default: whole diff / whole working tree)",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--working-tree",
        action="store_true",
        help="tracked changes vs HEAD (staged + unstaged); source read from disk",
    )
    source.add_argument(
        "--staged",
        action="store_true",
        help="index changes vs HEAD; source read from the index",
    )
    source.add_argument(
        "--base",
        metavar="REF",
        help="diff REF...HEAD (changes since the merge base); source read from the HEAD revision",
    )
    source.add_argument(
        "--stdin",
        action="store_true",
        help="read a unified diff from stdin, even on a terminal",
    )
    source.add_argument(
        "--lines",
        help="explicit changed lines spec, e.g. '10,15-20' (requires PATH; alternative to a change source)",
    )
    p.add_argument(
        "--repo",
        metavar="DIR",
        help="run git there and resolve paths independent of the current directory "
        "(default: current directory)",
    )
    p.add_argument("--lang", help="override language detection")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("symbol", help="print symbol source for each NAME")
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser(
        "impact", help="depends-on/dependents influence chain per NAME, with relation kinds"
    )
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.add_argument(
        "--detailed",
        action="store_true",
        help="include paths, lines, entity kinds, and reference sites (label@line)",
    )
    _add_kind(p)
    p.set_defaults(func=cmd_impact)

    # grep (short: matched lines + symbol span) and astgrep (extended: the whole
    # source of the matched symbol) share every option; only the output differs.
    for name, func, what in (
        ("grep", cmd_grep, "matched lines of every symbol whose body matches any PATTERN"),
        ("astgrep", cmd_astgrep, "full source of every symbol whose body matches any PATTERN"),
    ):
        p = sub.add_parser(name, help=what)
        p.add_argument(
            "patterns",
            nargs="+",
            metavar="PATTERN",
            help="regex patterns; a symbol matching any of them is reported once",
        )
        _add_root(p, what="search")
        p.add_argument("--lang", help="override language detection")
        p.add_argument(
            "--max-chars",
            type=_positive,
            default=10_000,
            help="page size in chars; pages never split a match block (default: 10000)",
        )
        p.add_argument(
            "--pages",
            action="append",
            default=None,
            metavar="SPEC",
            help="page numbers to print, e.g. '2', '2-4', '1,3' (repeatable, unioned; default: page 1)",
        )
        p.set_defaults(func=func)

    p = sub.add_parser(
        "trace",
        help="influence chains through each NAME, labelled with relation kinds "
        "(both sides; --direction narrows the walk)",
    )
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.add_argument(
        "--direction",
        choices=("both", "up", "down"),
        default="both",
        help=(
            "sides to walk: both = through NAME (default); "
            "down = what NAME references; up = what references NAME"
        ),
    )
    p.add_argument("--depth", type=_positive, default=3, help="max depth of each chain in symbols (default: 3)")
    p.add_argument("--max-paths", type=_positive, default=100, help="max chains to show")
    _add_kind(p)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser(
        "path",
        help="shortest chains from SOURCE to TARGET through depends_on relations",
    )
    p.add_argument("source", metavar="SOURCE", help="symbol the chains start at")
    p.add_argument("target", metavar="TARGET", help="symbol the chains must reach")
    _add_root(p)
    p.add_argument(
        "--depth",
        type=_positive,
        default=10,
        help="max symbols per chain, both ends included (default: 10)",
    )
    p.add_argument("--max-paths", type=_positive, default=100, help="max chains to show")
    _add_kind(p)
    p.set_defaults(func=cmd_path)

    p = sub.add_parser(
        "info",
        help="accumulated symbol source + influence paths + impact chain per NAME",
    )
    p.add_argument("names", nargs="+", metavar="NAME", help="symbol names (simple or qualified)")
    _add_root(p)
    p.add_argument(
        "--depth", type=_positive, default=20, help="max depth of the influence chains in symbols (default: 20)"
    )
    p.add_argument(
        "--max-paths",
        type=_positive,
        default=50,
        help="max chains to show (default: 50)",
    )
    _add_kind(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser(
        "doctor",
        help="diagnose indexing: roots, files kept and skipped with reasons, "
        "extraction warnings",
    )
    _add_root(p, what="scan", dirs_only=True)
    p.add_argument(
        "--verbose",
        action="store_true",
        help="list the paths behind the counts instead of the counts alone",
    )
    p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    p.set_defaults(func=cmd_doctor)

    options = "\n".join(
        f"  {name:<9}{_usage_options(sp)}" for name, sp in sub.choices.items()
    )
    parser.description = (
        f"{__doc__}\n"
        "Commands and options (per-command detail: `codenav CMD --help`):\n"
        f"{options}"
    )
    args = parser.parse_args(argv)
    # --kind accepts the short display labels too; commands use full names
    if getattr(args, "kind", None):
        args.kind = resolve_kinds(args.kind)
    args.func(args)


if __name__ == "__main__":
    main()
