"""Diff handling: unified-diff parsing and slicing code around changed lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from codenav.model import MAX_GAP_LINES, Entity, Slice
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
