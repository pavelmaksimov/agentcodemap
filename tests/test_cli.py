import textwrap

import pytest

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


def _py_module(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_outline_orders_modules_root_first_and_reports_cap(tmp_path, capsys):
    from codenav.outline import module_name

    root = tmp_path / "project"
    _py_module(root, "top.py", "def top():\n    return 1\n")
    _py_module(root, "deep/a.py", "def deep_a():\n    return 1\n")
    for i in range(400):
        _py_module(root, f"deep/m{i:03}.py", f"def fn{i}():\n    return {i}\n")

    main(["outline", str(root), "--max-chars", "2000"])

    out = capsys.readouterr().out
    assert "not shown:" in out
    body = out.split("\nnot shown:")[0]
    assert body.startswith(module_name(str(root / "top.py")) + ":")
    assert len(body) <= 2000


def test_outline_high_cap_shows_all_modules_in_order(tmp_path, capsys):
    from codenav.outline import module_name

    root = tmp_path / "project"
    _py_module(root, "zz_top.py", "def top():\n    return 1\n")
    _py_module(root, "aa_nested/m.py", "def n():\n    return 1\n")

    main(["outline", str(root), "--max-chars", "100000"])

    out = capsys.readouterr().out
    assert "not shown:" not in out
    top_hdr = module_name(str(root / "zz_top.py")) + ":"
    nested_hdr = module_name(str(root / "aa_nested" / "m.py")) + ":"
    assert out.index(top_hdr) < out.index(nested_hdr)


def test_outline_filter_keeps_only_matching_module_paths(tmp_path, capsys):
    root = tmp_path / "project"
    for d in ("schemas", "services", "models"):
        _py_module(root, f"{d}/x.py", "def x():\n    return 1\n")

    main(["outline", str(root), "--filter", "schemas|services"])

    out = capsys.readouterr().out
    assert "schemas" in out
    assert "services" in out
    assert "models" not in out
    assert "not shown:" not in out


def test_outline_repeated_filters_are_or_ed(tmp_path, capsys):
    root = tmp_path / "project"
    for d in ("schemas", "services", "models"):
        _py_module(root, f"{d}/x.py", "def x():\n    return 1\n")

    main(["outline", str(root), "--filter", "schemas", "--filter", "models"])

    out = capsys.readouterr().out
    assert "schemas" in out
    assert "models" in out
    assert "services" not in out


def test_outline_no_match_is_successful_empty_result(tmp_path, capsys):
    root = tmp_path / "project"
    _py_module(root, "a.py", "def a():\n    return 1\n")

    main(["outline", str(root), "--filter", "zzz"])

    assert capsys.readouterr().out == "(no modules match: zzz)\n"


def test_outline_empty_project_is_successful_empty_result(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()

    main(["outline", str(root)])

    assert capsys.readouterr().out == "(no modules found)\n"


def test_outline_single_module_output_shape_unchanged(tmp_path, capsys):
    from codenav.outline import module_name

    path = _py_module(
        tmp_path,
        "m.py",
        "MY_MODULE_ATTR = 1\n\n\n"
        "def my_func():\n    return MY_MODULE_ATTR\n\n\n"
        "class MyClass:\n    my_attr = 2\n\n"
        "    def my_method(self):\n        return self.my_attr\n",
    )

    main(["outline", str(path)])

    expected = (
        f"{module_name(str(path))}:\n"
        "A MY_MODULE_ATTR\n"
        "\n"
        "F my_func\n"
        "\n"
        "C MyClass\n"
        " A my_attr\n"
        " M my_method\n"
        "\n"
    )
    assert capsys.readouterr().out == expected


def test_outline_filter_accepts_several_values_in_one_flag(tmp_path, capsys):
    root = tmp_path / "project"
    for d in ("schemas", "services", "models"):
        _py_module(root, f"{d}/x.py", "def x():\n    return 1\n")

    main(["outline", str(root), "--filter", "schemas", "models"])

    out = capsys.readouterr().out
    assert "schemas" in out
    assert "models" in out
    assert "services" not in out


def _three_symbol_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "alpha.py").write_text("def alpha():\n    return 1\n")
    (root / "beta.py").write_text("def beta():\n    return alpha()\n")
    (root / "gamma.py").write_text("def gamma():\n    return 1\n")
    return root


def test_symbol_accepts_several_names(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["symbol", "alpha", "beta", "--root", str(root)])

    out = capsys.readouterr().out
    assert "### alpha" in out
    assert "### beta" in out
    assert "### gamma" not in out


def test_symbol_with_any_missing_name_aborts_without_output(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["symbol", "alpha", "no_such", "--root", str(root)])

    assert "not found" in str(exc.value.code)
    assert capsys.readouterr().out == ""


def test_impact_accepts_several_names(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["impact", "alpha", "gamma", "--root", str(root)])

    out = capsys.readouterr().out
    assert "impact chain for alpha:\n" in out
    assert "impact chain for gamma:\n" in out
    assert "beta\n" in out  # alpha's only dependent


def test_graph_accepts_several_names(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["graph", "beta", "gamma", "--root", str(root)])

    out = capsys.readouterr().out
    assert "beta:\nbeta -> alpha\n" in out  # beta references alpha
    assert "gamma: no influence data (0 paths)\n" in out


def _sibling_roots(tmp_path):
    project = tmp_path / "project"
    tests = tmp_path / "tests"
    other = tmp_path / "other"
    for d in (project, tests, other):
        d.mkdir()
    (project / "target.py").write_text("def target():\n    return 1\n")
    (project / "user.py").write_text(
        "from target import target\n\n\ndef use():\n    return target()\n"
    )
    (tests / "test_user.py").write_text(
        "def test_use():\n    return target()\n"
    )
    (other / "noise.py").write_text(
        "def other_user():\n    return target()\n"
    )
    return project, tests, other


def test_impact_two_roots_exclude_sibling_directory(tmp_path, capsys):
    project, tests, other = _sibling_roots(tmp_path)

    main(
        [
            "impact",
            "target",
            "--root",
            str(project),
            str(tests),
            "--detailed",
        ]
    )

    out = capsys.readouterr().out
    assert "project/user.py:" in out
    assert "tests/test_user.py:" in out
    assert "other_user" not in out
    assert "noise" not in out


def test_symbol_indexed_only_within_listed_roots(tmp_path, capsys):
    project, tests, other = _sibling_roots(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["symbol", "other_user", "--root", str(project), str(tests)])
    assert "not found" in str(exc.value.code)

    main(["symbol", "target", "--root", str(project), str(tests)])
    assert "### target" in capsys.readouterr().out


def _grep_sample_root(tmp_path):
    (tmp_path / "m.py").write_text(
        textwrap.dedent(
            """\
            import json


            def loader(path):
                return json.load(open(path))


            class Saver:
                def save(self, data):
                    json.dump(data, open("out.json", "w"))

                def other(self):
                    return 42
            """
        )
    )
    return tmp_path


def test_grep_defaults_to_full_symbol_source(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", r"\bjson\b", "--root", str(root)])

    out = capsys.readouterr().out
    assert "def loader(path):" in out
    assert "def save(self, data):" in out
    assert "1\timport json" in out  # module-level lines stay matched-only
    assert "def other" not in out
    assert out.count("def loader(path):") == 1
    assert out.count("def save(self, data):") == 1


def test_grep_match_only_prints_only_matched_lines(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", r"\bjson\b", "--root", str(root), "--match-only"])

    out = capsys.readouterr().out
    assert "def loader(path):" not in out
    assert "def save(self, data):" not in out
    assert "return json.load(open(path))" in out
    assert 'json.dump(data, open("out.json", "w"))' in out
    assert "1\timport json" in out


def test_grep_several_patterns_print_matching_symbol_once(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", r"\bjson\b", "open", "--root", str(root)])

    out = capsys.readouterr().out
    assert out.count("def loader(path):") == 1
    assert out.count("def save(self, data):") == 1
    assert out.count("---") == 2  # loader, save, module-level import
