import json
import textwrap

import pytest

from codenav.agent import (
    build_context,
    build_expand,
    build_read,
    build_select,
    encode_json,
    limit_report,
)
from codenav.cli import main


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


def make_repo(tmp_path):
    (tmp_path / "chain.py").write_text(CHAIN)
    return str(tmp_path)


def test_eager_context_combines_source_relations_and_paths(tmp_path):
    report = build_context(make_repo(tmp_path), name="my_func")

    assert report["workflow"] == "eager_context"
    assert report["target"]["qualified_name"] == "my_func"
    assert "def my_func" in report["source"]["text"]
    assert {item["entity"]["name"] for item in report["relations"]["incoming"]} == {"top"}
    assert {item["entity"]["name"] for item in report["relations"]["outgoing"]} == {"mid"}
    assert report["relations"]["incoming"][0]["evidence"][0]["line"] == 14
    assert any("my_func" in path["labels"] for path in report["paths"])


def test_progressive_workflow_splits_selection_source_and_expansion(tmp_path):
    root = make_repo(tmp_path)
    selected = build_select(root, "my_func")
    entity_id = selected["target"]["id"]
    read = build_read(root, entity_id)
    expanded = build_expand(root, entity_id)

    assert selected["workflow"] == "progressive_select"
    assert "source" not in selected and "relations" not in selected
    assert read["workflow"] == "progressive_read"
    assert "def my_func" in read["source"]["text"] and "relations" not in read
    assert expanded["workflow"] == "progressive_expand"
    assert "relations" in expanded and "source" not in expanded


def test_ambiguous_name_requires_exact_id(tmp_path):
    (tmp_path / "a.py").write_text("def handler():\n    return 1\n")
    (tmp_path / "b.py").write_text("def handler():\n    return 2\n")
    root = str(tmp_path)

    report = build_context(root, name="handler")
    assert report["status"] == "ambiguous"
    assert len(report["candidates"]) == 2
    assert "target" not in report

    exact = build_context(root, exact_id=report["candidates"][1]["id"])
    assert exact["status"] == "ok"
    assert exact["target"]["location"]["path"] == "b.py"


def test_output_budget_keeps_valid_json(tmp_path):
    report = limit_report(build_context(make_repo(tmp_path), name="my_func"), 1024)
    encoded = encode_json(report)

    assert len(encoded.encode()) <= 1024
    assert report["truncation"]["truncated"] is True
    assert report["truncation"]["output_bytes"] == len(encoded.encode())
    assert json.loads(encoded)["status"] == "partial"


def test_cli_progressive_round_trip(tmp_path, capsys):
    root = make_repo(tmp_path)
    main(["select", "my_func", "--root", root])
    selected = json.loads(capsys.readouterr().out)

    main(["read", selected["target"]["id"], "--root", root])
    read = json.loads(capsys.readouterr().out)
    main(["expand", selected["target"]["id"], "--root", root])
    expanded = json.loads(capsys.readouterr().out)

    assert read["source"]["complete"] is True
    assert expanded["relations"]["analysis"] == "name_based_heuristic"


@pytest.mark.parametrize("argv", [["context"], ["context", "my_func", "--id", "e:x.py:1:x"]])
def test_context_selector_misuse_is_usage_error(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
