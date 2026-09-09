import json
import textwrap

import pytest

from codenav.agent import (
    build_context,
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


def test_context_multi_root_keeps_paths_and_ids_distinct(tmp_path):
    project = tmp_path / "project"
    tests = tmp_path / "tests"
    for d in (project, tests):
        d.mkdir()
    (project / "core.py").write_text("def dup():\n    return 1\n")
    (tests / "core.py").write_text("def dup():\n    return 2\n")

    report = build_context([str(project), str(tests)], name="dup")

    assert report["status"] == "ambiguous"
    records = report["candidates"]
    assert len(records) == 2
    assert {r["location"]["path"] for r in records} == {
        "project/core.py",
        "tests/core.py",
    }
    assert len({r["id"] for r in records}) == 2
    for action in report["next_actions"]:
        assert action["argv"][-2:] == [str(project), str(tests)]


def test_cli_context_exact_id_round_trip_in_json_and_text(tmp_path, capsys):
    root = make_repo(tmp_path)
    main(["context", "my_func", "--root", root])
    report = json.loads(capsys.readouterr().out)

    main(["context", "--id", report["target"]["id"], "--root", root, "--format", "text"])
    rendered = capsys.readouterr().out

    assert f"id: {report['target']['id']}" in rendered
    assert "SOURCE complete=true" in rendered
    assert "RELATIONS analysis=name_based_heuristic" in rendered


@pytest.mark.parametrize("argv", [["context"], ["context", "my_func", "--id", "e:x.py:1:x"]])
def test_context_selector_misuse_is_usage_error(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
