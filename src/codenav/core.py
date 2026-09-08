"""Tree-sitter core: entity extraction, diff slicing, symbol lookup, symbol grep.

Line numbers are 1-indexed, end_line inclusive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from heapq import nsmallest

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser

logger = logging.getLogger(__name__)

# Symbols never shown as graph/impact nodes: ubiquitous infra names add noise.
GRAPH_EXCLUDED_SYMBOLS = frozenset({"logger"})

# language alias -> tree-sitter grammar name
LANGUAGES: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "react": "javascript",
    "react-typescript": "tsx",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "scala": "scala",
    "ruby": "ruby",
    "php": "php",
    "csharp": "csharp",
    "cpp": "cpp",
    "c": "c",
}

EXT_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "react-typescript",
    ".jsx": "react",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".scala": "scala",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".c": "c",
    ".h": "c",
}

ENTITY_NODE_TYPES: dict[str, tuple[str, ...]] = {
    "python": (
        "function_definition",
        "class_definition",
        "decorated_definition",
        "assignment",
    ),
    "javascript": (
        "function_declaration",
        "function",
        "method_definition",
        "class_declaration",
        "class",
        "interface_declaration",
        "enum_declaration",
        "lexical_declaration",
        "variable_declaration",
        "type_alias_declaration",
    ),
    "tsx": (
        "function_declaration",
        "function",
        "method_definition",
        "class_declaration",
        "class",
        "interface_declaration",
        "enum_declaration",
        "lexical_declaration",
        "variable_declaration",
        "type_alias_declaration",
    ),
    "typescript": (
        "function_declaration",
        "function",
        "method_definition",
        "class_declaration",
        "class",
        "interface_declaration",
        "enum_declaration",
        "lexical_declaration",
        "variable_declaration",
        "type_alias_declaration",
    ),
    "go": ("function_declaration", "type_declaration", "const_declaration", "var_declaration"),
    "rust": (
        "function_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "impl_item",
        "const_item",
        "type_item",
    ),
    "java": ("class_declaration", "enum_declaration", "interface_declaration", "method_declaration"),
    "scala": (
        "class_definition",
        "object_definition",
        "trait_definition",
        "function_definition",
        "val_definition",
        "var_definition",
    ),
    "ruby": ("method", "class", "module"),
    "php": ("function_definition", "class_declaration"),
    "csharp": ("class_declaration", "interface_declaration", "enum_declaration", "method_declaration"),
    "cpp": ("function_definition", "class_specifier", "enum_specifier"),
    "c": ("function_definition",),
}

IMPORT_NODE_TYPES: dict[str, tuple[str, ...]] = {
    "python": ("import_statement", "import_from_statement"),
    "javascript": ("import_statement",),
    "typescript": ("import_statement",),
    "tsx": ("import_statement",),
    "go": ("import_declaration",),
    "rust": ("use_declaration",),
    "java": ("import_declaration",),
    "scala": ("import_declaration",),
    "php": ("namespace_use_declaration",),
    "csharp": ("using_directive",),
    "cpp": ("preproc_include",),
    "c": ("preproc_include",),
}

NESTED_ENTITY_PARENT_TYPES = frozenset(
    {
        "function_definition",
        "class_definition",
        "class_body",
        "decorated_definition",
        "function_declaration",
        "function",
        "method_definition",
        "method_declaration",
        "method",
        "function_item",
        "impl_item",
        "class_declaration",
        "class",
        "class_specifier",
        "object_definition",
        "trait_definition",
        "module",
    }
)

CLASS_LIKE_TYPES = frozenset(
    {
        "class_definition",
        "class_declaration",
        "class",
        "struct_item",
        "enum_item",
        "trait_item",
        "enum_declaration",
        "interface_declaration",
        "class_specifier",
        "enum_specifier",
        "object_definition",
        "trait_definition",
        "module",
        "impl_item",
    }
)

ASSIGN_LIKE_TYPES = frozenset(
    {
        "assignment",
        "lexical_declaration",
        "variable_declaration",
        "val_definition",
        "var_definition",
        "const_declaration",
        "const_item",
    }
)

MAX_GAP_LINES = 5


@dataclass(frozen=True)
class Slice:
    """A contiguous block of source lines belonging to one entity."""

    start_line: int
    end_line: int
    content: str
    kind: str  # class | method | function | attr | constant | import | block | ...
    name: str  # entity name or "<unknown>" / "<import>"


@dataclass(eq=False)
class Entity:
    file: str
    kind: str  # class | method | function | attr | constant | type
    name: str
    start_line: int  # 1-indexed inclusive
    end_line: int  # 1-indexed inclusive
    parent: "Entity | None" = None

    @property
    def qualified_name(self) -> str:
        parts: list[str] = []
        cur: Entity | None = self
        while cur is not None and cur.name != "<unknown>":
            parts.append(cur.name)
            cur = cur.parent
        return ".".join(reversed(parts)) if parts else self.name

    @property
    def location(self) -> str:
        return f"{self.file}:{self.start_line}-{self.end_line}"

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


@dataclass
class ParsedFile:
    """Parsed source file: entities, imports, identifier references."""

    path: str
    content_lines: list[str]
    entities: list[Entity] = field(default_factory=list)
    imports: list[Slice] = field(default_factory=list)
    # identifier text -> entities whose body references it (definition names excluded)
    refs: dict[str, set[Entity]] = field(default_factory=dict)
    # bare identifier references, excluding identifiers inside member access
    bare_refs: dict[str, set[Entity]] = field(default_factory=dict)
    # qualified attribute text (e.g. ``ChatMessage.session``) -> owners
    qualified_refs: dict[str, set[Entity]] = field(default_factory=dict)
    # qualified declaration/variable name -> declared or inferred type
    declared_types: dict[str, str] = field(default_factory=dict)

    def find_symbol(self, name: str) -> list[Entity]:
        """Entities matching simple or dotted qualified name (suffix match on qualified)."""
        out = [
            e
            for e in self.entities
            if e.name == name or e.qualified_name == name or e.qualified_name.endswith("." + name)
        ]
        exact = [e for e in out if e.name == name]
        return exact or out

    def grep_symbols(self, pattern: str) -> list[tuple[Entity | None, list[tuple[int, str]]]]:
        """grep-ast style: regex matches grouped by smallest enclosing symbol.

        Returns (entity_or_None_for_module_level, [(line_no, line_text)]) pairs.
        """
        rx = re.compile(pattern)
        hits: list[tuple[Entity | None, list[tuple[int, str]]]] = []
        claimed: set[int] = set()
        for e in sorted(self.entities, key=lambda e: e.end_line - e.start_line):
            matched = [
                (ln, self.content_lines[ln - 1])
                for ln in range(e.start_line, min(e.end_line, len(self.content_lines)) + 1)
                if ln not in claimed and rx.search(self.content_lines[ln - 1])
            ]
            if matched:
                claimed.update(range(e.start_line, e.end_line + 1))
                hits.append((e, matched))

        module_level = [
            (i, line)
            for i, line in enumerate(self.content_lines, start=1)
            if i not in claimed and rx.search(line)
        ]
        if module_level:
            hits.append((None, module_level))
        return hits


def detect_language(file_path: str) -> str | None:
    dot = file_path.rfind(".")
    if dot == -1:
        return None
    return EXT_LANGUAGES.get(file_path[dot:].lower())


_parsers: dict[str, Parser] = {}


def _get_parser(language: str) -> Parser | None:
    ts_language = LANGUAGES.get(language.lower())
    if not ts_language:
        return None
    if ts_language not in _parsers:
        try:
            _parsers[ts_language] = get_parser(ts_language)
        except Exception:
            logger.exception("Failed to load tree-sitter parser for %s", ts_language)
            return None
    return _parsers[ts_language]


def parse_file(path: str, content: str, language: str) -> ParsedFile | None:
    parser = _get_parser(language)
    if not parser:
        return None
    try:
        tree = parser.parse(content.encode())
    except Exception:
        logger.exception("tree-sitter failed to parse %s", path)
        return None

    content_bytes = content.encode()
    content_lines = content.split("\n")
    ts_language = LANGUAGES.get(language.lower(), language.lower())
    entity_types = ENTITY_NODE_TYPES.get(ts_language, ())
    import_types = IMPORT_NODE_TYPES.get(ts_language, ())

    parsed = ParsedFile(path=path, content_lines=content_lines)

    def simple_type_name(text: str) -> str:
        text = text.strip().strip("\"'")
        return text.split("[", 1)[0].rsplit(".", 1)[-1]

    def node_text(node: Node | None) -> str:
        if node is None:
            return ""
        return content_bytes[node.start_byte : node.end_byte].decode()

    def first_declarator_name(node: Node) -> str:
        for child in node.children:
            if child.type == "variable_declarator":
                return node_text(child.child_by_field_name("name"))
        return ""

    def make_entity(node: Node, parent: Entity | None) -> tuple[Entity, Node | None]:
        """Build an Entity from an entity-typed node. Returns (entity, name_node)."""
        name_node = node.child_by_field_name("name")
        name_text = node_text(name_node)

        if node.type in ASSIGN_LIKE_TYPES:
            kind = "attr"
            left = node.child_by_field_name("left") or node.child_by_field_name("pattern")
            name_text = node_text(left) or name_text or first_declarator_name(node)
            if name_text.isupper():
                kind = "constant"
            name_node = left
        elif node.type == "type_alias_declaration" or node.type == "type_item":
            kind = "type"
        elif node.type in CLASS_LIKE_TYPES:
            kind = "class"
            if not name_text:
                for child in node.children:
                    if child.type.endswith(("type_identifier", "generic_type")):
                        name_text = node_text(child)
                        break
        elif node.type in ("method_definition", "method_declaration"):
            kind = "method"
        else:
            kind = "method" if parent is not None and parent.kind == "class" else "function"

        return (
            Entity(
                file=path,
                kind=kind,
                name=name_text or "<unknown>",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent=parent,
            ),
            name_node,
        )

    # pass 1: extract entities and import spans
    def walk(node: Node, parent_entity: Entity | None, top_level: bool) -> None:
        if node.type in import_types and top_level:
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            parsed.imports.append(
                Slice(start, end, "\n".join(content_lines[start - 1 : end]), "import", "<import>")
            )
            return

        if node.type in entity_types and node.type != "decorated_definition":
            # locals and functions nested inside callables are not project symbols
            is_local = (
                parent_entity is not None
                and parent_entity.kind in ("method", "function")
                and (
                    node.type in ASSIGN_LIKE_TYPES
                    or make_entity(node, None)[0].kind in ("function", "method")
                )
            )
            if node.type in ASSIGN_LIKE_TYPES:
                left = node.child_by_field_name("left") or node.child_by_field_name("pattern")
                left_name = node_text(left)
                type_node = node.child_by_field_name("type")
                type_name = simple_type_name(node_text(type_node)) if type_node is not None else ""
                if not type_name:
                    right = node.child_by_field_name("right")
                    function = right.child_by_field_name("function") if right is not None else None
                    if function is not None and function.type == "identifier":
                        type_name = node_text(function)
                if left_name and type_name:
                    prefix = f"{parent_entity.qualified_name}." if parent_entity else ""
                    parsed.declared_types[f"{prefix}{left_name}"] = type_name
            if not is_local:
                entity, _ = make_entity(node, parent_entity)
                parsed.entities.append(entity)
                parent_entity = entity

        child_top_level = top_level and (
            node.parent is None or node.type not in NESTED_ENTITY_PARENT_TYPES
        )
        for child in node.children:
            walk(child, parent_entity, child_top_level)

    walk(tree.root_node, None, True)

    # decorated python definitions: extend first inner entity to cover decorators
    def absorb_decorators(node: Node) -> None:
        if node.type == "decorated_definition":
            deco_start = node.start_point[0] + 1
            for child in node.children:
                if child.type != "decorator":
                    line = child.start_point[0] + 1
                    for e in parsed.entities:
                        if e.start_line == line:
                            e.start_line = deco_start
                            break
                    absorb_decorators(child)
            return
        for child in node.children:
            absorb_decorators(child)

    absorb_decorators(tree.root_node)

    # pass 2: collect identifier refs attributed to innermost containing entity;
    # definition-name nodes are excluded from refs.
    def find_owner(node: Node) -> Entity | None:
        line = node.start_point[0] + 1

        def size(e: Entity) -> int:
            return e.end_line - e.start_line

        covering = [e for e in parsed.entities if e.contains(line)]
        if not covering:
            return None
        callables = [e for e in covering if e.kind in ("method", "function", "class")]
        return min(callables or covering, key=size)

    def gather_def_positions(node: Node, acc: set[tuple[int, int]]) -> None:
        if node.type in entity_types and node.type != "decorated_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None and node.type in ASSIGN_LIKE_TYPES:
                name_node = node.child_by_field_name("left") or node.child_by_field_name("pattern")
            if name_node is not None:
                acc.add((name_node.start_byte, name_node.end_byte))
                stack = [name_node]
                while stack:
                    cur = stack.pop()
                    for c in cur.children:
                        if c.type == "identifier":
                            acc.add((c.start_byte, c.end_byte))
                        stack.append(c)
        for child in node.children:
            gather_def_positions(child, acc)

    # word tokens for string-literal scanning (DI paths, forward refs)
    word_rx = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")

    def walk_refs(
        node: Node,
        owner: Entity | None,
        skip: set[tuple[int, int]],
        inside_attribute: bool = False,
    ) -> None:
        if owner is None:
            pass
        elif node.type == "attribute":
            object_node = node.child_by_field_name("object")
            attribute_node = node.child_by_field_name("attribute")
            if object_node is not None and attribute_node is not None:
                qualified = f"{node_text(object_node)}.{node_text(attribute_node)}"
                parsed.qualified_refs.setdefault(qualified, set()).add(owner)
            inside_attribute = True
        elif node.type == "identifier" and (node.start_byte, node.end_byte) not in skip:
            name = node_text(node)
            parsed.refs.setdefault(name, set()).add(owner)
            if not inside_attribute:
                parsed.bare_refs.setdefault(name, set()).add(owner)
        elif (
            node.type == "string"
            and node.parent is not None
            and node.parent.type not in ("expression_statement", "block", "module")
        ):
            # and forward annotations ("Symbol"); docstrings are expression
            # statements and stay excluded
            text = node_text(node)
            if len(text) <= 500:
                for token in word_rx.findall(text):
                    parsed.refs.setdefault(token, set()).add(owner)
        for child in node.children:
            walk_refs(child, find_owner(child), skip, inside_attribute)

    skip: set[tuple[int, int]] = set()
    gather_def_positions(tree.root_node, skip)
    walk_refs(tree.root_node, find_owner(tree.root_node), skip)

    return parsed


def slice_diff(file_path: str, content: str, changed_lines: set[int], language: str) -> list[Slice]:
    """Expand diff-changed lines to enclosing symbols; merge into slices (gap <= MAX_GAP_LINES)."""
    parsed = parse_file(file_path, content, language)
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


@dataclass
class ImpactReport:
    """Impact chain for a symbol: what it uses and what uses it."""

    target: Entity
    depends_on: list[Entity]
    dependents: list[Entity]


class RepoIndex:
    """Index over a directory tree for symbol lookup and impact analysis.

    Impact resolution prefers qualified member references and declared receiver
    types; unqualified bare names are used only when the symbol name is unique.
    """

    # ponytail: size cap skips generated/minified bundles; per-language ignore files if needed
    MAX_FILE_BYTES = 512 * 1024
    SKIP_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache"})

    def __init__(self, root: str, languages: list[str] | None = None) -> None:
        import os

        self.root = root
        self.files: dict[str, ParsedFile] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in self.SKIP_DIRS and not d.startswith(".")
            )
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                lang = detect_language(fn)
                if lang is None or os.path.getsize(full) > self.MAX_FILE_BYTES:
                    continue
                if languages and lang not in languages:
                    continue
                try:
                    content = open(full, encoding="utf-8").read()
                except (OSError, UnicodeDecodeError):
                    continue
                parsed = parse_file(full, content, lang)
                if parsed is not None:
                    self.files[full] = parsed
        self._by_name: dict[str, list[Entity]] = {}
        self._attribute_types: dict[str, set[str]] = {}
        self._bare_ref_owners: dict[str, set[Entity]] = {}
        self._qualified_ref_owners_by_member: dict[str, dict[str, set[Entity]]] = {}
        self._qualified_ref_owners_by_receiver: dict[str, dict[str, set[Entity]]] = {}
        for pf in self.files.values():
            for e in pf.entities:
                if e.name != "<unknown>":
                    self._by_name.setdefault(e.name, []).append(e)
            for qualified, type_name in pf.declared_types.items():
                attribute = qualified.rsplit(".", 1)[-1]
                self._attribute_types.setdefault(attribute, set()).add(type_name)
            for name, owners in pf.bare_refs.items():
                self._bare_ref_owners.setdefault(name, set()).update(owners)
            for reference, owners in pf.qualified_refs.items():
                receiver, member = reference.rsplit(".", 1)
                receiver = receiver.rsplit(".", 1)[-1]
                self._qualified_ref_owners_by_member.setdefault(member, {}).setdefault(
                    reference, set()
                ).update(owners)
                self._qualified_ref_owners_by_receiver.setdefault(receiver, {}).setdefault(
                    reference, set()
                ).update(owners)

    def find_symbol(self, name: str) -> list[Entity]:
        out: list[Entity] = []
        for pf in self.files.values():
            out.extend(pf.find_symbol(name))
        exact = [e for e in out if e.name == name]
        return exact or out

    def impact(self, name: str) -> ImpactReport | None:
        targets = self.find_symbol(name)
        if not targets:
            return None
        return self.impact_entity(targets[0])

    def impact_entity(self, target: Entity) -> ImpactReport:
        """Impact for an exact definition, without resolving its name again."""
        pf = self.files.get(target.file)
        if pf is None:
            return ImpactReport(target=target, depends_on=[], dependents=[])

        deps: list[Entity] = []
        dep_keys: set[tuple] = set()
        for owner in self._owners_referencing(target):
            if owner.name in GRAPH_EXCLUDED_SYMBOLS or owner is target or any(
                a.file == target.file and a.contains(target.start_line)
                for a in _ancestors(owner)
            ):
                continue
            key = (owner.file, owner.qualified_name, owner.start_line)
            if key not in dep_keys:
                dep_keys.add(key)
                deps.append(owner)
        # bare refs are attributed to the innermost owner (a method, not its class),
        # so "what the target uses" aggregates over the whole subtree
        members = {id(e) for e in pf.entities if e is target or _within(e, target)}
        depends_on: list[Entity] = []
        seen: set[tuple] = set()
        for used, owners in pf.bare_refs.items():
            if used in GRAPH_EXCLUDED_SYMBOLS or not any(id(o) in members for o in owners):
                continue
            candidates = self._by_name.get(used, [])
            for cand in candidates:
                if cand is target or cand.qualified_name == target.qualified_name:
                    continue
                if len(candidates) > 1 and not self._qualified_dependency_used(cand, pf, members):
                    continue
                if cand.kind == "attr" and not self._qualified_dependency_used(cand, pf, members):
                    continue
                key = (cand.file, cand.qualified_name, cand.start_line)
                if key not in seen:
                    seen.add(key)
                    depends_on.append(cand)
        for candidate in self._qualified_dependencies(pf, members):
            if candidate is target or candidate.qualified_name == target.qualified_name:
                continue
            key = (candidate.file, candidate.qualified_name, candidate.start_line)
            if key not in seen:
                seen.add(key)
                depends_on.append(candidate)
        return ImpactReport(target=target, depends_on=depends_on, dependents=deps)

    def _owners_referencing(self, target: Entity) -> set[Entity]:
        owners: set[Entity] = set()
        qualified = dict(self._qualified_ref_owners_by_member.get(target.name, {}))
        if target.kind in ("class", "type"):
            for reference, references in self._qualified_ref_owners_by_receiver.get(
                target.name, {}
            ).items():
                qualified.setdefault(reference, set()).update(references)
        for reference, references in qualified.items():
            receiver = reference.rsplit(".", 1)[0]
            if target.kind in ("class", "type") and receiver.rsplit(".", 1)[-1] == target.name:
                owners.update(references)
            elif self._reference_matches(target, reference, references):
                owners.update(references)
        if target.kind != "attr" and len(self._by_name.get(target.name, [])) == 1:
            owners.update(self._bare_ref_owners.get(target.name, set()))
        return owners

    def _reference_matches(
        self, target: Entity, reference: str, owners: set[Entity]
    ) -> bool:
        if not reference.endswith(f".{target.name}"):
            return False
        if reference == target.qualified_name:
            return True
        target_class = next((ancestor.name for ancestor in _ancestors(target) if ancestor.kind == "class"), None)
        if target_class is None:
            return False
        receiver = reference.rsplit(".", 1)[0]
        if receiver == target_class:
            return True
        if receiver == "self":
            return any(
                any(ancestor.kind == "class" and ancestor.name == target_class for ancestor in _ancestors(owner))
                for owner in owners
            )
        receiver_name = receiver.rsplit(".", 1)[-1]
        return target_class in self._attribute_types.get(receiver_name, set())

    def _qualified_dependency_used(
        self, candidate: Entity, parsed: ParsedFile, members: set[int]
    ) -> bool:
        for reference, owners in parsed.qualified_refs.items():
            if not any(id(owner) in members for owner in owners):
                continue
            if reference == candidate.qualified_name:
                return True
            if reference != f"self.{candidate.name}":
                continue
            candidate_class = next(
                (ancestor.name for ancestor in _ancestors(candidate) if ancestor.kind == "class"),
                None,
            )
            if candidate_class and any(
                any(ancestor.kind == "class" and ancestor.name == candidate_class for ancestor in _ancestors(owner))
                for owner in owners
            ):
                return True
        return False

    def _qualified_dependencies(self, parsed: ParsedFile, members: set[int]) -> set[Entity]:
        result: set[Entity] = set()
        for reference, owners in parsed.qualified_refs.items():
            if not any(id(owner) in members for owner in owners):
                continue
            for candidate in self._by_name.get(reference.rsplit(".", 1)[-1], []):
                if candidate.qualified_name == reference or self._qualified_dependency_used(
                    candidate, parsed, members
                ):
                    result.add(candidate)
        return result

    def _entity_key(self, e: Entity) -> tuple:
        return (e.file, e.qualified_name, e.start_line)

    def influence_paths(
        self, name: str, max_nodes: int = 5, max_paths: int = 100
    ) -> list[list[Entity]]:
        paths, _ = self.influence_paths_with_total(
            name, max_nodes=max_nodes, max_paths=max_paths
        )
        return paths

    def influence_paths_with_total(
        self, name: str, max_nodes: int = 5, max_paths: int = 100
    ) -> tuple[list[list[Entity]], int]:
        """Return visible paths and their total before max_paths truncation.

        Direction: A -> B means "A references B". Nodes are definition sites,
        so same-named symbols in different files stay distinct. max_nodes limits
        each rendered path without changing how many paths are found. Relations
        use the same qualified/type-aware resolution as impact(). Deterministic
        order, capped at max_paths. Unknown symbols and isolated nodes return an
        empty list with a zero total.
        """
        targets = self.find_symbol(name)
        if not targets:
            return [], 0
        return self.influence_paths_entity_with_total(
            targets[0], max_nodes=max_nodes, max_paths=max_paths
        )

    def influence_paths_entity(
        self, target: Entity, max_nodes: int = 5, max_paths: int = 100
    ) -> list[list[Entity]]:
        paths, _ = self.influence_paths_entity_with_total(
            target, max_nodes=max_nodes, max_paths=max_paths
        )
        return paths

    def influence_paths_entity_with_total(
        self, target: Entity, max_nodes: int = 5, max_paths: int = 100
    ) -> tuple[list[list[Entity]], int]:
        """Influence paths for an exact definition, without name re-resolution."""
        if max_nodes < 1 or max_paths < 1:
            return [], 0

        tq = self._entity_key(target)
        rep = {
            self._entity_key(entity): entity
            for parsed in self.files.values()
            for entity in parsed.entities
        }
        if tq not in rep:
            return [], 0
        reports: dict[tuple, ImpactReport] = {}

        def neighbors(node: tuple, relation: str) -> list[tuple]:
            if node not in reports:
                reports[node] = self.impact_entity(rep[node])
            report = reports[node]
            entities = report.depends_on if relation == "down" else report.dependents
            return sorted(
                (self._entity_key(entity) for entity in entities),
                key=lambda key: (rep[key].kind, key),
            )

        def simple_paths(start: tuple, relation: str) -> list[list[tuple]]:
            # ponytail: hard caps keep hub symbols (Container, logger) tractable;
            # raise MAX_ENUM/MAX_FANOUT if deeper exploration is ever needed
            max_enum = 2000
            max_fanout = 20
            result: list[list[tuple]] = []
            stack: list[list[tuple]] = [[start]]
            enumerated = 0
            while stack and enumerated < max_enum:
                p = stack.pop()
                enumerated += 1
                next_nodes = [
                    nb for nb in neighbors(p[-1], relation)[:max_fanout] if nb not in p
                ]
                if not next_nodes or enumerated == max_enum:
                    result.append(p)
                else:
                    stack.extend(p + [nb] for nb in next_nodes)
            return result

        down = simple_paths(tq, "down")
        up = simple_paths(tq, "up")
        if up == [[tq]] and down == [[tq]]:
            return [], 0

        def trim(path: list[tuple]) -> list[tuple]:
            if len(path) <= max_nodes:
                return path
            target_index = path.index(tq)
            start = max(0, target_index - max_nodes // 2)
            end = min(len(path), start + max_nodes)
            return path[max(0, end - max_nodes) : end]

        def candidates():
            merged_up: set[int] = set()
            merged_down: set[int] = set()
            for incoming_index, incoming in enumerate(up):
                for outgoing_index, outgoing in enumerate(down):
                    path = list(reversed(incoming))[:-1] + outgoing
                    if len(path) > 1 and len(set(path)) == len(path):
                        merged_up.add(incoming_index)
                        merged_down.add(outgoing_index)
                        yield path
            for index, incoming in enumerate(up):
                if index not in merged_up and len(incoming) > 1:
                    path = list(reversed(incoming))
                    yield path
            for index, outgoing in enumerate(down):
                if index not in merged_down and len(outgoing) > 1:
                    yield outgoing

        visible_keys: set[tuple] = set()

        def unique_visible_candidates():
            for path in candidates():
                key = tuple(trim(path))
                if key in visible_keys:
                    continue
                visible_keys.add(key)
                yield path

        picked = nsmallest(
            max_paths,
            unique_visible_candidates(),
            key=lambda path: (-len(path), path),
        )
        picked = [
            path
            for path in picked
            if not any(
                _is_contiguous_subseq(path, other)
                for other in picked
                if len(other) > len(path)
            )
        ]

        visible: list[list[tuple]] = []
        seen_visible: set[tuple] = set()
        for path in (trim(path) for path in picked):
            key = tuple(path)
            if key not in seen_visible:
                seen_visible.add(key)
                visible.append(path)
        return [[rep[q] for q in path] for path in visible], len(visible_keys)


def _is_contiguous_subseq(small: list, big: list) -> bool:
    """True when small appears inside big as a contiguous run."""
    n = len(small)
    return any(big[i : i + n] == small for i in range(len(big) - n + 1))


def _within(entity: Entity, ancestor: Entity) -> bool:
    """True when entity lies inside ancestor (same file, by parent chain)."""
    return any(a is ancestor for a in _ancestors(entity))


def _ancestors(entity: Entity):
    cur = entity.parent
    while cur is not None:
        yield cur
        cur = cur.parent
