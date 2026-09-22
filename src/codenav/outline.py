"""Compact outline rendering: module header + one line per symbol."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence

from codenav.model import Entity

LETTERS = {"class": "C", "method": "M", "function": "F", "attr": "A", "constant": "A", "type": "A"}


def module_name(file_path: str) -> str:
    base = file_path
    if base.startswith("./"):
        base = base[2:]
    base = os.path.splitext(base)[0]
    return base.replace(os.sep, ".").replace("/", ".").strip(".") or "<module>"


def render_outline(
    entities: list[Entity],
    file_path: str,
    with_lines: bool = False,
    top_level: bool = False,
    deps: Mapping[Entity, Sequence[str]] | None = None,
) -> str:
    """Format::

        project.mymodule:
        C MyClass
         A my_attr
         M my_method

        F my_func
        A MY_MODULE_ATTR

    With `top_level=True`, nested members (attributes, methods, inner
    classes/functions) are omitted: only symbols directly in the module body
    are printed.

    `deps` maps a symbol to pre-rendered dependency labels; each label is
    printed on its own line right below the symbol, indented one level deeper
    and prefixed with ``->``::

        F my_func
         -> other.helper [call]

    Symbols absent from `deps` (or mapped to an empty sequence) get no extra
    line, so an outline without dependencies stays one line per symbol.

    Dunder names — magic methods (`__init__`, `__repr__`) and metadata
    attributes (`__all__`) — are dropped before rendering: neither is
    outline context. Relations (`impact`/`trace`) are untouched.
    """

    entities = [
        e for e in entities if not (e.name.startswith("__") and e.name.endswith("__"))
    ]

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
    if not top_level:
        for e in entities:
            if e.parent is not None:
                children.setdefault(id(e.parent), []).append(e)

    lines: list[str] = [f"{module_name(file_path)}:"]

    def emit(e: Entity) -> None:
        letter = LETTERS.get(e.kind, e.kind[0].upper() if e.kind else "?")
        suffix = f"  L{e.start_line}-{e.end_line}" if with_lines else ""
        indent = " " * depth(e)
        lines.append(f"{indent}{letter} {e.name}{suffix}")
        if deps:
            lines.extend(f"{indent} -> {label}" for label in deps.get(e, ()))
        if top_level:
            return
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
) -> tuple[list[str], list[str]]:
    """Order, filter, and split module outlines into pages.

    `modules` is (file_path, rendered_outline) pairs. Modules are sorted
    shallowest-first by nesting depth below their root (ties broken by path),
    so earlier pages always carry the modules closest to the project root.

    `filters` are regexes matched against the slash-normalized module path; a
    module is kept when any of them matches (e.g. "schemas|services").

    Returns (pages, oversized): every page body is at most `max_chars` chars
    and never splits a module or a line, so each kept module appears fully on
    exactly one page — unless its text alone exceeds `max_chars`: such a
    module takes a page of its own truncated at a line boundary and its file
    path is returned in `oversized` (the remainder is not paginated). When
    nothing survives, pages is empty.
    """
    if not modules:
        return [], []
    compiled = [re.compile(pattern) for pattern in filters]
    kept: list[tuple[int, str, str]] = []
    for file_path, text in modules:
        posix = file_path.replace(os.sep, "/")
        if compiled and not any(rx.search(posix) for rx in compiled):
            continue
        kept.append((module_depth(file_path, roots), posix, text))
    kept.sort(key=lambda item: (item[0], item[1]))
    if not kept:
        return [], []
    pages, oversized = _split_pages([text for _, _, text in kept], max_chars)
    return pages, [kept[i][1] for i in oversized]


def _split_pages(texts: Sequence[str], limit: int) -> tuple[list[str], list[int]]:
    """Split whole module texts into page bodies of at most ``limit`` chars.

    Pages join whole modules with a blank line between them. A module that
    does not fit into the current page starts the next one; a module larger
    than ``limit`` alone gets a page of its own holding its leading lines
    (never a partial line), and its index lands in the returned oversized
    list — the rest of that module is dropped, not paginated.
    """
    pages: list[str] = []
    oversized: list[int] = []
    page: list[str] = []
    used = 0

    def flush() -> None:
        nonlocal page, used
        if page:
            pages.append("\n\n".join(page))
        page, used = [], 0

    for idx, text in enumerate(texts):
        if used and used + 2 + len(text) <= limit:
            page.append(text)
            used += 2 + len(text)
            continue
        if len(text) <= limit:
            flush()
            page.append(text)
            used = len(text)
            continue
        # Module alone exceeds the page: own page, leading lines only.
        flush()
        oversized.append(idx)
        lines: list[str] = []
        total = 0
        for line in text.split("\n"):
            extra = len(line) + (0 if not lines else 1)
            if total + extra > limit:
                break
            lines.append(line)
            total += extra
        pages.append("\n".join(lines))
    flush()
    return pages, oversized
