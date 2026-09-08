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
