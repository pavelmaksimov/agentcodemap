"""Tree-sitter parsing: entity extraction and reference collection.

Reference collection splits observations into channels (bare identifiers,
DI-position strings, qualified member access, declared types) attributed to
the innermost containing entity.  Each observation also records the syntactic
role of its site (call, param, return, inheritance, string, reference) and its
line, so impact/trace output can explain why a relation exists
(``model.ReferenceObs``).  Which string literals count as DI positions
is a per-language policy (``DI_STRING_POLICY``); languages without an entry
collect only identifier refs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser

from codenav.model import (
    ASSIGN_LIKE_TYPES,
    CLASS_LIKE_TYPES,
    ENTITY_NODE_TYPES,
    IMPORT_NODE_TYPES,
    LANGUAGES,
    NESTED_ENTITY_PARENT_TYPES,
    Entity,
    ModuleBinding,
    ParsedFile,
    QualifiedRef,
    ReferenceObs,
    Slice,
)

logger = logging.getLogger(__name__)

_parsers: dict[str, Parser] = {}

# word tokens of DI-position string contents (LazyService paths, forward annotations)
WORD_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


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


def _node_text(content: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return content[node.start_byte : node.end_byte].decode()


def _simple_type_name(text: str) -> str:
    text = text.strip().strip("\"'")
    return text.split("[", 1)[0].rsplit(".", 1)[-1]


# Node types whose callee sits in a named field; grammars name that field
# differently (python/js/go/rust ``function``, java ``name``, ruby ``method``).
CALL_NODE_TYPES = frozenset(
    {
        "call",
        "call_expression",
        "method_invocation",
        "invocation_expression",
        "object_creation_expression",
        "macro_invocation",
    }
)
CALLEE_FIELDS = ("function", "name", "method", "constructor")

ANNOTATION_NODE_TYPES = frozenset({"type", "type_annotation"})
# Ancestors that end a type-annotation search: an annotation never crosses
# into the enclosing callable's body.
ANNOTATION_SCOPE_STOPS = frozenset(
    {"function_definition", "class_definition", "module", "block", "lambda"}
)

# Direct return position: the returned expression itself (`return x`,
# `return build()`).  ``RETURN_WRAPPERS`` are transparent in-between nodes
# (python spells await just "await").  ``new_expression`` joins the callee
# step so `return new Foo()` marks a producer without changing which
# references count as calls.
RETURN_NODE_TYPES = frozenset({"return_statement", "return_expression", "return"})
RETURN_WRAPPERS = frozenset({"parenthesized_expression", "await", "await_expression"})
RETURN_CTOR_NODE_TYPES = CALL_NODE_TYPES | {"new_expression"}


def _in_class_bases(node: Node) -> bool:
    """True for a name in a class definition's base list (``class A(B)``)."""
    cur = node.parent
    for _ in range(4):
        if cur is None:
            return False
        if cur.type == "argument_list":
            return cur.parent is not None and cur.parent.type == "class_definition"
        cur = cur.parent
    return False


def _annotation_kind(node: Node) -> str | None:
    """``param``/``return`` for a name inside a type annotation, else None.

    The annotation hanging off a callable as its ``return_type`` field
    (`-> T`) is a producer position; every other annotated position —
    parameter, class field, local declaration — reads ``param``.
    """
    cur = node.parent
    while cur is not None and cur.type not in ANNOTATION_SCOPE_STOPS:
        if cur.type in ANNOTATION_NODE_TYPES:
            parent = cur.parent
            if parent is not None and parent.child_by_field_name("return_type") == cur:
                return "return"
            return "param"
        cur = cur.parent
    return None


def _returned(node: Node) -> bool:
    """True when node is (part of) the expression a return statement returns.

    `return x` and `return build()` mark the name; `return x + y` does not —
    the sum, not the operand, is what comes back.  The callee step lets the
    constructed/called value itself count as the returned data.
    """
    parent = node.parent
    if (
        parent is not None
        and parent.type in RETURN_CTOR_NODE_TYPES
        and any(parent.child_by_field_name(field) == node for field in CALLEE_FIELDS)
    ):
        parent = parent.parent
    while parent is not None and parent.type in RETURN_WRAPPERS:
        parent = parent.parent
    return parent is not None and parent.type in RETURN_NODE_TYPES


def _ref_kind(node: Node) -> str:
    """Syntactic role of a reference node (see ``model.REF_KINDS``).

    Node comparison uses ``==``, not ``is``: tree-sitter returns fresh Python
    wrappers for the same tree node on each access.
    """
    parent = node.parent
    if (
        parent is not None
        and parent.type in CALL_NODE_TYPES
        and any(parent.child_by_field_name(field) == node for field in CALLEE_FIELDS)
    ):
        return "call"
    if _in_class_bases(node):
        return "inheritance"
    annotation = _annotation_kind(node)
    if annotation is not None:
        return annotation
    # a specific role supersedes the fallback: the returned name is 'return'
    if _returned(node):
        return "return"
    return "reference"


def _sites(node: Node) -> set[ReferenceObs]:
    """Reference sites of a name node: kind(s) plus the 1-indexed line.

    A directly returned call stacks 'return' on top of 'call':
    `return build()` both invokes and produces.
    """
    line = node.start_point[0] + 1
    kind = _ref_kind(node)
    sites = {ReferenceObs(kind, line)}
    if kind == "call" and _returned(node):
        sites.add(ReferenceObs("return", line))
    return sites


def _call_target_name(content: bytes, right: Node | None) -> str:
    """Identifier of the callable an assignment RHS invokes (e.g. ``Service()``).

    Field shapes differ per language grammar; only python-style ``call`` nodes
    carry a ``function`` identifier, so receiver-type inference stays
    python-first by design.
    """
    if right is None:
        return ""
    function = right.child_by_field_name("function")
    if function is not None and function.type == "identifier":
        return _node_text(content, function)
    return ""


def _python_di_string(node: Node, owner: Entity, content: bytes) -> bool:
    """True when node is a python string in a dependency-injection position.

    Such strings become real relations: type annotations (``"Symbol"``) and
    strings inside the RHS of class/module-level assignments
    (``LazyService("pkg.mod:Symbol")``).  Docstrings and arbitrary
    method-local string contents do not.
    """
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "type":
        return True
    if owner.kind not in ("class", "attr"):
        return False
    cur = parent
    while cur is not None:
        if cur.type in ("function_definition", "class_definition"):
            return False
        if cur.type in ASSIGN_LIKE_TYPES:
            right = cur.child_by_field_name("right")
            return right is not None and (
                right.start_byte <= node.start_byte and node.end_byte <= right.end_byte
            )
        cur = cur.parent
    return False


# Which string literals contribute word references, per tree-sitter language.
# Python: annotations and class/module-level attribute values (DI wiring).
# Other languages: bare identifier refs only.
DI_STRING_POLICY: dict[str, Callable[[Node, Entity, bytes], bool]] = {
    "python": _python_di_string,
}


def parse_file(
    path: str, content: str, language: str, collect_refs: bool = True
) -> ParsedFile | None:
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
    di_string = DI_STRING_POLICY.get(ts_language)

    parsed = ParsedFile(path=path, content_lines=content_lines)

    def first_declarator_name(node: Node) -> str:
        for child in node.children:
            if child.type == "variable_declarator":
                return _node_text(content_bytes, child.child_by_field_name("name"))
        return ""

    def make_entity(node: Node, parent: Entity | None) -> tuple[Entity, Node | None]:
        """Build an Entity from an entity-typed node. Returns (entity, name_node)."""
        name_node = node.child_by_field_name("name")
        name_text = _node_text(content_bytes, name_node)

        if node.type in ASSIGN_LIKE_TYPES:
            kind = "attr"
            left = node.child_by_field_name("left") or node.child_by_field_name("pattern")
            name_text = _node_text(content_bytes, left) or name_text or first_declarator_name(node)
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
                        name_text = _node_text(content_bytes, child)
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
    def python_import_bindings(node: Node) -> list[ModuleBinding]:
        """Module names bound by a python import statement.

        import a.b.c            -> binds 'a' (top package; full-chain
                                   receivers a.b.c match by module path)
        import a.b.c as x       -> binds 'x' (path a.b.c)
        from a.b import c [as x] -> binds c/x (path a.b.c.c); 'c' may be a
        submodule of a.b or an attribute of it — resolution keeps the binding
        only when the path points at an indexed module.
        Relative from-imports keep their leading-dot count in rel_level; the
        base package is resolved later from the importing file's own package.
        """
        if ts_language != "python":
            return []
        if node.type == "import_statement":
            bindings = []
            for child in node.children:
                if child.type == "aliased_import":
                    alias = child.child_by_field_name("alias")
                    name = child.child_by_field_name("name")
                    if alias is not None and name is not None:
                        segs = tuple(_node_text(content_bytes, name).split("."))
                        bindings.append(ModuleBinding(_node_text(content_bytes, alias), segs))
                elif child.type == "dotted_name":
                    segs = tuple(_node_text(content_bytes, child).split("."))
                    if segs:
                        # `import a.b.c` binds only the top-level package name
                        bindings.append(ModuleBinding(segs[0], segs[:1]))
            return bindings
        if node.type == "import_from_statement":
            # children: 'from', base (dotted_name | relative_import),
            # 'import', then the imported names (dotted_name | aliased_import)
            base: tuple[str, ...] = ()
            rel_level = 0
            names: list[Node] = []
            after_import = False
            for child in node.children:
                if child.type == "import":
                    after_import = True
                    continue
                if not after_import:
                    if child.type == "dotted_name":
                        base = tuple(_node_text(content_bytes, child).split("."))
                    elif child.type == "relative_import":
                        for part in child.children:
                            if part.type == "import_prefix":
                                rel_level = len(_node_text(content_bytes, part))
                            elif part.type == "dotted_name":
                                base = tuple(_node_text(content_bytes, part).split("."))
                else:
                    names.append(child)
            bindings = []
            for child in names:
                if child.type == "aliased_import":
                    alias = child.child_by_field_name("alias")
                    name = child.child_by_field_name("name")
                    if alias is not None and name is not None:
                        name_segs = tuple(_node_text(content_bytes, name).split("."))
                        bindings.append(
                            ModuleBinding(
                                _node_text(content_bytes, alias),
                                base + name_segs,
                                rel_level,
                            )
                        )
                elif child.type == "dotted_name":
                    name_segs = tuple(_node_text(content_bytes, child).split("."))
                    bindings.append(ModuleBinding(name_segs[0], base + name_segs, rel_level))
            return bindings
        return []

    def walk(node: Node, parent_entity: Entity | None, top_level: bool) -> None:
        if node.type in import_types and top_level:
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            parsed.imports.append(
                Slice(start, end, "\n".join(content_lines[start - 1 : end]), "import", "<import>")
            )
            parsed.module_bindings.extend(python_import_bindings(node))
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
                # receiver typing: `x: Service` / `x = Service()`; the shape is
                # python's (fields on the assignment node itself), other
                # grammars carry these fields on declarator children instead
                left = node.child_by_field_name("left") or node.child_by_field_name("pattern")
                left_name = _node_text(content_bytes, left)
                type_node = node.child_by_field_name("type")
                type_name = (
                    _simple_type_name(_node_text(content_bytes, type_node))
                    if type_node is not None
                    else ""
                )
                if not type_name:
                    type_name = _call_target_name(content_bytes, node.child_by_field_name("right"))
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

    # pass 2: collect refs attributed to innermost containing entity; definition
    # name nodes are excluded.  covering_by_line (swept in the collect_refs
    # branch below) holds each line's covering entities in declaration order,
    # so owner lookups are O(covering) instead of a scan of every entity.
    def find_owner(node: Node) -> Entity | None:
        covering = covering_by_line[node.start_point[0] + 1]
        if not covering:
            return None
        callables = [e for e in covering if e.kind in ("method", "function", "class")]
        return min(callables or covering, key=lambda e: e.end_line - e.start_line)

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
                reference = QualifiedRef(
                    receiver=_node_text(content_bytes, object_node),
                    member=_node_text(content_bytes, attribute_node),
                )
                parsed.qualified_refs.setdefault(reference, {}).setdefault(owner, set()).update(
                    _sites(node)
                )
            inside_attribute = True
        elif node.type == "identifier" and (node.start_byte, node.end_byte) not in skip:
            if not inside_attribute:
                name = _node_text(content_bytes, node)
                parsed.bare_refs.setdefault(name, {}).setdefault(owner, set()).update(
                    _sites(node)
                )
        elif node.type == "string" and owner is not None and di_string is not None:
            text = _node_text(content_bytes, node)
            if len(text) <= 500 and di_string(node, owner, content_bytes):
                # A quoted forward reference takes its annotation position's
                # kind (param/return); a DI wiring value ("pkg.mod:Symbol"
                # inside LazyService) has no syntactic role and stays 'string'.
                kind = _ref_kind(node)
                site = ReferenceObs(
                    "string" if kind == "reference" else kind, node.start_point[0] + 1
                )
                for token in WORD_RX.findall(text):
                    parsed.string_refs.setdefault(token, {}).setdefault(owner, set()).add(site)
        for child in node.children:
            walk_refs(child, find_owner(child), skip, inside_attribute)

    if collect_refs:
        # entities are declared in source order (start lines non-decreasing), so
        # one sweep gives every line its covering set in declaration order
        starts: dict[int, list[Entity]] = {}
        ends: dict[int, list[Entity]] = {}
        for e in parsed.entities:
            starts.setdefault(e.start_line, []).append(e)
            ends.setdefault(e.end_line + 1, []).append(e)
        covering_by_line: list[list[Entity]] = [
            [] for _ in range(len(content_lines) + 1)
        ]
        active: list[Entity] = []
        for ln in range(1, len(content_lines) + 1):
            for e in ends.get(ln, ()):
                active.remove(e)
            for e in starts.get(ln, ()):
                active.append(e)
            covering_by_line[ln] = list(active)

        skip: set[tuple[int, int]] = set()
        gather_def_positions(tree.root_node, skip)
        walk_refs(tree.root_node, find_owner(tree.root_node), skip)

    return parsed
