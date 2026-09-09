"""Repo index: symbol lookup, type-aware impact resolution, influence paths.

Impact resolution prefers qualified member references and declared receiver
types; unqualified bare names are used only when the symbol name is unique.
Reference observations come from ``ParsedFile`` (see parse.py); the index
aggregates them repo-wide and answers impact/path queries over definition
sites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from heapq import nsmallest

from codenav.model import Entity, ParsedFile, QualifiedRef, detect_language
from codenav.parse import parse_file

# Symbols never shown as graph/impact nodes: ubiquitous infra names add noise.
GRAPH_EXCLUDED_SYMBOLS = frozenset({"logger"})


@dataclass
class ImpactReport:
    """Impact chain for a symbol: what it uses and what uses it."""

    target: Entity
    depends_on: list[Entity]
    dependents: list[Entity]


def _ancestors(entity: Entity):
    cur = entity.parent
    while cur is not None:
        yield cur
        cur = cur.parent


def _containing_class_name(entity: Entity) -> str | None:
    """Name of the nearest class ancestor, if any."""
    for ancestor in _ancestors(entity):
        if ancestor.kind == "class":
            return ancestor.name
    return None


def _in_class_named(owner: Entity, class_name: str) -> bool:
    """True when any class ancestor of owner is named class_name."""
    return any(
        ancestor.kind == "class" and ancestor.name == class_name for ancestor in _ancestors(owner)
    )


def _parent_qualified(entity: Entity) -> str:
    """Qualified name of entity's container ('' for module-level symbols)."""
    parts: list[str] = []
    cur = entity.parent
    while cur is not None and cur.name != "<unknown>":
        parts.append(cur.name)
        cur = cur.parent
    return ".".join(reversed(parts))


def _within(entity: Entity, ancestor: Entity) -> bool:
    """True when entity lies inside ancestor (same file, by parent chain)."""
    return any(a is ancestor for a in _ancestors(entity))


def _is_contiguous_subseq(small: list, big: list) -> bool:
    """True when small appears inside big as a contiguous run."""
    n = len(small)
    return any(big[i : i + n] == small for i in range(len(big) - n + 1))


class RepoIndex:
    """Index over a directory tree for symbol lookup and impact analysis."""

    # ponytail: size cap skips generated/minified bundles; per-language ignore files if needed
    MAX_FILE_BYTES = 512 * 1024
    SKIP_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache"})

    def __init__(self, roots: str | list[str], languages: list[str] | None = None) -> None:
        """Index the union of one or more root directories.

        Roots are walked independently, so sibling directories on the same
        level stay out unless listed. Each file remembers the root it was
        walked from; ``relpath_of``/``display_path`` use that root so paths
        stay root-relative even when several roots share relative names.
        """
        if isinstance(roots, str):
            roots = [roots]
        # keep realpath-distinct roots; a root fully inside an already-kept
        # one is redundant (its files are walked by the broader root)
        canonical: list[tuple[str, str]] = []  # (as given, realpath)
        for r in roots:
            rp = os.path.realpath(r)
            if any(rp == c or rp.startswith(c + os.sep) for _, c in canonical):
                continue
            canonical = [
                (g, c)
                for (g, c) in canonical
                if not (c == rp or c.startswith(rp + os.sep))
            ]
            canonical.append((r, rp))
        self.roots = [given for given, _ in canonical]
        self.files: dict[str, ParsedFile] = {}
        # walked file path -> root directory it was discovered under
        self._file_root: dict[str, str] = {}
        for root in self.roots:
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
                        self._file_root[full] = root
        self._by_name: dict[str, list[Entity]] = {}
        # definition site (file, qualified name, line) -> entity, built once
        self._entity_by_key: dict[tuple, Entity] = {}
        self._attribute_types: dict[str, set[str]] = {}
        self._bare_ref_owners: dict[str, set[Entity]] = {}
        self._string_ref_owners: dict[str, set[Entity]] = {}
        # member name -> {reference: owners}; receiver last segment -> {reference: owners}
        self._qualified_ref_owners_by_member: dict[str, dict[QualifiedRef, set[Entity]]] = {}
        self._qualified_ref_owners_by_receiver: dict[str, dict[QualifiedRef, set[Entity]]] = {}
        for pf in self.files.values():
            for e in pf.entities:
                self._entity_by_key.setdefault(self._entity_key(e), e)
                if e.name != "<unknown>":
                    self._by_name.setdefault(e.name, []).append(e)
            for qualified, type_name in pf.declared_types.items():
                attribute = qualified.rsplit(".", 1)[-1]
                self._attribute_types.setdefault(attribute, set()).add(type_name)
            for name, owners in pf.bare_refs.items():
                self._bare_ref_owners.setdefault(name, set()).update(owners)
            for name, owners in pf.string_refs.items():
                self._string_ref_owners.setdefault(name, set()).update(owners)
            for reference, owners in pf.qualified_refs.items():
                self._qualified_ref_owners_by_member.setdefault(
                    reference.member, {}
                ).setdefault(reference, set()).update(owners)
                self._qualified_ref_owners_by_receiver.setdefault(
                    reference.receiver_last, {}
                ).setdefault(reference, set()).update(owners)

    def _root_of(self, file: str) -> str:
        return self._file_root.get(file) or (self.roots[0] if self.roots else ".")

    @staticmethod
    def _root_label(root: str) -> str:
        """Short display label for a root (its basename)."""
        return os.path.basename(os.path.normpath(os.path.abspath(root)))

    def relpath_of(self, file: str) -> str:
        """Path of an indexed file relative to the root it was walked from."""
        return os.path.relpath(file, self._root_of(file))

    def display_path(self, file: str) -> str:
        """Root label + root-relative path (impact --detailed convention)."""
        root = self._root_of(file)
        return os.path.join(self._root_label(root), self.relpath_of(file))

    def agent_path(self, file: str) -> str:
        """Location path for agent records.

        With a single root this matches the historical contract (path relative
        to the root). With several roots the root label is prefixed so equal
        relative names from different roots keep distinct entity ids.
        """
        relative = self.relpath_of(file)
        if len(self.roots) <= 1:
            return relative
        root = self._root_of(file)
        return os.path.join(self._root_label(root), relative)

    def find_symbol(self, name: str) -> list[Entity]:
        out: list[Entity] = []
        for pf in self.files.values():
            out.extend(pf.find_symbol(name))
        exact = [e for e in out if e.name == name]
        return exact or out

    def name_candidates(self, name: str) -> list[Entity]:
        """All definitions named `name` across the repo (empty when none)."""
        return list(self._by_name.get(name, ()))

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

        def add_dependent(owner: Entity) -> None:
            if owner.name in GRAPH_EXCLUDED_SYMBOLS or owner is target or any(
                a.file == target.file and a.contains(target.start_line)
                for a in _ancestors(owner)
            ):
                return
            key = (owner.file, owner.qualified_name, owner.start_line)
            if key not in dep_keys:
                dep_keys.add(key)
                deps.append(owner)

        for owner in self._owners_referencing(target):
            add_dependent(owner)
        # DI registrations ("pkg.mod:Symbol") and forward annotations also
        # reference the target; their owners are class/module entities
        for owner in self._string_ref_owners.get(target.name, ()):
            add_dependent(owner)
        # refs are attributed to the innermost owner (a method, not its class),
        # so "what the target uses" aggregates over the whole subtree
        members = {id(e) for e in pf.entities if e is target or _within(e, target)}
        depends_on: list[Entity] = []
        seen: set[tuple] = set()

        def add_dep_candidates(used: str, owners: set[Entity]) -> None:
            if used in GRAPH_EXCLUDED_SYMBOLS or not any(id(o) in members for o in owners):
                return
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

        for used, owners in pf.bare_refs.items():
            add_dep_candidates(used, owners)
        # DI-position strings (LazyService("pkg.mod:Symbol"), "Symbol" annotations)
        for used, owners in pf.string_refs.items():
            add_dep_candidates(used, owners)
        for candidate in self._qualified_dependencies(pf, members):
            if candidate is target or candidate.qualified_name == target.qualified_name:
                continue
            key = (candidate.file, candidate.qualified_name, candidate.start_line)
            if key not in seen:
                seen.add(key)
                depends_on.append(candidate)
        return ImpactReport(target=target, depends_on=depends_on, dependents=deps)

    def _owners_referencing(self, target: Entity) -> set[Entity]:
        """Entities whose member access text can resolve to target."""
        owners: set[Entity] = set()
        qualified = dict(self._qualified_ref_owners_by_member.get(target.name, {}))
        # a class/type used as a receiver (e.g. ``ChatMessage.session``) is a
        # dependent of the class itself, whatever member is accessed
        if target.kind in ("class", "type"):
            for reference, references in self._qualified_ref_owners_by_receiver.get(
                target.name, {}
            ).items():
                qualified.setdefault(reference, set()).update(references)
        for reference, references in qualified.items():
            if target.kind in ("class", "type") and reference.receiver_last == target.name:
                owners.update(references)
            elif self._reference_matches(target, reference, references):
                owners.update(references)
        if target.kind != "attr" and len(self._by_name.get(target.name, [])) == 1:
            owners.update(self._bare_ref_owners.get(target.name, set()))
        return owners

    def _reference_matches(
        self, target: Entity, reference: QualifiedRef, owners: set[Entity]
    ) -> bool:
        """True when reference (member == target.name) can denote target."""
        if reference.receiver == _parent_qualified(target):
            return True
        target_class = _containing_class_name(target)
        if target_class is None:
            return False
        if reference.receiver == target_class:
            return True
        if reference.receiver == "self":
            return any(_in_class_named(owner, target_class) for owner in owners)
        return target_class in self._attribute_types.get(reference.receiver_last, set())

    def _qualified_dependency_used(
        self, candidate: Entity, parsed: ParsedFile, members: set[int]
    ) -> bool:
        for reference, owners in parsed.qualified_refs.items():
            if not any(id(owner) in members for owner in owners):
                continue
            if (
                reference.member == candidate.name
                and reference.receiver == _parent_qualified(candidate)
            ):
                return True
            if reference.receiver != "self" or reference.member != candidate.name:
                continue
            candidate_class = _containing_class_name(candidate)
            if candidate_class and any(
                _in_class_named(owner, candidate_class) for owner in owners
            ):
                return True
        return False

    def _qualified_dependencies(self, parsed: ParsedFile, members: set[int]) -> set[Entity]:
        result: set[Entity] = set()
        for reference, owners in parsed.qualified_refs.items():
            if not any(id(owner) in members for owner in owners):
                continue
            for candidate in self._by_name.get(reference.member, []):
                if (
                    reference.receiver == _parent_qualified(candidate)
                    or self._qualified_dependency_used(candidate, parsed, members)
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

    def _side_neighbors(
        self, reports: dict[tuple, ImpactReport], node: tuple, relation: str
    ) -> list[tuple]:
        """Definition keys node depends on ('down') or that depend on it ('up')."""
        if node not in reports:
            reports[node] = self.impact_entity(self._entity_by_key[node])
        report = reports[node]
        entities = report.depends_on if relation == "down" else report.dependents
        return sorted(
            (self._entity_key(entity) for entity in entities),
            key=lambda key: (self._entity_by_key[key].kind, key),
        )

    def _side_paths(
        self,
        reports: dict[tuple, ImpactReport],
        start: tuple,
        relation: str,
        max_enum: int = 2000,
        max_fanout: int = 20,
    ) -> list[list[tuple]]:
        """Maximal depth-first walks from start along one relation (node keys).

        ponytail: hard caps keep hub symbols (Container, logger) tractable;
        raise MAX_ENUM/MAX_FANOUT if deeper exploration is ever needed
        """
        result: list[list[tuple]] = []
        stack: list[list[tuple]] = [[start]]
        enumerated = 0
        while stack and enumerated < max_enum:
            p = stack.pop()
            enumerated += 1
            next_nodes = [
                nb
                for nb in self._side_neighbors(reports, p[-1], relation)[:max_fanout]
                if nb not in p
            ]
            if not next_nodes or enumerated == max_enum:
                result.append(p)
            else:
                stack.extend(p + [nb] for nb in next_nodes)
        return result

    def influence_paths_entity_with_total(
        self, target: Entity, max_nodes: int = 5, max_paths: int = 100
    ) -> tuple[list[list[Entity]], int]:
        """Influence paths for an exact definition, without name re-resolution.

        The returned total counts every distinct visible path enumerated before
        the max_paths cap (exploration is itself bounded by the hard caps in
        simple_paths), so it is the denominator the CLI reports truncation
        against.
        """
        if max_nodes < 1 or max_paths < 1:
            return [], 0

        tq = self._entity_key(target)
        if tq not in self._entity_by_key:
            return [], 0
        reports: dict[tuple, ImpactReport] = {}
        down = self._side_paths(reports, tq, "down")
        up = self._side_paths(reports, tq, "up")
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
        return [[self._entity_by_key[q] for q in path] for path in visible], len(visible_keys)

    def direction_paths_entity_with_total(
        self,
        target: Entity,
        relation: str,
        max_nodes: int = 5,
        max_paths: int = 100,
    ) -> tuple[list[list[Entity]], int]:
        """Single-direction chains from an exact definition, no name re-resolution.

        Relation 'down' walks depends_on (what the target pulls in), 'up'
        walks dependents (what reaches the target); every chain starts at the
        target. With 'down' the chains read like the graph arrow convention
        (A -> B means A references B); with 'up' reverse a chain to get that
        order. max_nodes truncates each chain from the target end, so the
        whole budget goes into one direction. The total counts every distinct
        truncated chain enumerated before max_paths picked the shown ones,
        mirroring influence_paths_entity_with_total. Isolated nodes return an
        empty list with a zero total.
        """
        if max_nodes < 1 or max_paths < 1:
            return [], 0

        tq = self._entity_key(target)
        if tq not in self._entity_by_key:
            return [], 0
        reports: dict[tuple, ImpactReport] = {}
        paths = self._side_paths(reports, tq, relation)
        if paths == [[tq]]:
            return [], 0

        def trim(path: list[tuple]) -> list[tuple]:
            return path[:max_nodes]

        visible_keys: set[tuple] = set()

        def candidates():
            for path in paths:
                key = tuple(trim(path))
                if key in visible_keys:
                    continue
                visible_keys.add(key)
                yield path

        picked = nsmallest(
            max_paths,
            candidates(),
            key=lambda path: (-len(path), path),
        )
        visible: list[list[tuple]] = []
        seen_visible: set[tuple] = set()
        for path in (trim(path) for path in picked):
            key = tuple(path)
            if key not in seen_visible:
                seen_visible.add(key)
                visible.append(path)
        return [[self._entity_by_key[q] for q in path] for path in visible], len(visible_keys)
