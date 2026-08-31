"""EXPERIMENTAL agent workflows used to compare eager and progressive navigation."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from urllib.parse import quote

from codenav.core import Entity, RepoIndex

SCHEMA = "codenav.agent/v1"
ANALYSIS = "name_based_heuristic"


def encode_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def entity_id(entity: Entity, root: str) -> str:
    path = Path(os.path.relpath(os.path.abspath(entity.file), os.path.abspath(root))).as_posix()
    return f"e:{quote(path, safe='/._-')}:{entity.start_line}:{quote(entity.qualified_name, safe='._-')}"


def entity_record(entity: Entity, root: str) -> dict:
    path = Path(os.path.relpath(os.path.abspath(entity.file), os.path.abspath(root))).as_posix()
    return {
        "id": entity_id(entity, root),
        "name": entity.name,
        "qualified_name": entity.qualified_name,
        "kind": entity.kind,
        "location": {
            "path": path,
            "start_line": entity.start_line,
            "end_line": entity.end_line,
        },
    }


def _entities(index: RepoIndex) -> list[Entity]:
    return sorted(
        (entity for parsed in index.files.values() for entity in parsed.entities),
        key=lambda entity: (entity.file, entity.start_line, entity.qualified_name),
    )


def _resolve(
    index: RepoIndex, root: str, *, name: str | None = None, exact_id: str | None = None
) -> tuple[str, Entity | None, list[Entity]]:
    if exact_id:
        matches = [entity for entity in _entities(index) if entity_id(entity, root) == exact_id]
        return ("ok", matches[0], matches) if matches else ("not_found", None, [])
    matches = sorted(
        index.find_symbol(name or ""),
        key=lambda entity: (entity.file, entity.start_line, entity.qualified_name),
    )
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


def _source(entity: Entity, root: str) -> dict:
    lines = Path(entity.file).read_text(encoding="utf-8").splitlines()
    end = min(entity.end_line, len(lines))
    return {
        "location": entity_record(entity, root)["location"],
        "complete": True,
        "text": "\n".join(lines[entity.start_line - 1 : end]),
    }


def _preview(entity: Entity) -> str:
    lines = Path(entity.file).read_text(encoding="utf-8").splitlines()
    return lines[entity.start_line - 1].strip() if entity.start_line <= len(lines) else ""


def _evidence(owner: Entity, spelling: str, root: str, limit: int = 3) -> list[dict]:
    lines = Path(owner.file).read_text(encoding="utf-8").splitlines()
    rx = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(spelling)}(?![A-Za-z0-9_])")
    path = entity_record(owner, root)["location"]["path"]
    out: list[dict] = []
    for line_number in range(owner.start_line, min(owner.end_line, len(lines)) + 1):
        if rx.search(lines[line_number - 1]):
            out.append(
                {"path": path, "line": line_number, "text": lines[line_number - 1].strip()}
            )
            if len(out) == limit:
                break
    return out


def _relations(index: RepoIndex, target: Entity, root: str) -> dict:
    impact = index.impact_entity(target)

    def relation(neighbor: Entity, spelling: str, owner: Entity) -> dict:
        candidates = index._by_name.get(spelling, [])
        return {
            "entity": entity_record(neighbor, root),
            "resolution": "unique_by_name" if len(candidates) == 1 else "ambiguous_by_name",
            "candidate_count": len(candidates),
            "evidence": _evidence(owner, spelling, root),
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


def _paths(index: RepoIndex, target: Entity, root: str, nodes: int) -> list[dict]:
    return [
        {
            "entity_ids": [entity_id(entity, root) for entity in path],
            "labels": [entity.name for entity in path],
        }
        for path in index.influence_paths_entity(target, max_nodes=nodes)
    ]


def _resolution_report(
    workflow: str,
    index: RepoIndex,
    root: str,
    status: str,
    candidates: list[Entity],
    query: str,
) -> dict:
    records = [entity_record(entity, root) | {"preview": _preview(entity)} for entity in candidates]
    command = "context" if workflow == "eager_context" else "read"
    return {
        "schema": SCHEMA,
        "workflow": workflow,
        "status": status,
        "query": query,
        "candidates": records,
        "coverage": _coverage(index),
        "next_actions": [
            {
                "reason": "select_candidate",
                "argv": ["codenav", command, "--id", record["id"], "--root", root]
                if command == "context"
                else ["codenav", command, record["id"], "--root", root],
            }
            for record in records
        ],
    }


def build_context(
    root: str, *, name: str | None = None, exact_id: str | None = None, nodes: int = 5
) -> dict:
    index = RepoIndex(root)
    status, target, candidates = _resolve(index, root, name=name, exact_id=exact_id)
    query = exact_id or name or ""
    if target is None:
        return _resolution_report("eager_context", index, root, status, candidates, query)
    relations = _relations(index, target, root)
    report = {
        "schema": SCHEMA,
        "workflow": "eager_context",
        "status": "ok",
        "target": entity_record(target, root),
        "source": _source(target, root),
        "relations": relations,
        "paths": _paths(index, target, root, nodes),
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
                "argv": ["codenav", "context", "--id", item["entity"]["id"], "--root", root],
            }
        )
    return report


def build_select(root: str, name: str) -> dict:
    index = RepoIndex(root)
    status, target, candidates = _resolve(index, root, name=name)
    if target is None:
        return _resolution_report("progressive_select", index, root, status, candidates, name)
    record = entity_record(target, root)
    return {
        "schema": SCHEMA,
        "workflow": "progressive_select",
        "status": "ok",
        "target": record | {"preview": _preview(target)},
        "coverage": _coverage(index),
        "next_actions": [
            {"reason": "read_source", "argv": ["codenav", "read", record["id"], "--root", root]},
            {
                "reason": "expand_relations",
                "argv": ["codenav", "expand", record["id"], "--root", root],
            },
        ],
    }


def build_read(root: str, exact_id: str) -> dict:
    index = RepoIndex(root)
    status, target, _ = _resolve(index, root, exact_id=exact_id)
    if target is None:
        return _resolution_report("progressive_read", index, root, status, [], exact_id)
    record = entity_record(target, root)
    return {
        "schema": SCHEMA,
        "workflow": "progressive_read",
        "status": "ok",
        "target": record,
        "source": _source(target, root),
        "coverage": _coverage(index),
        "next_actions": [
            {
                "reason": "expand_relations",
                "argv": ["codenav", "expand", record["id"], "--root", root],
            }
        ],
    }


def build_expand(root: str, exact_id: str, nodes: int = 5) -> dict:
    index = RepoIndex(root)
    status, target, _ = _resolve(index, root, exact_id=exact_id)
    if target is None:
        return _resolution_report("progressive_expand", index, root, status, [], exact_id)
    record = entity_record(target, root)
    relations = _relations(index, target, root)
    return {
        "schema": SCHEMA,
        "workflow": "progressive_expand",
        "status": "ok",
        "target": record,
        "relations": relations,
        "paths": _paths(index, target, root, nodes),
        "coverage": _coverage(index),
        "next_actions": [
            {"reason": "read_source", "argv": ["codenav", "read", record["id"], "--root", root]}
        ],
    }


def limit_report(report: dict, limit: int) -> dict:
    """Fit low-priority report sections into a byte cap without breaking JSON."""
    omitted: dict[str, int] = {}
    report["truncation"] = {"truncated": False, "limit_bytes": limit, "omitted": omitted}

    def size() -> int:
        current = report["truncation"].get("output_bytes", 0)
        for _ in range(3):
            report["truncation"]["output_bytes"] = current
            updated = len(encode_json(report).encode())
            if updated == current:
                break
            current = updated
        report["truncation"]["output_bytes"] = current
        return current

    while report.get("paths") and size() > limit:
        report["paths"].pop()
        omitted["paths"] = omitted.get("paths", 0) + 1
    for direction in ("outgoing", "incoming"):
        items = report.get("relations", {}).get(direction, [])
        while items and size() > limit:
            items.pop()
            omitted[direction] = omitted.get(direction, 0) + 1
    if size() > limit and report.get("source", {}).get("text"):
        original = report["source"]["text"].splitlines()
        keep = len(original)
        while keep > 2 and size() > limit:
            keep = max(2, keep // 2)
            head = (keep + 1) // 2
            tail = keep // 2
            omitted_lines = len(original) - keep
            report["source"]["text"] = "\n".join(
                original[:head] + [f"... {omitted_lines} lines omitted ..."] + original[-tail:]
            )
            report["source"]["complete"] = False
            omitted["source_lines"] = omitted_lines
        if size() > limit:
            report["source"]["text"] = ""
            report["source"]["complete"] = False
            omitted["source_lines"] = len(original)
    while report.get("next_actions") and size() > limit:
        report["next_actions"].pop()
        omitted["next_actions"] = omitted.get("next_actions", 0) + 1
    while len(report.get("candidates", [])) > 1 and size() > limit:
        report["candidates"].pop()
        omitted["candidates"] = omitted.get("candidates", 0) + 1
    report["truncation"]["truncated"] = bool(omitted)
    if omitted and report.get("status") == "ok":
        report["status"] = "partial"
        while report.get("next_actions") and size() > limit:
            report["next_actions"].pop()
            omitted["next_actions"] = omitted.get("next_actions", 0) + 1
    size()
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
