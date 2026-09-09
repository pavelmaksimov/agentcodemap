"""Build and render the agent-oriented context report."""

from __future__ import annotations

import json
import re
import shlex
from urllib.parse import quote

from codenav.index import RepoIndex
from codenav.model import Entity

SCHEMA = "codenav.agent/v1"
ANALYSIS = "name_based_heuristic"

#: How much of the source preview to keep when halving it toward the middle.
MIN_SOURCE_LINES = 2


def encode_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _location(entity: Entity, index: RepoIndex) -> dict[str, object]:
    path = index.agent_path(entity.file)
    return {
        "path": path,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
    }


def _record_id(location: dict[str, object], entity: Entity) -> str:
    return (
        f"e:{quote(str(location['path']), safe='/._-')}:"
        f"{location['start_line']}:{quote(entity.qualified_name, safe='._-')}"
    )


def entity_record(entity: Entity, index: RepoIndex) -> dict[str, object]:
    location = _location(entity, index)
    return {
        "id": _record_id(location, entity),
        "name": entity.name,
        "qualified_name": entity.qualified_name,
        "kind": entity.kind,
        "location": location,
    }


def _entity_sort_key(entity: Entity) -> tuple:
    return (entity.file, entity.start_line, entity.qualified_name)


def _entities(index: RepoIndex) -> list[Entity]:
    return sorted(
        (entity for parsed in index.files.values() for entity in parsed.entities),
        key=_entity_sort_key,
    )


def _lines(index: RepoIndex, entity: Entity) -> list[str]:
    """Parsed source lines for entity's file (already cached by the index)."""
    return index.files[entity.file].content_lines


def _resolve(
    index: RepoIndex, *, name: str | None = None, exact_id: str | None = None
) -> tuple[str, Entity | None, list[Entity]]:
    if exact_id:
        matches = [entity for entity in _entities(index) if _record_id(_location(entity, index), entity) == exact_id]
        return ("ok", matches[0], matches) if matches else ("not_found", None, [])
    matches = sorted(index.find_symbol(name or ""), key=_entity_sort_key)
    if not matches:
        return "not_found", None, []
    if len(matches) > 1:
        return "ambiguous", None, matches
    return "ok", matches[0], matches


def _coverage(index: RepoIndex) -> dict:
    return {
        "files_parsed": len(index.files),
        "entities_indexed": sum(len(parsed.entities) for parsed in index.files.values()),
    }


def _source(entity: Entity, index: RepoIndex) -> dict:
    lines = _lines(index, entity)
    end = min(entity.end_line, len(lines))
    return {
        "location": _location(entity, index),
        "complete": True,
        "text": "\n".join(lines[entity.start_line - 1 : end]),
    }


def _preview(entity: Entity, index: RepoIndex) -> str:
    lines = _lines(index, entity)
    return lines[entity.start_line - 1].strip() if entity.start_line <= len(lines) else ""


def _evidence(owner: Entity, spelling: str, index: RepoIndex, limit: int = 3) -> list[dict]:
    lines = _lines(index, owner)
    rx = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(spelling)}(?![A-Za-z0-9_])")
    path = _location(owner, index)["path"]
    out: list[dict] = []
    for line_number in range(owner.start_line, min(owner.end_line, len(lines)) + 1):
        if rx.search(lines[line_number - 1]):
            out.append({"path": path, "line": line_number, "text": lines[line_number - 1].strip()})
            if len(out) == limit:
                break
    return out


def _relations(index: RepoIndex, target: Entity) -> dict:
    impact = index.impact_entity(target)

    def relation(neighbor: Entity, spelling: str, owner: Entity) -> dict:
        candidates = index.name_candidates(spelling)
        return {
            "entity": entity_record(neighbor, index),
            "resolution": "unique_by_name" if len(candidates) == 1 else "ambiguous_by_name",
            "candidate_count": len(candidates),
            "evidence": _evidence(owner, spelling, index),
        }

    incoming = [relation(entity, target.name, entity) for entity in impact.dependents]
    outgoing = [relation(entity, entity.name, target) for entity in impact.depends_on]
    key = lambda item: (
        item["resolution"] != "unique_by_name",
        {"function": 0, "method": 0, "class": 1, "constant": 2, "attr": 3}.get(
            item["entity"]["kind"], 4
        ),
        item["entity"]["location"]["path"],
        item["entity"]["location"]["start_line"],
        item["entity"]["qualified_name"],
    )
    return {"analysis": ANALYSIS, "incoming": sorted(incoming, key=key), "outgoing": sorted(outgoing, key=key)}


def _paths(index: RepoIndex, target: Entity, nodes: int) -> list[dict]:
    paths, _ = index.influence_paths_entity_with_total(target, max_nodes=nodes)
    return [
        {
            "entity_ids": [
                _record_id(_location(entity, index), entity) for entity in path
            ],
            "labels": [entity.name for entity in path],
        }
        for path in paths
    ]


def _resolution_report(
    index: RepoIndex,
    status: str,
    candidates: list[Entity],
    query: str,
) -> dict:
    records = [
        entity_record(entity, index) | {"preview": _preview(entity, index)} for entity in candidates
    ]
    return {
        "schema": SCHEMA,
        "workflow": "eager_context",
        "status": status,
        "query": query,
        "candidates": records,
        "coverage": _coverage(index),
        "next_actions": [
            {
                "reason": "select_candidate",
                "argv": ["codenav", "context", "--id", record["id"], "--root", *index.roots],
            }
            for record in records
        ],
    }


def build_context(
    roots: str | list[str],
    *,
    name: str | None = None,
    exact_id: str | None = None,
    nodes: int = 5,
) -> dict:
    index = RepoIndex(roots)
    status, target, candidates = _resolve(index, name=name, exact_id=exact_id)
    query = exact_id or name or ""
    if target is None:
        return _resolution_report(index, status, candidates, query)
    relations = _relations(index, target)
    report = {
        "schema": SCHEMA,
        "workflow": "eager_context",
        "status": "ok",
        "target": entity_record(target, index),
        "source": _source(target, index),
        "relations": relations,
        "paths": _paths(index, target, nodes),
        "coverage": _coverage(index),
        "next_actions": [],
    }
    neighbors = [
        item
        for item in relations["incoming"] + relations["outgoing"]
        if item["resolution"] == "unique_by_name"
    ]
    for item in neighbors[:2]:
        report["next_actions"].append(
            {
                "reason": "inspect_related_symbol",
                "argv": ["codenav", "context", "--id", item["entity"]["id"], "--root", *index.roots],
            }
        )
    return report


def limit_report(report: dict, limit: int) -> dict:
    """Fit low-priority report sections into a byte cap without breaking JSON.

    Sections are dropped in a fixed order: paths, then relation entries (the
    sort in _relations puts ambiguous ones last), then the source preview is
    halved toward the middle until it is gone, then next actions, then
    candidates.  The truncation block tracks omissions; when anything is
    dropped the status becomes "partial".  `output_bytes` is self-describing:
    size() re-encodes until the field's own digits stop changing the total
    (converges within a few passes), so the reported size equals the encoded
    output.
    """
    truncation = {
        "truncated": False,
        "limit_bytes": limit,
        "output_bytes": 0,
        "omitted": {},
    }
    report["truncation"] = truncation
    omitted = truncation["omitted"]

    def size() -> int:
        while True:
            actual = len(encode_json(report).encode())
            if truncation["output_bytes"] == actual:
                return actual
            truncation["output_bytes"] = actual

    source_original: list[str] | None = None
    source_keep = 0

    def bump(section: str) -> str:
        omitted[section] = omitted.get(section, 0) + 1
        return section

    def drop_path() -> str | None:
        paths = report.get("paths")
        if not paths:
            return None
        paths.pop()
        return bump("paths")

    def drop_relation() -> str | None:
        relations = report.get("relations")
        if relations is None:
            return None
        for direction in ("outgoing", "incoming"):
            items = relations.get(direction)
            if items:
                items.pop()
                return bump(direction)
        return None

    def shrink_source() -> str | None:
        nonlocal source_original, source_keep
        source = report.get("source")
        if not source:
            return None
        if source_original is None:
            source_original = source.get("text", "").splitlines()
        if not source_original:
            return None
        if source_keep == 0:
            source_keep = len(source_original)
        if source_keep > MIN_SOURCE_LINES:
            source_keep = max(MIN_SOURCE_LINES, source_keep // 2)
            head = (source_keep + 1) // 2
            tail = source_keep // 2
            source["text"] = "\n".join(
                source_original[:head]
                + [f"... {len(source_original) - source_keep} lines omitted ..."]
                + source_original[-tail:]
            )
            source["complete"] = False
            omitted["source_lines"] = len(source_original) - source_keep
            return "source_lines"
        if source.get("text"):
            source["text"] = ""
            source["complete"] = False
            omitted["source_lines"] = len(source_original)
            return "source_lines"
        return None

    def drop_next_action() -> str | None:
        next_actions = report.get("next_actions")
        if not next_actions:
            return None
        next_actions.pop()
        return bump("next_actions")

    def drop_candidate() -> str | None:
        candidates = report.get("candidates")
        if not candidates or len(candidates) <= 1:
            return None
        candidates.pop()
        return bump("candidates")

    def drop() -> str | None:
        """Drop the next least-valuable unit; return its section name."""
        for dropper in (drop_path, drop_relation, shrink_source, drop_next_action, drop_candidate):
            section = dropper()
            if section is not None:
                return section
        return None

    while size() > limit:
        if drop() is None:
            break
    truncation["truncated"] = bool(omitted)
    if omitted and report.get("status") == "ok":
        report["status"] = "partial"
        # the status flip adds a few bytes; keep trimming until it fits again
        while size() > limit:
            if drop() is None:
                break
    return report


def render_text(report: dict) -> str:
    lines = [f"STATUS {report['status']}  WORKFLOW {report['workflow']}"]
    if report.get("target"):
        target = report["target"]
        loc = target["location"]
        lines += [
            f"TARGET {target['kind']} {target['qualified_name']}",
            f"  id: {target['id']}",
            f"  location: {loc['path']}:{loc['start_line']}-{loc['end_line']}",
        ]
        if target.get("preview"):
            lines.append(f"  preview: {target['preview']}")
    if report.get("candidates"):
        lines.append("CANDIDATES")
        for candidate in report["candidates"]:
            loc = candidate["location"]
            lines.append(f"  {candidate['id']}  {loc['path']}:{loc['start_line']}-{loc['end_line']}")
    if report.get("source"):
        source = report["source"]
        lines += [f"SOURCE complete={str(source['complete']).lower()}", source["text"]]
    if report.get("relations"):
        relations = report["relations"]
        lines.append(f"RELATIONS analysis={relations['analysis']}")
        for direction in ("incoming", "outgoing"):
            lines.append(f"  {direction}:")
            for item in relations[direction]:
                entity = item["entity"]
                loc = entity["location"]
                lines.append(
                    f"    {entity['qualified_name']}  {loc['path']}:{loc['start_line']}-{loc['end_line']}  {item['resolution']}"
                )
                for evidence in item["evidence"]:
                    lines.append(f"      evidence: {evidence['path']}:{evidence['line']}  {evidence['text']}")
    if report.get("paths") is not None:
        lines.append("PATHS")
        for path in report.get("paths", []):
            lines.append("  " + " -> ".join(path["labels"]))
    coverage = report.get("coverage", {})
    lines.append(
        f"COVERAGE files_parsed={coverage.get('files_parsed', 0)} entities_indexed={coverage.get('entities_indexed', 0)}"
    )
    if report.get("truncation"):
        truncation = report["truncation"]
        lines.append(
            f"OUTPUT bytes={truncation['output_bytes']} limit={truncation['limit_bytes']} truncated={str(truncation['truncated']).lower()}"
        )
    if report.get("next_actions"):
        lines.append("NEXT")
        for action in report["next_actions"]:
            lines.append(f"  {action['reason']}: {shlex.join(action['argv'])}")
    return "\n".join(lines)
