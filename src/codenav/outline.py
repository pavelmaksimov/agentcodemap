"""Compact outline rendering: module header + one line per symbol."""

from __future__ import annotations

import os

from codenav.core import Entity

LETTERS = {"class": "C", "method": "M", "function": "F", "attr": "A", "constant": "A", "type": "A"}


def module_name(file_path: str) -> str:
    base = file_path
    if base.startswith("./"):
        base = base[2:]
    base = os.path.splitext(base)[0]
    return base.replace(os.sep, ".").replace("/", ".").strip(".") or "<module>"


def render_outline(entities: list[Entity], file_path: str, with_lines: bool = False) -> str:
    """Format::

        project.mymodule
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
