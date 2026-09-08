from codenav.cli import main


def test_impact_output_groups_entities_by_file(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "target.py").write_text(
        "def target():\n    return Zed.value + Alpha.value\n"
    )
    (root / "zed_one.py").write_text("class Zed:\n    value = 1\n")
    (root / "zed_two.py").write_text("class Zed:\n    value = 2\n")
    (root / "alpha.py").write_text("class Alpha:\n    value = 3\n")
    (root / "user.py").write_text(
        "from target import target\n\n\ndef use():\n    return target()\n"
    )

    main(["impact", "target", "--root", str(root)])

    output = capsys.readouterr().out
    assert "impact chain for target:\n- depends-on:\n" in output
    assert "- dependents:\n" in output
    assert "project/" not in output
    assert "target.py" not in output
    assert output.count("Zed.value\n") == 1
    assert output.index("Alpha.value\n") < output.index("Zed.value\n")
    assert "use\n" in output
    assert str(tmp_path) not in output


def test_impact_detailed_output_includes_location_and_kind(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "target.py").write_text("class Target:\n    value = 1\n")
    (root / "user.py").write_text(
        "from target import Target\n\n\ndef use():\n    return Target.value\n"
    )

    main(["impact", "Target", "--root", str(root), "--detailed"])

    output = capsys.readouterr().out
    assert "project/user.py:4-5::use function" in output


def test_graph_reports_omitted_paths(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "graph.py").write_text(
        "def target():\n    return 1\n\n\n"
        "def first():\n    return target()\n\n\n"
        "def second():\n    return target()\n\n\n"
        "def third():\n    return target()\n"
    )

    main(["graph", "target", "--root", str(root), "--max-paths", "2"])

    output = capsys.readouterr().out
    assert "target:\nfirst -> target\nsecond -> target\n" in output
    assert "," not in output
    assert "not shown: 1 paths (max_paths=2)" in output


def test_graph_reports_successful_empty_result(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "graph.py").write_text("def target():\n    return 1\n")

    main(["graph", "target", "--root", str(root)])

    assert capsys.readouterr().out == "target: no influence data (0 paths)\n"
