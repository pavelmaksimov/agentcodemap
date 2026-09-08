"""Experimental JSON projections for the agent context report."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from codenav.agent import limit_report, render_text


VARIANTS = ("minimal", "evidence", "task", "hybrid")
SCHEMA = "codenav.context/v2"
COMPLETE_LIMIT = 16 * 1024


def _relations(report: dict) -> list[tuple[str, dict]]:
    relations = report.get("relations", {})
    return [
        (direction, item)
        for direction in ("incoming", "outgoing")
        for item in relations.get(direction, [])
    ]


def _actions(report: dict) -> dict[str, dict]:
    actions: dict[str, dict] = {}
    for action in report.get("next_actions", []):
        argv = action.get("argv", [])
        if "--id" in argv:
            try:
                actions[argv[argv.index("--id") + 1]] = action
            except IndexError:
                pass
    return actions


def _action_entity_id(action: dict) -> str | None:
    argv = action.get("argv", [])
    if "--id" not in argv:
        return None
    index = argv.index("--id") + 1
    return argv[index] if index < len(argv) else None


def _location_text(location: dict) -> str:
    return f"{location['path']}:{location['start_line']}-{location['end_line']}"


def _target(report: dict) -> dict | None:
    target = report.get("target")
    if target is None:
        return None
    source = report.get("source", {})
    return {
        "id": target["id"],
        "name": target["name"],
        "qualified_name": target["qualified_name"],
        "kind": target["kind"],
        "location": target["location"],
        "source": source.get("text", ""),
    }


def _coverage(report: dict, *, files_skipped: bool = False) -> dict:
    coverage = report.get("coverage", {})
    result = {"files_parsed": coverage.get("files_parsed", 0)}
    if files_skipped:
        result["files_skipped"] = 0
    result["entities_indexed"] = coverage.get("entities_indexed", 0)
    return result


def _minimal(report: dict, *, include_actions: bool = True) -> dict:
    target = _target(report)
    result: dict[str, Any] = {}
    if target is not None:
        result["target"] = target
        result["relation_analysis"] = report.get("relations", {}).get("analysis")
        result["relations"] = []
        actions = _actions(report) if include_actions else {}
        for direction, item in _relations(report):
            entity = item["entity"]
            relation = {
                "direction": direction,
                "entity_id": entity["id"],
                "name": entity["name"],
                "kind": entity["kind"],
                "location": entity["location"],
                "resolution": item["resolution"],
                "evidence": item.get("evidence", []),
            }
            if entity["id"] in actions:
                relation["argv"] = actions[entity["id"]]["argv"]
            result["relations"].append(relation)
        result["paths"] = [
            {"entity_ids": path.get("entity_ids", []), "names": path.get("labels", [])}
            for path in report.get("paths", [])
        ]
    else:
        result["candidates"] = report.get("candidates", [])

    return {
        "schema": SCHEMA,
        "status": report.get("status"),
        "result": result,
        "meta": {
            "coverage": _coverage(report, files_skipped=True),
            "truncation": {
                "limit_bytes": COMPLETE_LIMIT,
                "output_bytes": 0,
                "omitted": {},
            },
        },
    }


def _entity_record(entity: dict, roles: list[str]) -> dict:
    return {
        "id": entity["id"],
        "roles": roles,
        "qualified_name": entity["qualified_name"],
        "kind": entity["kind"],
        "location": entity["location"],
    }


def _evidence_variant(report: dict) -> dict:
    target = report.get("target")
    target_id = target["id"] if target else None
    all_relations = _relations(report)
    entities: dict[str, dict] = {}
    evidence: list[dict] = []

    def add_entity(entity: dict, role: str) -> None:
        record = entities.get(entity["id"])
        if record is None:
            entities[entity["id"]] = _entity_record(entity, [role])
        elif role not in record["roles"]:
            record["roles"].append(role)

    if target is not None:
        add_entity(target, "target")
    else:
        for candidate in report.get("candidates", []):
            add_entity(candidate, "candidate")
    evidence_ids: dict[tuple[str, str, int, str], str] = {}
    relation_specs: list[dict] = []
    actions = _actions(report)
    for index, (direction, item) in enumerate(all_relations):
        entity = item["entity"]
        add_entity(entity, "relation_candidate")
        relation_evidence: list[str] = []
        for occurrence in item.get("evidence", []):
            key = (
                entity["id"],
                occurrence["path"],
                occurrence["line"],
                occurrence["text"],
            )
            ev_id = evidence_ids.get(key)
            if ev_id is None:
                ev_id = f"ev:{len(evidence)}"
                evidence_ids[key] = ev_id
                evidence.append(
                    {
                        "id": ev_id,
                        "kind": "lexical_name_occurrence",
                        "owner_entity_id": entity["id"],
                        "location": {"path": occurrence["path"], "line": occurrence["line"]},
                        "snippet": occurrence["text"],
                    }
                )
            relation_evidence.append(ev_id)
        from_id = entity["id"] if direction == "incoming" else target_id
        candidate_id = target_id if direction == "incoming" else entity["id"]
        resolution = item.get("resolution")
        relation = {
            "id": f"rel:{index}",
            "from_entity_id": from_id,
            "candidate_entity_ids": [candidate_id] if candidate_id else [],
            "resolution": {
                "state": "unique_candidate"
                if resolution == "unique_by_name"
                else "ambiguous_candidates",
                "candidate_count": item.get("candidate_count", 1),
                "semantic_binding": False,
            },
            "evidence_ids": relation_evidence,
            "roles": [
                "incoming_to_target" if direction == "incoming" else "outgoing_from_target"
            ],
        }
        if entity["id"] in actions:
            relation["argv"] = actions[entity["id"]]["argv"]
        relation_specs.append(relation)

    paths: list[dict] = []
    known_edges: dict[tuple[str, str], str] = {}
    for relation in relation_specs:
        from_id = relation["from_entity_id"]
        for candidate_id in relation["candidate_entity_ids"]:
            known_edges[(from_id, candidate_id)] = relation["id"]
    for path_index, path in enumerate(report.get("paths", [])):
        ids = path.get("entity_ids", [])
        relation_ids: list[str] = []
        for edge_index, (from_id, to_id) in enumerate(zip(ids, ids[1:])):
            relation_id = known_edges.get((from_id, to_id))
            if relation_id is None:
                relation_id = f"rel:path:{path_index}:{edge_index}"
                relation_specs.append(
                    {
                        "id": relation_id,
                        "from_entity_id": from_id,
                        "candidate_entity_ids": [to_id],
                        "resolution": {
                            "state": "path_only",
                            "candidate_count": 1,
                            "semantic_binding": False,
                        },
                        "evidence_ids": [],
                        "roles": ["path_step"],
                    }
                )
                known_edges[(from_id, to_id)] = relation_id
            else:
                relation = next(item for item in relation_specs if item["id"] == relation_id)
                if "path_step" not in relation["roles"]:
                    relation["roles"].append("path_step")
            relation_ids.append(relation_id)
            if from_id not in entities:
                entities[from_id] = {"id": from_id, "roles": ["path_node"]}
            elif "path_node" not in entities[from_id]["roles"]:
                entities[from_id]["roles"].append("path_node")
            if to_id not in entities:
                entities[to_id] = {"id": to_id, "roles": ["path_node"]}
            elif "path_node" not in entities[to_id]["roles"]:
                entities[to_id]["roles"].append("path_node")
        paths.append({"id": f"path:{path_index}", "entity_ids": ids, "relation_ids": relation_ids})

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": {
            "outcome": "resolved"
            if report.get("status") in ("ok", "partial")
            else report.get("status"),
            "completeness": "partial"
            if report.get("status") == "partial"
            or report.get("source", {}).get("complete") is False
            else "complete",
        },
        "resolution": {
            "method": "exact_name",
            "candidate_count": 1 if target is not None else len(report.get("candidates", [])),
            "selected_entity_id": target_id,
        },
        "analysis": {
            "relation_method": "parsed_reference_name_match",
            "semantic_binding": False,
            "claim_strength": "heuristic",
        },
        "target": target_id,
        "entities": list(entities.values()),
    }
    if target is not None:
        location = report["source"].get("location", target["location"])
        payload["source"] = {
            "entity_id": target_id,
            "complete": report.get("source", {}).get("complete", True),
            "chunks": [
                {
                    "start_line": location["start_line"],
                    "end_line": location["end_line"],
                    "text": report["source"].get("text", ""),
                }
            ],
        }
    elif report.get("candidates"):
        payload["candidates"] = [
            _entity_record(candidate, ["candidate"]) | {"preview": candidate.get("preview", "")}
            for candidate in report["candidates"]
        ]
    payload.update(
        {
            "evidence": evidence,
            "relations": relation_specs,
            "paths": paths,
            "omissions": [],
        }
    )
    for relation in relation_specs:
        if relation.get("evidence_ids") and relation.get("argv"):
            payload["next_action"] = {
                "kind": "inspect_related_entity",
                "based_on_relation_id": relation["id"],
                "argv": relation["argv"],
            }
            break
    if target is None:
        payload["candidates"] = report.get("candidates", [])
    return payload


def _task(report: dict) -> dict:
    target = report.get("target")
    target_id = target["id"] if target else None
    source = report.get("source", {})
    items: list[dict] = []
    actions = _actions(report)
    for direction, item in _relations(report):
        entity = item["entity"]
        relation = {
            "direction": direction,
            "entity_id": entity["id"],
            "qualified_name": entity["qualified_name"],
            "kind": entity["kind"],
            "location": entity["location"],
            "resolution": item["resolution"],
            "evidence": item.get("evidence", []),
        }
        if entity["id"] in actions:
            relation["argv"] = actions[entity["id"]]["argv"]
        items.append(relation)

    incoming = [item for item in items if item["direction"] == "incoming"]
    outgoing = [item for item in items if item["direction"] == "outgoing"]
    if target is None:
        summary = f"{report.get('query', '')}: {report.get('status', 'not_found')}."
    else:
        summary = (
            f"{target['qualified_name']}: source complete; "
            f"review {len(incoming)} caller{'s' if len(incoming) != 1 else ''} before changing behavior."
        )
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": report.get("status"),
        "summary": summary,
    }
    if target is not None:
        first = next(
            ((index, item) for index, item in enumerate(items) if item.get("evidence")),
            None,
        )
        if first is not None:
            index, item = first
            evidence = item["evidence"][0]
            verb = "calls the target" if item["direction"] == "incoming" else "is used by the target"
            payload["next_action"] = {
                "kind": "review_relation",
                "relation_index": index,
                "reason": f"{item['qualified_name']} {verb} at {evidence['path']}:{evidence['line']}",
            }
        payload["target"] = {
            "entity_id": target_id,
            "qualified_name": target["qualified_name"],
            "kind": target["kind"],
            "location": target["location"],
        }
        payload["source"] = {
            "complete": source.get("complete", True),
            "text": source.get("text", ""),
        }
    elif report.get("candidates"):
        payload["candidates"] = report["candidates"]
    else:
        payload["candidates"] = report.get("candidates", [])
    payload["relations"] = {
        "analysis": report.get("relations", {}).get("analysis"),
        "counts": {
            "incoming": {"found": len(incoming), "shown": len(incoming)},
            "outgoing": {"found": len(outgoing), "shown": len(outgoing)},
        },
        "items": items,
    }
    path_items = [
        {
            "entity_ids": path.get("entity_ids", []),
            "display": " -> ".join(path.get("labels", [])),
        }
        for path in report.get("paths", [])
    ]
    payload["paths"] = {
        "counts": {"reported": len(path_items), "shown": len(path_items)},
        "items": path_items,
    }
    payload["truncation"] = {
        "truncated": False,
        "limit_bytes": COMPLETE_LIMIT,
        "omitted": [],
    }
    return payload


def _hybrid(report: dict) -> dict:
    target = report.get("target")
    target_id = target["id"] if target else None
    actions = _actions(report)
    relations: list[dict] = []
    for index, (direction, item) in enumerate(_relations(report)):
        entity = item["entity"]
        resolution = item.get("resolution")
        relation = {
            "id": f"rel:{index}",
            "direction": direction,
            "entity": {
                "id": entity["id"],
                "name": entity["name"],
                "qualified_name": entity["qualified_name"],
                "kind": entity["kind"],
                "location": _location_text(entity["location"]),
            },
            "resolution": "unique_name_candidate"
            if resolution == "unique_by_name"
            else "ambiguous_name_candidate",
            "semantic_binding": False,
            "evidence": item.get("evidence", []),
        }
        if entity["id"] in actions:
            relation["argv"] = actions[entity["id"]]["argv"]
        relations.append(relation)

    attention: list[dict] = []
    for relation in relations:
        if relation["evidence"]:
            attention.append(
                {
                    "kind": "review_caller"
                    if relation["direction"] == "incoming"
                    else "review_dependency",
                    "relation_id": relation["id"],
                }
            )
            break
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": {
            "outcome": "resolved"
            if report.get("status") in ("ok", "partial")
            else report.get("status"),
            "completeness": "partial"
            if report.get("status") == "partial"
            or report.get("source", {}).get("complete") is False
            else "complete",
        },
    }
    if target is not None:
        payload["target"] = {
            "id": target_id,
            "name": target["name"],
            "qualified_name": target["qualified_name"],
            "kind": target["kind"],
            "location": _location_text(target["location"]),
            "source": report.get("source", {}).get("text", ""),
        }
    elif report.get("candidates"):
        payload["candidates"] = report["candidates"]
    else:
        payload["candidates"] = report.get("candidates", [])
    payload.update(
        {
            "attention": attention,
            "relations": relations,
            "paths": [path.get("entity_ids", []) for path in report.get("paths", [])],
            "meta": {
                "coverage": {"files_parsed": report.get("coverage", {}).get("files_parsed", 0)},
                "truncation": {"omitted": []},
            },
        }
    )
    return payload


def _metadata(payload: dict, variant: str) -> dict:
    if variant == "minimal":
        return payload["meta"]["truncation"]
    if variant == "hybrid":
        return payload["meta"]["truncation"]
    return payload["truncation"]


def _set_metadata(payload: dict, variant: str, *, limit: int, truncated: bool) -> dict:
    if variant not in ("minimal", "hybrid"):
        payload.setdefault("truncation", {})
    metadata = _metadata(payload, variant)
    metadata["limit_bytes"] = limit
    metadata["output_bytes"] = 0
    metadata["truncated"] = truncated
    if variant == "minimal":
        metadata["omitted"] = {}
    else:
        metadata["omitted"] = []
    return metadata


def _encode(payload: dict, metadata: dict) -> str:
    for _ in range(8):
        output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        size = len(output.encode("utf-8"))
        if metadata["output_bytes"] == size:
            return output
        metadata["output_bytes"] = size
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _append_omission(metadata: dict, name: str) -> None:
    omitted = metadata["omitted"]
    if isinstance(omitted, list):
        if name not in omitted:
            omitted.append(name)
    else:
        omitted[name] = omitted.get(name, 0) + 1


def _drop_paths(payload: dict, variant: str, metadata: dict) -> bool:
    if variant == "minimal":
        paths = payload["result"].get("paths", [])
    elif variant == "hybrid":
        paths = payload.get("paths", [])
    else:
        paths = payload.get("paths", [])
    if not paths:
        return False
    if variant == "task":
        payload["paths"]["items"].pop()
        payload["paths"]["counts"]["shown"] = len(payload["paths"]["items"])
    elif variant == "minimal":
        paths.pop()
    else:
        payload["paths"].pop()
    _append_omission(metadata, "paths")
    return True


def _drop_relations(payload: dict, variant: str, metadata: dict) -> bool:
    if variant == "minimal":
        relations = payload["result"].get("relations", [])
        if not relations:
            return False
        relations.pop()
    elif variant == "evidence":
        relations = payload.get("relations", [])
        if not relations:
            return False
        relation = relations.pop()
        relation_ids = {relation["id"]}
        referenced_evidence = {
            evidence_id
            for item in relations
            for evidence_id in item.get("evidence_ids", [])
        }
        payload["evidence"] = [
            evidence
            for evidence in payload.get("evidence", [])
            if evidence["id"] in referenced_evidence
        ]
        action = payload.get("next_action")
        if action and action.get("based_on_relation_id") in relation_ids:
            payload.pop("next_action", None)
    elif variant == "task":
        relations = payload["relations"]["items"]
        if not relations:
            return False
        relations.pop()
        payload["relations"]["counts"]["incoming"]["shown"] = sum(
            item["direction"] == "incoming" for item in relations
        )
        payload["relations"]["counts"]["outgoing"]["shown"] = sum(
            item["direction"] == "outgoing" for item in relations
        )
        payload.pop("next_action", None)
    else:
        relations = payload.get("relations", [])
        if not relations:
            return False
        removed = relations.pop()
        removed_id = removed["id"]
        payload["attention"] = [
            item for item in payload.get("attention", []) if item.get("relation_id") != removed_id
        ]
        payload.pop("next_action", None)
    _append_omission(metadata, "relations")
    return True


def _shrink_source(payload: dict, variant: str, metadata: dict) -> bool:
    holders: list[tuple[dict, str]] = []
    if variant == "minimal" and payload["result"].get("target"):
        holders.append((payload["result"]["target"], "source"))
    elif variant == "evidence" and payload.get("source"):
        chunks = payload["source"].get("chunks", [])
        if chunks:
            holders.append((chunks[0], "text"))
    elif variant == "task" and payload.get("source"):
        holders.append((payload["source"], "text"))
    elif variant == "hybrid" and payload.get("target"):
        holders.append((payload["target"], "source"))
    changed = False
    for holder, key in holders:
        text = holder.get(key, "")
        if not text:
            continue
        lines = text.splitlines()
        if len(lines) <= 2:
            holder[key] = ""
        else:
            keep = max(2, len(lines) // 2)
            head = (keep + 1) // 2
            tail = keep // 2
            holder[key] = "\n".join(lines[:head] + [f"... {len(lines) - keep} lines omitted ..."] + lines[-tail:])
        if variant == "evidence":
            payload["source"]["complete"] = False
        elif variant == "task":
            payload["source"]["complete"] = False
        _append_omission(metadata, "source")
        changed = True
    return changed


def _drop_optional(payload: dict, variant: str, metadata: dict) -> bool:
    if variant == "minimal":
        relations = payload["result"].get("relations", [])
        if relations:
            return _drop_relations(payload, variant, metadata)
        paths = payload["result"].get("paths", [])
        if paths:
            return _drop_paths(payload, variant, metadata)
        target = payload["result"].get("target")
        if target and target.get("source"):
            target["source"] = ""
            _append_omission(metadata, "source")
            return True
    elif variant == "evidence":
        if payload.get("next_action"):
            payload.pop("next_action")
            _append_omission(metadata, "next_action")
            return True
        if payload.get("relations"):
            return _drop_relations(payload, variant, metadata)
        if payload.get("paths"):
            return _drop_paths(payload, variant, metadata)
        if payload.get("evidence"):
            payload["evidence"].pop()
            _append_omission(metadata, "evidence")
            return True
        target_entities = [entity for entity in payload.get("entities", []) if "target" in entity.get("roles", [])]
        if len(payload.get("entities", [])) > len(target_entities):
            payload["entities"] = target_entities
            _append_omission(metadata, "entities")
            return True
        if _shrink_source(payload, variant, metadata):
            return True
    elif variant == "task":
        if payload.get("next_action"):
            payload.pop("next_action")
            _append_omission(metadata, "next_action")
            return True
        if payload.get("relations", {}).get("items"):
            return _drop_relations(payload, variant, metadata)
        if payload.get("paths", {}).get("items"):
            return _drop_paths(payload, variant, metadata)
        if _shrink_source(payload, variant, metadata):
            return True
        if payload.get("summary"):
            payload["summary"] = ""
            _append_omission(metadata, "summary")
            return True
    else:
        if payload.get("attention"):
            payload["attention"] = []
            _append_omission(metadata, "attention")
            return True
        if payload.get("relations"):
            return _drop_relations(payload, variant, metadata)
        if payload.get("paths"):
            payload["paths"].pop()
            _append_omission(metadata, "paths")
            return True
        if _shrink_source(payload, variant, metadata):
            return True
        target = payload.get("target")
        if target and target.get("source"):
            target["source"] = ""
            _append_omission(metadata, "source")
            return True
    return False


def _render_json(
    report: dict,
    variant: str,
    limit: int,
    *,
    include_actions: bool = True,
) -> str:
    if variant == "minimal":
        payload = _minimal(report, include_actions=include_actions)
    elif variant == "evidence":
        payload = _evidence_variant(report)
    elif variant == "task":
        payload = _task(report)
    else:
        payload = _hybrid(report)
    payload = deepcopy(payload)
    metadata = _set_metadata(payload, variant, limit=limit, truncated=False)
    if isinstance(payload.get("status"), dict):
        payload["status"]["completeness"] = (
            "partial"
            if report.get("status") == "partial"
            or report.get("source", {}).get("complete") is False
            else "complete"
        )
    output = _encode(payload, metadata)
    if len(output.encode("utf-8")) <= limit:
        return output

    metadata["truncated"] = True
    if isinstance(payload.get("status"), dict):
        payload["status"]["completeness"] = "partial"
    while len(output.encode("utf-8")) > limit and _drop_optional(payload, variant, metadata):
        output = _encode(payload, metadata)
    if len(output.encode("utf-8")) > limit:
        # Keep a valid target envelope even when all optional facts are gone.
        if variant == "evidence":
            payload.pop("source", None)
            payload["evidence"] = []
            payload["relations"] = []
            payload["paths"] = []
        elif variant == "task":
            payload.pop("source", None)
            payload["relations"]["items"] = []
            payload["paths"]["items"] = []
        elif variant == "minimal":
            payload["result"].pop("paths", None)
            payload["result"].pop("relations", None)
            if payload["result"].get("target"):
                payload["result"]["target"]["source"] = ""
        else:
            payload["target"]["source"] = ""
            payload["relations"] = []
            payload["paths"] = []
        _append_omission(metadata, "details")
        output = _encode(payload, metadata)
    if len(output.encode("utf-8")) > limit:
        raise ValueError("variant envelope exceeds max_output_bytes")
    return output


def _text_omissions(report: dict) -> str:
    omitted = report.get("truncation", {}).get("omitted", {})
    if isinstance(omitted, dict):
        details = ", ".join(f"{key}={value}" for key, value in sorted(omitted.items()))
    else:
        details = ", ".join(str(item) for item in omitted)
    return f"OMITTED {details or 'none'}"


def _text_output(
    report: dict,
    variant: str,
    limit: int,
    *,
    include_actions: bool = True,
) -> str:
    reduced = deepcopy(report)
    if not include_actions:
        reduced["next_actions"] = []
    limit_report(reduced, limit)
    visible_relation_ids = {
        item["entity"]["id"]
        for direction in ("incoming", "outgoing")
        for item in reduced.get("relations", {}).get(direction, [])
    }
    reduced["next_actions"] = [
        action
        for action in reduced.get("next_actions", [])
        if _action_entity_id(action) in visible_relation_ids
    ]

    def render() -> str:
        body = f"VARIANT {variant}\n" + render_text(reduced)
        body = re.sub(r"OUTPUT bytes=\d+", "OUTPUT bytes=0", body)
        return f"{body}\n{_text_omissions(reduced)}"

    def with_actual_bytes(body: str) -> str:
        for _ in range(8):
            actual = len(body.encode("utf-8"))
            updated = re.sub(r"OUTPUT bytes=\d+", f"OUTPUT bytes={actual}", body)
            if updated == body:
                return body
            body = updated
        return body

    output = with_actual_bytes(render())
    if len(output.encode("utf-8")) <= limit:
        return output

    # Text is a human Adapter; preserve the target address and an honest cap even
    # when the normal report projection cannot fit in the requested budget.
    target = reduced.get("target", {})
    target_id = target.get("id", reduced.get("query", ""))
    reduced["source"] = {"complete": False, "text": ""}
    reduced["relations"] = {"analysis": "name_based_heuristic", "incoming": [], "outgoing": []}
    reduced["paths"] = []
    reduced["next_actions"] = []
    reduced.setdefault("truncation", {})["truncated"] = True
    reduced["truncation"]["omitted"] = {"details": 1}
    output = with_actual_bytes(render())
    if len(output.encode("utf-8")) <= limit:
        return output

    fallback = f"VARIANT {variant}\nSTATUS partial\nTARGET {target_id}\n{_text_omissions(reduced)}\nOUTPUT bytes=0 limit={limit} truncated=true"
    return with_actual_bytes(fallback)


def render_variant(
    report: dict,
    *,
    variant: str,
    max_output_bytes: int,
    output_format: str,
    include_actions: bool = True,
) -> str:
    """Render one experimental projection without mutating the source report."""
    if variant not in VARIANTS:
        raise ValueError(f"unsupported variant: {variant!r}")
    if max_output_bytes < 1024:
        raise ValueError("max_output_bytes must be at least 1024")
    if output_format == "text":
        return _text_output(
            report,
            variant,
            max_output_bytes,
            include_actions=include_actions,
        )
    if output_format != "json":
        raise ValueError("unsupported output_format; expected 'json' or 'text'")
    return _render_json(
        report,
        variant,
        max_output_bytes,
        include_actions=include_actions,
    )
