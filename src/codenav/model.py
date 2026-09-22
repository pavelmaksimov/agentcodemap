"""Data model: language tables, entities, parsed-file reference records.

Line numbers are 1-indexed, end_line inclusive.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class QualifiedRef:
    """One recorded member access, split at the attribute boundary.

    ``receiver`` keeps the full object text (e.g. ``client`` or
    ``Services().repo``); ``member`` is the accessed attribute name.  Impact
    resolution matches on the parts instead of re-splitting dotted text.
    """

    receiver: str
    member: str

    @property
    def receiver_last(self) -> str:
        """Last dotted segment of the receiver, used for type-name matching."""
        return self.receiver.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class ModuleBinding:
    """One module name bound by a python import in a file's scope.

    ``local`` is the name bound in the importing namespace: the alias for
    ``import a.b.c as x`` / ``from a.b import c as x``, the plain name for
    ``from a.b import c``, or the first segment for an alias-less
    ``import a.b.c`` (which binds only the top package).  ``path`` is the
    dotted module path the binding may denote: the full path for import
    statements, or ``base + (name,)`` for from-imports — ``c`` may be a
    submodule or an attribute of ``a.b``, so a binding resolves only when
    the path points at an indexed module.  ``rel_level`` counts leading dots
    of relative from-imports (0 for absolute; ``from . import x`` is 1).
    """

    local: str
    path: tuple[str, ...]
    rel_level: int = 0


@dataclass(frozen=True)
class ReferenceObs:
    """One reference site: how it uses the name, and where it is.

    ``kind`` is the syntactic role of the site (see ``REF_KINDS``); ``line``
    is the 1-indexed line of the referencing node in the referencing file.
    """

    kind: str
    line: int


# Reference roles recorded per observation:
#   call        — callee of a call expression (stacks with return: `return f()`)
#   inheritance — inside a class definition's base list
#   param       — type annotation outside a return position: parameter, class
#                 field, local declaration (incl. quoted forward refs)
#   return      — producer position: return-type annotation (`-> T`) or a name
#                 directly returned (`return x`, `return build()`)
#   string      — word token of a DI-position string (text-only candidate)
#   reference   — any other read/use
REF_KINDS = ("call", "inheritance", "param", "reference", "return", "string")

# Kinds are stored in full and that is what --kind takes, but every report
# prints the short label: an agent reads these on each query, so the long
# names are pure context cost.
KIND_LABELS = {
    "call": "call",
    "inheritance": "inh",
    "param": "par",
    "reference": "ref",
    "return": "ret",
    "string": "str",
}
KIND_ALIASES = {label: kind for kind, label in KIND_LABELS.items()}
# What --kind accepts: full names plus their short labels.
KIND_CHOICES = tuple(dict.fromkeys(REF_KINDS + tuple(KIND_LABELS.values())))


def kind_label(kind: str) -> str:
    """Short display label of one kind; unknown kinds pass through."""
    return KIND_LABELS.get(kind, kind)


def kind_labels(kinds: Sequence[str]) -> str:
    """Comma-joined short labels of ``kinds``, sorted and de-duplicated."""
    return ",".join(kind_label(kind) for kind in sorted(set(kinds)))


def resolve_kinds(kinds: Sequence[str] | None) -> list[str] | None:
    """Full kind names for user input: a short label (``par``) maps back."""
    if not kinds:
        return None
    return [KIND_ALIASES.get(kind, kind) for kind in kinds]


@dataclass
class Relation:
    """One resolved relation of a report target: the other symbol and why.

    ``observations`` are the reference sites that produced the relation: the
    target's own subtree for ``depends_on``, the related entity itself for
    ``dependents`` (so their lines live in that entity's file).
    """

    entity: Entity
    observations: tuple[ReferenceObs, ...] = ()

    @property
    def kinds(self) -> tuple[str, ...]:
        """Distinct kinds of the relation, sorted."""
        return tuple(sorted({o.kind for o in self.observations}))


def filter_relations(
    relations: list[Relation], kinds: Sequence[str] | None
) -> list[Relation]:
    """Keep relations with any site of ``kinds``, trimmed to those sites.

    ``None``/empty keeps every relation untouched. Trimming observations keeps
    the reported kinds and lines consistent with the filter.
    """
    if not kinds:
        return relations
    wanted = frozenset(kinds)
    return [
        Relation(relation.entity, tuple(o for o in relation.observations if o.kind in wanted))
        for relation in relations
        if any(o.kind in wanted for o in relation.observations)
    ]


def merge_ref_owners(
    dst: dict[Entity, set[ReferenceObs]], src: dict[Entity, set[ReferenceObs]]
) -> None:
    """Union per-owner observation sites of one reference into ``dst``."""
    for owner, observations in src.items():
        dst.setdefault(owner, set()).update(observations)


@dataclass
class ParsedFile:
    """Parsed source file: entities, imports, reference records."""

    path: str
    content_lines: list[str]
    entities: list[Entity] = field(default_factory=list)
    imports: list[Slice] = field(default_factory=list)
    # python module names bound by top-level imports (see ModuleBinding)
    module_bindings: list[ModuleBinding] = field(default_factory=list)
    # bare identifier references, excluding identifiers inside member access:
    # name -> owner -> reference sites
    bare_refs: dict[str, dict[Entity, set[ReferenceObs]]] = field(default_factory=dict)
    # word tokens of DI-position strings (per-language policy, see parse.py)
    string_refs: dict[str, dict[Entity, set[ReferenceObs]]] = field(default_factory=dict)
    # member access (receiver, member) -> owner -> reference sites
    qualified_refs: dict[QualifiedRef, dict[Entity, set[ReferenceObs]]] = field(
        default_factory=dict
    )
    # qualified declaration/variable name -> declared or inferred type
    declared_types: dict[str, str] = field(default_factory=dict)
    # tree-sitter reported ERROR/MISSING nodes (see parse.py): the grammar did
    # not read the whole file, so a symbol missing from the index may be an
    # extraction gap rather than an absent definition.  `codenav doctor`
    # reports the files carrying this flag.
    syntax_errors: bool = False

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


def is_dunder(name: str) -> bool:
    """True for dunder names: magic methods (`__init__`) and metadata (`__all__`)."""
    return name.startswith("__") and name.endswith("__")


def parse_int_spec(spec: str) -> set[int]:
    """'10,15-20' -> {10, 15..20} (diff --lines / outline --pages grammar)."""
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out
