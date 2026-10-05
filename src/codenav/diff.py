"""Diff handling: unified-diff parsing, slicing code around changed lines, and the `codenav diff` command.

The change source (git working tree / index / a base ref / stdin) is resolved
here too, so the CLI layer only wires arguments to :func:`cmd_diff`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from codenav.model import (
    MAX_GAP_LINES,
    Entity,
    Slice,
    detect_language,
    parse_int_spec,
)
from codenav.parse import parse_file


def slice_diff(file_path: str, content: str, changed_lines: set[int], language: str) -> list[Slice]:
    """Expand diff-changed lines to enclosing symbols; merge into slices (gap <= MAX_GAP_LINES)."""
    parsed = parse_file(file_path, content, language, collect_refs=False)
    if parsed is None:
        return []

    non_empty = {
        ln
        for ln in changed_lines
        if 1 <= ln <= len(parsed.content_lines) and parsed.content_lines[ln - 1].strip()
    }
    if not non_empty:
        return []

    slices: list[Slice] = []
    import_lines: set[int] = set()
    for imp in parsed.imports:
        if any(imp.start_line <= ln <= imp.end_line for ln in non_empty):
            slices.append(imp)
            import_lines |= set(range(imp.start_line, imp.end_line + 1))
    covered: set[int] = set()

    targets: list[Entity] = []
    by_line: dict[int, list[Entity]] = {}
    for e in parsed.entities:
        for ln in range(e.start_line, e.end_line + 1):
            by_line.setdefault(ln, []).append(e)

    seen_targets: set[int] = set()
    for cl in sorted(non_empty):
        covering = by_line.get(cl, [])
        if not covering:
            continue
        target = min(covering, key=lambda e: e.end_line - e.start_line)
        if target.kind in ("attr", "constant"):
            better = [e for e in covering if e.kind in ("method", "function")] or [
                e for e in covering if e.kind == "class"
            ]
            target = min(better or covering, key=lambda e: e.end_line - e.start_line)
        elif target.kind in ("method", "function"):
            funcs = [e for e in covering if e.kind in ("method", "function")]
            if len(funcs) > 1:
                target = max(funcs, key=lambda e: e.end_line - e.start_line)
        covered |= set(range(target.start_line, target.end_line + 1))
        if id(target) not in seen_targets:
            seen_targets.add(id(target))
            targets.append(target)

    ranges: list[tuple[int, int]] = []
    for sl in sorted(covered - import_lines):
        if ranges and sl <= ranges[-1][1] + MAX_GAP_LINES:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], sl))
        else:
            ranges.append((sl, sl))

    for start, end in ranges:
        best = next(
            (
                t
                for t in sorted(targets, key=lambda e: e.end_line - e.start_line)
                if t.start_line <= end and t.end_line >= start
            ),
            None,
        )
        slices.append(
            Slice(
                start,
                end,
                "\n".join(parsed.content_lines[start - 1 : end]),
                best.kind if best else "block",
                best.name if best else "<unknown>",
            )
        )
    slices.sort(key=lambda s: s.start_line)
    return slices


@dataclass
class DiffFile:
    """One file entry of a parsed unified diff."""

    path: str
    status: str  # "changed" | "added" | "deleted"
    added_lines: set[int] = field(default_factory=set)


def parse_unified_diff(diff: str) -> dict[str, DiffFile]:
    """Parse a unified diff -> per-file status and added (new-side) lines."""
    result: dict[str, DiffFile] = {}
    old_path: str | None = None
    current: DiffFile | None = None
    new_ln = 0
    for raw in diff.split("\n"):
        if raw.startswith("--- "):
            old_path = raw[4:].split("\t")[0].removeprefix("a/")
        elif raw.startswith("+++ "):
            new_path = raw[4:].split("\t")[0].removeprefix("b/")
            if new_path == "/dev/null":
                current = DiffFile(path=old_path or "", status="deleted")
            elif old_path == "/dev/null":
                current = DiffFile(path=new_path, status="added")
            else:
                current = DiffFile(path=new_path, status="changed")
            result[current.path] = current
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            if m:
                new_ln = int(m.group(1))
        elif current is not None and current.status != "deleted":
            if raw.startswith("+"):
                current.added_lines.add(new_ln)
                new_ln += 1
            elif not raw.startswith(("\\", "-")):
                new_ln += 1
    return result


def added_lines_from_unified_diff(diff: str) -> dict[str, set[int]]:
    """Parse a unified diff -> per-file sets of added (new-side) line numbers."""
    return {
        f.path: f.added_lines
        for f in parse_unified_diff(diff).values()
        if f.status == "changed" and f.added_lines
    }


def read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def lang_or_die(path: str, lang: str | None) -> str:
    resolved = lang or detect_language(path)
    if not resolved:
        sys.exit(f"codenav: cannot detect language for {path}; pass --lang")
    return resolved


def print_slice(sl: Slice) -> None:
    header = f"{sl.kind} {sl.name}" if sl.name != "<unknown>" else sl.kind
    print(f"### L{sl.start_line}-{sl.end_line}  [{header}]")
    for i, line in enumerate(sl.content.split("\n"), start=sl.start_line):
        print(f"{i}\t{line}")
    print()


def disk_reader(base: str | None) -> Callable[[str], str]:
    """Content reader for on-disk sources: relative paths resolve against base."""

    def read(path: str) -> str:
        if base is None or os.path.isabs(path):
            return read_file(path)
        return read_file(os.path.join(base, path))

    return read


def git_reader(repo: str | None, spec: str, label: str) -> Callable[[str], str]:
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


def git_toplevel(repo: str | None) -> str | None:
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


# Pin the output format parse_unified_diff expects, whatever the user's git
# config says: plain a/ b/ prefixes (diff.mnemonicPrefix, diff.noprefix),
# no color (color.diff=always), no external/textconv drivers, root-relative
# (diff.relative) and unquoted non-ASCII paths (core.quotePath).
_GIT_DIFF_CONFIG = ["-c", "core.quotePath=false"]
_GIT_DIFF_FLAGS = [
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]


def git_diff(
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
                ["git", *_GIT_DIFF_CONFIG, *variant, *_GIT_DIFF_FLAGS, *tail],
                cwd=repo,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            return None, str(exc)
        if proc.returncode == 0:
            return proc.stdout, ""
        error = proc.stderr.strip()
    return None, error


def cmd_diff(args) -> None:
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
            args.path, parse_int_spec(args.lines), args.lang, disk_reader(args.repo)
        )
        return
    git_source = args.working_tree or args.staged or args.base
    if args.stdin or (not git_source and not sys.stdin.isatty()):
        _diff_from_stdin(args)
        return
    top = git_toplevel(args.repo)
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
        diff_text, error = git_diff(args.repo, [["diff", f"{args.base}...HEAD"]], paths)
        if diff_text is None:
            sys.exit(f"codenav diff: git diff {args.base}...HEAD failed: {error}")
        read = git_reader(args.repo, "HEAD:", "HEAD")
    elif args.staged:
        # Index vs HEAD; a checkout without HEAD compares the index with the tree.
        diff_text, error = git_diff(
            args.repo, [["diff", "--cached", "HEAD"], ["diff", "--cached"]], paths
        )
        read = git_reader(args.repo, ":", "index")
    else:
        diff_text, error = git_diff(args.repo, [["diff", "HEAD"], ["diff"]], paths)
        read = disk_reader(top)
    if diff_text is None:
        sys.exit(f"codenav diff: git diff failed: {error}")
    if not diff_text:
        print("(no changes)")
        return
    _diff_all_files(parse_unified_diff(diff_text), read)


def _diff_from_stdin(args) -> None:
    """Explicit --stdin and the legacy piped mode: slice the unified diff as given."""
    per_file = parse_unified_diff(sys.stdin.read())
    read = disk_reader(args.repo)
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
    language = lang_or_die(path, lang)
    slices = slice_diff(path, content, changed_lines, language)
    if not slices:
        print("(no slices)")
        return
    for sl in slices:
        print_slice(sl)


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
            print_slice(sl)
