"""Repo index: symbol lookup, type-aware impact resolution, influence paths.

Impact resolution prefers qualified member references and declared receiver
types; unqualified bare names are used only when the symbol name is unique.
Reference observations come from ``ParsedFile`` (see parse.py); the index
aggregates them repo-wide and answers impact/path queries over definition
sites.  Each resolved relation keeps the reference sites (kind plus line)
that produced it, so impact/trace output can name the relation type (call,
param, return, inheritance, string, reference) instead of only the symbol;
``relation_observations`` labels one chain edge that way.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from heapq import nsmallest

from codenav.model import (
    Entity,
    ModuleBinding,
    ParsedFile,
    QualifiedRef,
    ReferenceObs,
    Relation,
    detect_language,
    filter_relations,
    is_dunder,
    merge_ref_owners,
    read_file,
)
from codenav.parse import parse_file

# Symbols never shown as influence/impact nodes: ubiquitous infra names add noise.
GRAPH_EXCLUDED_SYMBOLS = frozenset({"logger"})


def _graph_excluded(entity: Entity) -> bool:
    """True for symbols that carry no meaning as influence/impact nodes.

    Beyond the name blacklist, attribute/constant assignments named as
    dunders (``__all__``, ``__version__``, ``__slots__``) declare exports or
    metadata, not dependencies: their string/name bodies would otherwise wire
    the metadata node to every declared symbol. Callable and class dunders
    (``__init__``, ``__call__``) are real code and stay in relations.
    """
    return entity.name in GRAPH_EXCLUDED_SYMBOLS or (
        entity.kind in ("attr", "constant") and is_dunder(entity.name)
    )


@dataclass
class ImpactReport:
    """Impact chain for a symbol: what it uses and what uses it.

    Every relation carries the reference sites (kind plus line) that produced
    it, so output can explain why a relation exists, not only that it does.
    """

    target: Entity
    depends_on: list[Relation]
    dependents: list[Relation]


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


def _sorted_obs(observations: set[ReferenceObs]) -> tuple[ReferenceObs, ...]:
    """Reference sites in a stable order (line, then kind)."""
    return tuple(sorted(observations, key=lambda o: (o.line, o.kind)))


# Why a path the discovery walk reached is not in the index.  `codenav doctor`
# groups its report by these reasons; nothing else may pass them around.
IGNORED_DIR = "ignored dir"  # directory pruned from the walk entirely
UNSUPPORTED = "unsupported language"  # extension maps to no grammar
FILTERED = "language filtered"  # outside the caller's language allowlist
TOO_LARGE = "too large"  # above RepoIndex.MAX_FILE_BYTES
UNREADABLE = "unreadable"  # OSError, or not UTF-8 text
PARSE_FAILED = "parse failed"  # tree-sitter could not parse it


@dataclass(frozen=True)
class ScanItem:
    """One path the discovery walk reached, and what indexing did with it.

    ``skip`` is empty for an indexed file and one of the reasons above
    otherwise; ``lang`` is set for every file with a supported extension,
    ``parsed`` only when the file made it into the index.
    """

    path: str
    lang: str | None = None
    parsed: ParsedFile | None = None
    skip: str = ""


class RepoIndex:
    """Index over a directory tree for symbol lookup and impact analysis."""

    # ponytail: size cap skips generated/minified bundles; per-language ignore files if needed
    MAX_FILE_BYTES = 512 * 1024
    SKIP_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache"})

    @staticmethod
    def canonical_roots(roots: Sequence[str]) -> list[str]:
        """Roots in as-given spelling, duplicates and nested ones dropped.

        A root fully inside an already-kept root is redundant: walking the
        broader root reaches its files anyway.  Roots are compared by
        realpath, so symlinked duplicates collapse too.
        """
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
        return [given for given, _ in canonical]

    @classmethod
    def scan_tree(
        cls, root: str, languages: Sequence[str] | None = None
    ) -> Iterator[ScanItem]:
        """Walk ``root`` the way the index does: one item per path visited.

        Applies exactly the indexing filters (pruned directories, supported
        extension, language allowlist, size cap, readable text), so a caller
        can tell which files the index kept and why it dropped the rest.
        Pruned directories are reported themselves (``skip == IGNORED_DIR``);
        the files under them are never visited.  A read error of one file is
        reported and skipped, never raised.
        """
        for dirpath, dirnames, filenames in os.walk(root):
            kept = {d for d in dirnames if d not in cls.SKIP_DIRS and not d.startswith(".")}
            for d in dirnames:
                if d not in kept:
                    yield ScanItem(os.path.join(dirpath, d), skip=IGNORED_DIR)
            dirnames[:] = sorted(kept)
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                lang = detect_language(fn)
                if lang is None:
                    yield ScanItem(full, skip=UNSUPPORTED)
                    continue
                if languages and lang not in languages:
                    yield ScanItem(full, lang=lang, skip=FILTERED)
                    continue
                try:
                    if os.path.getsize(full) > cls.MAX_FILE_BYTES:
                        yield ScanItem(full, lang=lang, skip=TOO_LARGE)
                        continue
                    content = read_file(full)
                except (OSError, UnicodeDecodeError):
                    yield ScanItem(full, lang=lang, skip=UNREADABLE)
                    continue
                parsed = parse_file(full, content, lang)
                if parsed is None:
                    yield ScanItem(full, lang=lang, skip=PARSE_FAILED)
                    continue
                yield ScanItem(full, lang=lang, parsed=parsed)

    def __init__(self, roots: str | list[str], languages: list[str] | None = None) -> None:
        """Index the union of one or more root directories.

        Indexing walks through ``scan_tree`` (shared with ``codenav doctor``),
        so the diagnosis describes this index instead of a second
        implementation of the same filters.

        Roots are walked independently, so sibling directories on the same
        level stay out unless listed. Each file remembers the root it was
        walked from; ``relpath_of``/``display_path`` use that root so paths
        stay root-relative even when several roots share relative names.
        """
        if isinstance(roots, str):
            roots = [roots]
        self.roots = self.canonical_roots(roots)
        self.files: dict[str, ParsedFile] = {}
        # walked file path -> root directory it was discovered under
        self._file_root: dict[str, str] = {}
        for root in self.roots:
            for item in self.scan_tree(root, languages):
                if item.parsed is None:
                    continue
                self.files[item.path] = item.parsed
                self._file_root[item.path] = root
        self._by_name: dict[str, list[Entity]] = {}
        # definition site (file, qualified name, line) -> entity, built once
        self._entity_by_key: dict[tuple, Entity] = {}
        # module path (root-relative dotted segments) -> files defining it
        self._module_files: dict[tuple[str, ...], list[str]] = {}
        # file -> [ModuleBinding] collected by the parser (python only)
        self._bindings_by_file: dict[str, list[ModuleBinding]] = {}
        for full, pf in self.files.items():
            rel = os.path.relpath(full, self._root_of(full))
            parts = rel.split(os.sep)
            stem = os.path.splitext(parts[-1])[0]
            if stem == "__init__":
                module_path: tuple[str, ...] = tuple(parts[:-1])
            else:
                module_path = tuple(parts[:-1]) + (stem,)
            self._module_files.setdefault(module_path, []).append(full)
            if pf.module_bindings:
                self._bindings_by_file[full] = list(pf.module_bindings)
        self._attribute_types: dict[str, set[str]] = {}
        # reference channel -> name -> owner -> reference sites
        self._bare_ref_owners: dict[str, dict[Entity, set[ReferenceObs]]] = {}
        self._string_ref_owners: dict[str, dict[Entity, set[ReferenceObs]]] = {}
        # member name -> {reference: owners}; receiver last segment -> {reference: owners}
        self._qualified_ref_owners_by_member: dict[
            str, dict[QualifiedRef, dict[Entity, set[ReferenceObs]]]
        ] = {}
        self._qualified_ref_owners_by_receiver: dict[
            str, dict[QualifiedRef, dict[Entity, set[ReferenceObs]]]
        ] = {}
        # entity key -> impact report; an index never changes after construction
        self._impact_cache: dict[tuple, ImpactReport] = {}
        for pf in self.files.values():
            for e in pf.entities:
                self._entity_by_key.setdefault(self._entity_key(e), e)
                if e.name != "<unknown>":
                    self._by_name.setdefault(e.name, []).append(e)
            for qualified, type_name in pf.declared_types.items():
                attribute = qualified.rsplit(".", 1)[-1]
                self._attribute_types.setdefault(attribute, set()).add(type_name)
            for name, owners in pf.bare_refs.items():
                merge_ref_owners(self._bare_ref_owners.setdefault(name, {}), owners)
            for name, owners in pf.string_refs.items():
                merge_ref_owners(self._string_ref_owners.setdefault(name, {}), owners)
            for reference, owners in pf.qualified_refs.items():
                merge_ref_owners(
                    self._qualified_ref_owners_by_member.setdefault(
                        reference.member, {}
                    ).setdefault(reference, {}),
                    owners,
                )
                merge_ref_owners(
                    self._qualified_ref_owners_by_receiver.setdefault(
                        reference.receiver_last, {}
                    ).setdefault(reference, {}),
                    owners,
                )

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

    def impact_entity(self, target: Entity, kinds: Sequence[str] | None = None) -> ImpactReport:
        """Impact for an exact definition, without resolving its name again.

        Memoized per index: chain walks and CLI edge lookups ask for the
        same nodes repeatedly, and an index never changes after construction.
        ``kinds`` returns a filtered view (relations and their sites) of the
        memoized report; the cache itself always keeps every kind.
        """
        key = self._entity_key(target)
        report = self._impact_cache.get(key)
        if report is None:
            report = self._impact_for(target)
            self._impact_cache[key] = report
        if not kinds:
            return report
        return ImpactReport(
            target=report.target,
            depends_on=filter_relations(report.depends_on, kinds),
            dependents=filter_relations(report.dependents, kinds),
        )

    def _impact_for(self, target: Entity) -> ImpactReport:
        pf = self.files.get(target.file)
        if pf is None or _graph_excluded(target):
            return ImpactReport(target=target, depends_on=[], dependents=[])

        dep_owners: dict[tuple, Entity] = {}
        dep_obs: dict[tuple, set[ReferenceObs]] = {}

        def add_dependent(owner: Entity, observations: set[ReferenceObs]) -> None:
            if _graph_excluded(owner) or owner is target or any(
                a.file == target.file and a.contains(target.start_line)
                for a in _ancestors(owner)
            ):
                return
            key = (owner.file, owner.qualified_name, owner.start_line)
            dep_owners.setdefault(key, owner)
            dep_obs.setdefault(key, set()).update(observations)

        for owner, observations in self._owners_referencing(target).items():
            add_dependent(owner, observations)
        # DI registrations ("pkg.mod:Symbol") and forward annotations also
        # reference the target; their owners are class/module entities
        for owner, observations in self._string_ref_owners.get(target.name, {}).items():
            add_dependent(owner, observations)
        # refs are attributed to the innermost owner (a method, not its class),
        # so "what the target uses" aggregates over the whole subtree
        members = {id(e) for e in pf.entities if e is target or _within(e, target)}
        return ImpactReport(
            target=target,
            depends_on=self._dependencies(pf, target, members),
            dependents=[
                Relation(entity, _sorted_obs(dep_obs[key]))
                for key, entity in dep_owners.items()
            ],
        )

    def _dependencies(
        self, pf: ParsedFile, target: Entity, members: set[int]
    ) -> list[Relation]:
        """What the references owned by ``members`` resolve to.

        ``members`` holds the ids of the entities whose reference sites count
        as the target's own use: the target plus its subtree for
        ``impact_entity``, only the target itself for ``direct_dependencies``.
        """
        if _graph_excluded(target):
            return []
        owners: dict[tuple, Entity] = {}
        observations: dict[tuple, set[ReferenceObs]] = {}

        def add_dependency(candidate: Entity, sites: set[ReferenceObs]) -> None:
            if (
                _graph_excluded(candidate)
                or candidate is target
                or candidate.qualified_name == target.qualified_name
            ):
                return
            key = (candidate.file, candidate.qualified_name, candidate.start_line)
            owners.setdefault(key, candidate)
            observations.setdefault(key, set()).update(sites)

        def add_candidates(used: str, refs: dict[Entity, set[ReferenceObs]]) -> None:
            sites = [obs for owner, obs in refs.items() if id(owner) in members]
            if not sites:
                return
            used_sites = set().union(*sites)
            candidates = self._by_name.get(used, [])
            for cand in candidates:
                if (
                    _graph_excluded(cand)
                    or cand is target
                    or cand.qualified_name == target.qualified_name
                ):
                    continue
                if len(candidates) > 1 and not self._qualified_dependency_used(cand, pf, members):
                    continue
                if cand.kind == "attr" and not self._qualified_dependency_used(cand, pf, members):
                    continue
                add_dependency(cand, used_sites)

        for used, refs in pf.bare_refs.items():
            add_candidates(used, refs)
        # DI-position strings (LazyService("pkg.mod:Symbol"), "Symbol" annotations)
        for used, refs in pf.string_refs.items():
            add_candidates(used, refs)
        for candidate, sites in self._qualified_dependencies(pf, members).items():
            add_dependency(candidate, sites)
        return [
            Relation(entity, _sorted_obs(observations[key]))
            for key, entity in owners.items()
        ]

    def direct_dependencies(self, target: Entity) -> list[Relation]:
        """What ``target``'s own body references, excluding nested members.

        ``impact_entity`` aggregates a class with its methods; the outline
        wants each symbol's own references so a class and its methods never
        repeat the same dependency.
        """
        pf = self.files.get(target.file)
        if pf is None:
            return []
        return self._dependencies(pf, target, {id(target)})

    def relation_observations(
        self, source: Entity, target: Entity, kinds: Sequence[str] | None = None
    ) -> tuple[ReferenceObs, ...]:
        """Reference sites of source's direct relation to target.

        Follows the graph arrow convention (``A -> B`` means A references B):
        the result is non-empty exactly when source references target, which
        is what one chain edge means. Empty tuple when the edge does not exist.
        ``kinds`` narrows the sites to those kinds, so a filtered chain edge is
        labelled only with the kinds the caller asked for.
        """
        key = self._entity_key(target)
        for relation in self.impact_entity(source).depends_on:
            if self._entity_key(relation.entity) == key:
                if not kinds:
                    return relation.observations
                wanted = frozenset(kinds)
                return tuple(o for o in relation.observations if o.kind in wanted)
        return ()

    def _owners_referencing(self, target: Entity) -> dict[Entity, set[ReferenceObs]]:
        """Entities whose member access text can resolve to target, with sites."""
        owners: dict[Entity, set[ReferenceObs]] = {}
        qualified = self._qualified_ref_owners_by_member.get(target.name, {})
        # a class/type used as a receiver (e.g. ``ChatMessage.session``) is a
        # dependent of the class itself, whatever member is accessed; the
        # inner maps are copied because receiver refs merge into them
        if target.kind in ("class", "type"):
            merged = {reference: dict(refs) for reference, refs in qualified.items()}
            for reference, references in self._qualified_ref_owners_by_receiver.get(
                target.name, {}
            ).items():
                merge_ref_owners(merged.setdefault(reference, {}), references)
            qualified = merged
        for reference, references in qualified.items():
            if target.kind in ("class", "type") and reference.receiver_last == target.name:
                merge_ref_owners(owners, references)
            elif self._reference_matches(target, reference, references):
                merge_ref_owners(owners, references)
        if target.kind != "attr" and len(self._by_name.get(target.name, [])) == 1:
            merge_ref_owners(owners, self._bare_ref_owners.get(target.name, {}))
        return owners

    def _suffix_module_files(self, path: tuple[str, ...]) -> list[str]:
        """Indexed files whose root-relative module path is a suffix of path.

        Longest suffix wins: ``project.components.code_review.use_cases``
        resolves ``components/code_review/use_cases.py`` when the index root
        sits inside (or at) the real import root.
        """
        if not path:
            return []
        for k in range(len(path), 0, -1):
            files = self._module_files.get(path[-k:])
            if files:
                return files
        return []

    def _relative_binding_files(self, file: str, binding: ModuleBinding) -> list[str]:
        """Files for a relative from-import, from the importer's package.

        ``from . import x`` (rel_level 1) resolves inside the directory of
        ``file`` under its root; each further leading dot steps one package up.
        """
        rel = os.path.relpath(file, self._root_of(file)).split(os.sep)
        keep = len(rel) - 1 - (binding.rel_level - 1)
        if keep < 0:
            return []
        return list(self._module_files.get(tuple(rel[:keep]) + binding.path, ()))

    def _module_denoted_files(self, file: str, receiver: str) -> list[str]:
        """Indexed module files that ``receiver`` can denote in file's scope.

        ``receiver`` resolves through (1) module names bound by top-level
        imports of ``file`` (``use_cases`` after ``from pkg import
        use_cases``) and (2) the receiver text itself as a dotted module path
        (``pkg.mod`` after ``import pkg.mod``). Returns each file once.
        """
        out: list[str] = []
        seen: set[str] = set()

        def add(files: list[str]) -> None:
            for f in files:
                if f not in seen:
                    seen.add(f)
                    out.append(f)

        for binding in self._bindings_by_file.get(file, ()):
            if binding.local != receiver:
                continue
            if binding.rel_level:
                add(self._relative_binding_files(file, binding))
            else:
                add(self._suffix_module_files(binding.path))
        add(self._suffix_module_files(tuple(receiver.split("."))))
        return out

    def _module_level_dependency(
        self, candidate: Entity, parsed: ParsedFile, reference: QualifiedRef
    ) -> bool:
        """True when reference (receiver, member) can denote module-level candidate.

        Only module-level symbols are reachable through a module path; class
        members keep their receiver-typing resolution.
        """
        if candidate.parent is not None:
            return False
        return candidate.file in self._module_denoted_files(parsed.path, reference.receiver)

    def _reference_matches(
        self, target: Entity, reference: QualifiedRef, owners: set[Entity]
    ) -> bool:
        """True when reference (member == target.name) can denote target."""
        if reference.receiver == _parent_qualified(target):
            return True
        if target.parent is None:
            # module-level symbol reached through a module alias or a dotted
            # module path (e.g. ``use_cases.start_code_review``)
            return any(
                target.file in self._module_denoted_files(owner.file, reference.receiver)
                for owner in owners
            )
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

    def _qualified_dependencies(
        self, parsed: ParsedFile, members: set[int]
    ) -> dict[Entity, set[ReferenceObs]]:
        result: dict[Entity, set[ReferenceObs]] = {}
        for reference, owners in parsed.qualified_refs.items():
            sites = [obs for owner, obs in owners.items() if id(owner) in members]
            if not sites:
                continue
            observations = set().union(*sites)
            for candidate in self._by_name.get(reference.member, []):
                if (
                    reference.receiver == _parent_qualified(candidate)
                    or self._qualified_dependency_used(candidate, parsed, members)
                    or self._module_level_dependency(candidate, parsed, reference)
                ):
                    result.setdefault(candidate, set()).update(observations)
        return result

    def _entity_key(self, e: Entity) -> tuple:
        return (e.file, e.qualified_name, e.start_line)

    def influence_paths(
        self,
        name: str,
        max_nodes: int = 5,
        max_paths: int = 100,
        kinds: Sequence[str] | None = None,
    ) -> list[list[Entity]]:
        paths, _ = self.influence_paths_with_total(
            name, max_nodes=max_nodes, max_paths=max_paths, kinds=kinds
        )
        return paths

    def influence_paths_with_total(
        self,
        name: str,
        max_nodes: int = 5,
        max_paths: int = 100,
        kinds: Sequence[str] | None = None,
    ) -> tuple[list[list[Entity]], int]:
        """Return visible paths and their total before max_paths truncation.

        Direction: A -> B means "A references B". Nodes are definition sites,
        so same-named symbols in different files stay distinct. max_nodes limits
        each rendered path without changing how many paths are found. Relations
        use the same qualified/type-aware resolution as impact(). ``kinds``
        keeps only edges whose reference sites include one of those kinds, so
        paths never route through a relation the caller filtered out.
        Deterministic order, capped at max_paths. Unknown symbols and isolated
        nodes return an empty list with a zero total.
        """
        targets = self.find_symbol(name)
        if not targets:
            return [], 0
        return self.influence_paths_entity_with_total(
            targets[0], max_nodes=max_nodes, max_paths=max_paths, kinds=kinds
        )

    def _side_neighbors(
        self, node: tuple, relation: str, kinds: Sequence[str] | None = None
    ) -> list[tuple]:
        """Definition keys node depends on ('down') or that depend on it ('up')."""
        report = self.impact_entity(self._entity_by_key[node], kinds=kinds)
        relations = report.depends_on if relation == "down" else report.dependents
        return sorted(
            (self._entity_key(relation.entity) for relation in relations),
            key=lambda key: (self._entity_by_key[key].kind, key),
        )

    def _side_paths(
        self,
        start: tuple,
        relation: str,
        max_enum: int = 2000,
        max_fanout: int = 20,
        kinds: Sequence[str] | None = None,
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
                for nb in self._side_neighbors(p[-1], relation, kinds)[:max_fanout]
                if nb not in p
            ]
            if not next_nodes or enumerated == max_enum:
                result.append(p)
            else:
                stack.extend(p + [nb] for nb in next_nodes)
        return result

    def influence_paths_entity_with_total(
        self,
        target: Entity,
        max_nodes: int = 5,
        max_paths: int = 100,
        kinds: Sequence[str] | None = None,
    ) -> tuple[list[list[Entity]], int]:
        """Influence paths for an exact definition, without name re-resolution.

        The returned total counts every distinct visible path enumerated before
        the max_paths cap (exploration is itself bounded by the hard caps in
        simple_paths), so it is the denominator the CLI reports truncation
        against. ``kinds`` filters the edges the walk may use.
        """
        if max_nodes < 1 or max_paths < 1:
            return [], 0

        tq = self._entity_key(target)
        if tq not in self._entity_by_key:
            return [], 0
        down = self._side_paths(tq, "down", kinds=kinds)
        up = self._side_paths(tq, "up", kinds=kinds)
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
        kinds: Sequence[str] | None = None,
    ) -> tuple[list[list[Entity]], int]:
        """Single-direction chains from an exact definition, no name re-resolution.

        Relation 'down' walks depends_on (what the target pulls in), 'up'
        walks dependents (what reaches the target); every chain starts at the
        target. With 'down' the chains read like the graph arrow convention
        (A -> B means A references B); with 'up' reverse a chain to get that
        order. max_nodes truncates each chain from the target end, so the
        whole budget goes into one direction. The total counts every distinct
        truncated chain enumerated before max_paths picked the shown ones,
        mirroring influence_paths_entity_with_total. ``kinds`` filters the
        edges the walk may use. Isolated nodes return an empty list with a
        zero total.
        """
        if max_nodes < 1 or max_paths < 1:
            return [], 0

        tq = self._entity_key(target)
        if tq not in self._entity_by_key:
            return [], 0
        paths = self._side_paths(tq, relation, kinds=kinds)
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

    def paths_between_entities_with_total(
        self,
        source: Entity,
        target: Entity,
        max_nodes: int = 5,
        max_paths: int = 100,
        kinds: Sequence[str] | None = None,
    ) -> tuple[list[list[Entity]], int]:
        """Shortest chains from an exact source reaching an exact target.

        Both ends are exact definitions; no name re-resolution happens. The
        walk uses only depends_on edges, so a chain reads like the graph
        arrow convention (source -> ... -> target); swap the arguments for
        the reverse question. Every returned chain has the same, minimal
        length, bounded by max_nodes symbols including both ends: BFS layer
        by layer, each definition expanded once, predecessor lists per
        layer keep every shortest chain without enumerating longer detours.
        When no route fits max_nodes (or none exists) the result is empty —
        an exact answer, not an enumeration cap artifact. The total is every
        shortest chain counted over the predecessor DAG, the denominator the
        CLI reports truncation against; max_paths picks the shown chains in
        stable order. ``kinds`` filters the edges the walk may use.
        source == target and unknown definitions return empty with zero.
        """
        if max_nodes < 2 or max_paths < 1:
            return [], 0

        sq = self._entity_key(source)
        tq = self._entity_key(target)
        if sq == tq or sq not in self._entity_by_key or tq not in self._entity_by_key:
            return [], 0

        # BFS over the predecessor DAG: depth_of assigns each node its
        # minimal layer, preds keeps every predecessor arriving inside it.
        depth_of: dict[tuple, int] = {sq: 0}
        preds: dict[tuple, list[tuple]] = {sq: []}
        frontier = [sq]
        reached = False
        for depth in range(1, max_nodes):
            next_frontier: list[tuple] = []
            for node in frontier:
                for neighbor in self._side_neighbors(node, "down", kinds):
                    if neighbor == tq:
                        depth_of[tq] = depth
                        bucket = preds.setdefault(tq, [])
                        if node not in bucket:
                            bucket.append(node)
                        reached = True
                    elif neighbor not in depth_of:
                        depth_of[neighbor] = depth
                        preds[neighbor] = [node]
                        next_frontier.append(neighbor)
                    elif depth_of[neighbor] == depth and node not in preds[neighbor]:
                        # same layer, another shortest route into the node
                        preds[neighbor].append(node)
            if reached or not next_frontier:
                break
            frontier = next_frontier
        if tq not in preds:
            return [], 0

        counts = {sq: 1}
        for node in sorted(preds, key=depth_of.__getitem__):
            if node != sq:
                counts[node] = sum(counts[pred] for pred in preds[node])
        total = counts[tq]

        # ponytail: recursive materialization, iterative if chains ever exceed ~900 symbols
        def chains_to(node: tuple) -> Iterator[tuple]:
            if node == sq:
                yield (sq,)
                return
            for pred in preds[node]:
                for chain in chains_to(pred):
                    yield chain + (node,)

        visible: list[list[tuple]] = []
        for chain in chains_to(tq):
            visible.append(list(chain))
            if len(visible) >= max_paths:
                break
        return [[self._entity_by_key[q] for q in path] for path in visible], total
