"""Compact outline rendering: module header + one line per symbol."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from codenav.model import Entity

LETTERS = {"class": "C", "method": "M", "function": "F", "attr": "A", "constant": "A", "type": "A"}


def module_name(file_path: str) -> str:
    base = file_path
    if base.startswith("./"):
        base = base[2:]
    base = os.path.splitext(base)[0]
    return base.replace(os.sep, ".").replace("/", ".").strip(".") or "<module>"


def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
    """Format::

        project.mymodule:
        C MyClass
         A my_attr
         M my_method

        F my_func
        A MY_MODULE_ATTR
    """

    def depth(e: Entity) -> int:
        d = 0
        cur = e.parent
        while cur is not None:
            d += 1
            cur = cur.parent
        return d

    roots = sorted((e for e in entities if e.parent is None), key=lambda e: e.start_line)
    if not roots:
        return ""
    children: dict[int, list[Entity]] = {}
    for e in entities:
        if e.parent is not None:
            children.setdefault(id(e.parent), []).append(e)

    lines: list[str] = [f"{module_name(file_path)}:"]

    def emit(e: Entity) -> None:
        letter = LETTERS.get(e.kind, e.kind[0].upper() if e.kind else "?")
        suffix = f"  L{e.start_line}-{e.end_line}" if with_lines else ""
        indent = " " * depth(e)
        lines.append(f"{indent}{letter} {e.name}{suffix}")
        for child in sorted(children.get(id(e), []), key=lambda c: c.start_line):
            emit(child)

    for i, root in enumerate(roots):
        if i > 0:
            lines.append("")
        emit(root)
    return "\n".join(lines)


def module_depth(file_path: str, roots: Sequence[str]) -> int:
    """Nesting depth of a module below the root argument that contains it.

    0 = the file sits directly inside a root directory, 1 = one directory
    deeper, and so on. Paths not under any root (or roots that are plain
    files) default to 0.
    """
    norm = os.path.normpath(file_path)
    depths: list[int] = []
    for r in roots:
        root = os.path.normpath(r)
        if root == os.curdir:
            # '.' contains every relative path; absolute paths are outside it.
            if not os.path.isabs(norm):
                depths.append(norm.count(os.sep))
            continue
        if norm == root:
            return 0
        prefix = root + os.sep
        if norm.startswith(prefix):
            depths.append(norm[len(prefix):].count(os.sep))
    return min(depths) if depths else 0


def assemble_outline(
    modules: Sequence[tuple[str, str]],
    *,
    roots: Sequence[str] = (),
    filters: Sequence[str] = (),
    max_chars: int = 10_000,
) -> tuple[str, int, int]:
    """Order, filter, and cap module outlines for printing.

    `modules` is (file_path, rendered_outline) pairs. Modules are sorted
    shallowest-first by nesting depth below their root (ties broken by path),
    so when `max_chars` cuts the output the modules closest to the project
    root are always kept.

    `filters` are regexes matched against the slash-normalized module path; a
    module is kept when any of them matches (e.g. "schemas|services").

    Returns (body, shown, omitted): body is at most `max_chars` chars and
    never splits a line; shown is the count of fully printed modules; omitted
    is the count cut off by the cap. When nothing survives, body is "" and
    shown is 0.
    """
    if not modules:
        return "", 0, 0
    compiled = [re.compile(pattern) for pattern in filters]
    kept: list[tuple[int, str, str]] = []
    for file_path, text in modules:
        posix = file_path.replace(os.sep, "/")
        if compiled and not any(rx.search(posix) for rx in compiled):
            continue
        kept.append((module_depth(file_path, roots), posix, text))
    kept.sort(key=lambda item: (item[0], item[1]))
    if not kept:
        return "", 0, 0
    body, omitted = _join_limited([text for _, _, text in kept], max_chars)
    return body, len(kept) - omitted, omitted


def _join_limited(texts: Sequence[str], limit: int) -> tuple[str, int]:
    """Join module texts with one blank line between them inside a char budget.

    Whole modules are appended while they fit; once the next module would
    overflow, its leading full lines are appended (never a partial line) and
    the rest of the list is dropped. Returns (body, omitted_modules).
    """
    out: list[str] = []
    used = 0
    for idx, text in enumerate(texts):
        sep = "" if idx == 0 else "\n\n"
        if used + len(sep) + len(text) <= limit:
            out.append(sep + text)
            used += len(sep) + len(text)
            continue
        # Cap hit: fit full lines of this module, then stop.
        joined: list[str] = []
        total = used + len(sep)
        for line in text.split("\n"):
            extra = len(line) + (0 if not joined else 1)
            if total + extra > limit:
                break
            joined.append(line)
            total += extra
        if joined:
            out.append(sep + "\n".join(joined))
        return "".join(out), len(texts) - idx
    return "".join(out), 0
