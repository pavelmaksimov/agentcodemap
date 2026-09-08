import json
import re
import textwrap
from pathlib import Path

import pytest

from codenav.agent import build_context
from experiments.context_output_variants import VARIANTS, render_variant


CHAIN = textwrap.dedent(
    """\
    def base():
        return 1


    def mid():
        return base()


    def my_func():
        return mid()


    def top():
        return my_func()
    """
)


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _values_for_key(value, wanted):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == wanted:
                yield item
            yield from _values_for_key(item, wanted)
    elif isinstance(value, list):
        for item in value:
            yield from _values_for_key(item, wanted)


@pytest.mark.parametrize("variant", VARIANTS)
def test_json_variant_preserves_context_facts(tmp_path, variant):
    (tmp_path / "chain.py").write_text(CHAIN)
    report = build_context(str(tmp_path), name="my_func")

    output = render_variant(report, variant=variant, max_output_bytes=16384, output_format="json")
    payload = json.loads(output)
    strings = set(_strings(payload))

    assert payload["schema"] == "codenav.context/v2"
    assert report["target"]["id"] in strings
    assert report["source"]["text"] in strings
    assert report["relations"]["incoming"][0]["entity"]["id"] in strings
    assert report["relations"]["incoming"][0]["evidence"][0]["text"] in strings
    assert set(report["paths"][0]["entity_ids"]) <= strings


@pytest.mark.parametrize("variant", VARIANTS)
def test_json_variant_reports_its_actual_bytes_under_cap(tmp_path, variant):
    (tmp_path / "chain.py").write_text(CHAIN)
    report = build_context(str(tmp_path), name="my_func")

    output = render_variant(report, variant=variant, max_output_bytes=1024, output_format="json")
    payload = json.loads(output)
    actual_bytes = len(output.encode())

    assert actual_bytes <= 1024
    assert list(_values_for_key(payload, "output_bytes")) == [actual_bytes]
    assert list(_values_for_key(payload, "truncated")) == [True]
    assert report["target"]["id"] in set(_strings(payload))


@pytest.mark.parametrize("variant", VARIANTS)
def test_text_variant_preserves_agent_facts_and_reports_text_bytes(tmp_path, variant):
    (tmp_path / "chain.py").write_text(CHAIN)
    report = build_context(str(tmp_path), name="my_func")

    output = render_variant(report, variant=variant, max_output_bytes=16384, output_format="text")
    actual_bytes = len(output.encode())
    reported_bytes = int(re.search(r"OUTPUT bytes=(\d+)", output).group(1))

    assert report["target"]["id"] in output
    assert report["source"]["text"] in output
    assert report["relations"]["incoming"][0]["evidence"][0]["text"] in output
    assert reported_bytes == actual_bytes


@pytest.mark.parametrize("variant", VARIANTS)
def test_text_variant_respects_byte_cap(tmp_path, variant):
    (tmp_path / "chain.py").write_text(CHAIN)
    report = build_context(str(tmp_path), name="my_func")

    output = render_variant(report, variant=variant, max_output_bytes=1024, output_format="text")
    assert len(output.encode()) <= 1024
    assert "truncated=true" in output
    assert "omitted" in output.lower()


@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_preserves_ambiguous_candidates_and_not_found_status(tmp_path, variant):
    (tmp_path / "a.py").write_text("def handler():\n    return 1\n")
    (tmp_path / "b.py").write_text("def handler():\n    return 2\n")
    ambiguous = build_context(str(tmp_path), name="handler")
    missing = build_context(str(tmp_path), name="missing")

    ambiguous_payload = json.loads(
        render_variant(ambiguous, variant=variant, max_output_bytes=16384, output_format="json")
    )
    missing_payload = json.loads(
        render_variant(missing, variant=variant, max_output_bytes=16384, output_format="json")
    )
    ambiguous_strings = set(_strings(ambiguous_payload))
    missing_strings = set(_strings(missing_payload))

    assert "ambiguous" in ambiguous_strings
    assert "not_found" in missing_strings
    for candidate in ambiguous["candidates"]:
        assert candidate["id"] in ambiguous_strings
        assert candidate["preview"] in ambiguous_strings


@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_truncation_has_no_dangling_action_or_evidence_refs(tmp_path, variant):
    (tmp_path / "chain.py").write_text(CHAIN)
    report = build_context(str(tmp_path), name="my_func")
    payload = json.loads(
        render_variant(report, variant=variant, max_output_bytes=1024, output_format="json")
    )

    if variant == "minimal":
        relations = payload["result"].get("relations", [])
        for relation in relations:
            if "argv" in relation:
                assert relation["argv"][relation["argv"].index("--id") + 1] == relation["entity_id"]
    elif variant == "evidence":
        relation_ids = {item["id"] for item in payload.get("relations", [])}
        evidence_ids = {item["id"] for item in payload.get("evidence", [])}
        for relation in payload.get("relations", []):
            assert set(relation.get("evidence_ids", [])) <= evidence_ids
        for path in payload.get("paths", []):
            assert set(path.get("relation_ids", [])) <= relation_ids
        if "next_action" in payload:
            assert payload["next_action"]["based_on_relation_id"] in relation_ids
    elif variant == "task":
        items = payload["relations"]["items"]
        action = payload.get("next_action")
        if action is not None:
            assert 0 <= action["relation_index"] < len(items)
    else:
        relation_ids = {item["id"] for item in payload.get("relations", [])}
        assert {item["relation_id"] for item in payload.get("attention", [])} <= relation_ids


PROJECT_SYMBOLS = ("_parse_lines_spec", "render_outline", "slice_diff", "RepoIndex", "cmd_diff")


@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_matrix_is_deterministic_and_bounded_on_project_symbols(variant):
    root = str(Path(__file__).parents[1] / "src" / "codenav")
    for name in PROJECT_SYMBOLS:
        report = build_context(root, name=name)
        before = json.dumps(report, sort_keys=True)
        first = render_variant(report, variant=variant, max_output_bytes=16384, output_format="json")
        second = render_variant(report, variant=variant, max_output_bytes=16384, output_format="json")
        payload = json.loads(first)
        actual_bytes = len(first.encode())

        assert first == second
        assert json.dumps(report, sort_keys=True) == before
        assert actual_bytes <= 16384
        assert list(_values_for_key(payload, "output_bytes")) == [actual_bytes]


@pytest.mark.parametrize("variant", VARIANTS)
def test_text_variant_matrix_is_bounded_on_project_symbols(variant):
    root = str(Path(__file__).parents[1] / "src" / "codenav")
    for name in PROJECT_SYMBOLS:
        report = build_context(root, name=name)
        output = render_variant(report, variant=variant, max_output_bytes=16384, output_format="text")

        assert len(output.encode()) <= 16384
        assert report["target"]["id"] in output
        assert re.search(r"OUTPUT bytes=\d+ limit=16384 truncated=(?:true|false)", output)


@pytest.mark.parametrize("variant", VARIANTS)
def test_large_project_symbol_stays_bounded_in_both_adapters(variant):
    root = str(Path(__file__).parents[1] / "src" / "codenav")
    report = build_context(root, name="RepoIndex")

    json_output = render_variant(report, variant=variant, max_output_bytes=1024, output_format="json")
    text_output = render_variant(report, variant=variant, max_output_bytes=1024, output_format="text")
    payload = json.loads(json_output)

    assert len(json_output.encode()) <= 1024
    assert len(text_output.encode()) <= 1024
    assert report["target"]["id"] in set(_strings(payload))
    assert "truncated=true" in text_output
    assert "omitted" in text_output.lower()


@pytest.mark.parametrize("variant", VARIANTS)
def test_text_cap_does_not_keep_actions_for_hidden_relations(variant):
    root = str(Path(__file__).parents[1] / "src" / "codenav")
    report = build_context(root, name="RepoIndex")

    output = render_variant(report, variant=variant, max_output_bytes=1024, output_format="text")

    assert "OMITTED" in output
    assert "NEXT" not in output


@pytest.mark.parametrize("variant", ("evidence", "hybrid"))
def test_structured_status_marks_incomplete_variant_payload(variant):
    root = str(Path(__file__).parents[1] / "src" / "codenav")
    report = build_context(root, name="RepoIndex")

    payload = json.loads(
        render_variant(report, variant=variant, max_output_bytes=1024, output_format="json")
    )

    assert payload["status"]["outcome"] == "resolved"
    assert payload["status"]["completeness"] == "partial"


def test_minimal_action_ablation_changes_only_follow_up_guidance(tmp_path):
    (tmp_path / "chain.py").write_text(CHAIN)
    report = build_context(str(tmp_path), name="my_func")

    with_action = json.loads(
        render_variant(
            report,
            variant="minimal",
            max_output_bytes=16384,
            output_format="json",
            include_actions=True,
        )
    )
    without_action = json.loads(
        render_variant(
            report,
            variant="minimal",
            max_output_bytes=16384,
            output_format="json",
            include_actions=False,
        )
    )
    text_with_action = render_variant(
        report,
        variant="minimal",
        max_output_bytes=16384,
        output_format="text",
        include_actions=True,
    )
    text_without_action = render_variant(
        report,
        variant="minimal",
        max_output_bytes=16384,
        output_format="text",
        include_actions=False,
    )

    assert list(_values_for_key(with_action, "argv"))
    assert list(_values_for_key(without_action, "argv")) == []
    assert "NEXT" in text_with_action
    assert "NEXT" not in text_without_action
    assert with_action["result"]["target"]["source"] == without_action["result"]["target"]["source"]
