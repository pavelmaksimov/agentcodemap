import signal
import subprocess
import sys
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
    assert output.count("Zed.value [ref]\n") == 1
    assert output.index("Alpha.value [ref]\n") < output.index("Zed.value [ref]\n")
    assert "use [call,ret]\n" in output
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
    assert "project/user.py:4-5::use function  [ret@5]" in output


def test_graph_reports_omitted_paths(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "graph.py").write_text(
        "def target():\n    return 1\n\n\n"
        "def first():\n    return target()\n\n\n"
        "def second():\n    return target()\n\n\n"
        "def third():\n    return target()\n"
    )

    main(["trace", "target", "--root", str(root), "--max-paths", "2"])

    output = capsys.readouterr().out
    assert "target:\nfirst -[call,ret]-> target\nsecond -[call,ret]-> target\n" in output
    assert "not shown: 1 paths (max_paths=2)" in output


def test_graph_reports_successful_empty_result(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "graph.py").write_text("def target():\n    return 1\n")

    main(["trace", "target", "--root", str(root)])

    assert capsys.readouterr().out == "target: no influence data (0 paths)\n"


def _py_module(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_outline_orders_modules_root_first_and_reports_pages(tmp_path, capsys):
    from codenav.outline import module_name

    root = tmp_path / "project"
    _py_module(root, "top.py", "def top():\n    return 1\n")
    _py_module(root, "deep/a.py", "def deep_a():\n    return 1\n")
    for i in range(400):
        _py_module(root, f"deep/m{i:03}.py", f"def fn{i}():\n    return {i}\n")

    main(["outline", str(root), "--max-chars", "2000"])

    out = capsys.readouterr().out
    assert "(page 1 of" in out
    body = out.split("\n(page ")[0]
    assert body.startswith(module_name(str(root / "top.py")) + ":")
    assert len(body) <= 2000
    assert "not shown:" not in out


def _many_module_root(tmp_path, count=40):
    root = tmp_path / "project"
    for i in range(count):
        _py_module(root, f"m{i:03}.py", f"def fn{i}():\n    return {i}\n")
    return root


def _outline_page_one_line(out: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith("(page 1 of "))


def test_outline_default_reports_total_pages_and_remaining_hint(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    main(["outline", str(root), "--max-chars", "500"])

    out = capsys.readouterr().out
    line = _outline_page_one_line(out)
    total = int(line.split("of", 1)[1].split(";", 1)[0])
    assert total > 1
    assert line == f"(page 1 of {total}; {total - 1} more: --pages 2-{total})"
    # page 1 carries the first module; nothing claims truncation anymore
    assert "F fn0" in out
    assert "not shown:" not in out


def test_outline_pages_fetches_the_next_page(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    main(["outline", str(root), "--max-chars", "500"])
    total = int(_outline_page_one_line(capsys.readouterr().out).split("of", 1)[1].split(";", 1)[0])
    assert total > 2

    main(["outline", str(root), "--max-chars", "500", "--pages", "2"])

    out = capsys.readouterr().out
    assert "F fn0" not in out  # page 1 modules are not repeated
    assert f"(page 2 of {total}; {total - 2} more: --pages 3-{total})" in out


def test_outline_pages_union_of_specs_and_ranges(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    main(["outline", str(root), "--max-chars", "500", "--pages", "1,2"])

    out = capsys.readouterr().out
    assert "(page 1 of" in out
    assert "(page 2 of" in out
    assert out.index("(page 1 of") < out.index("(page 2 of")
    assert "F fn0" in out


def test_outline_pages_repeatable_flag_unions(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    main(["outline", str(root), "--max-chars", "500", "--pages", "2", "--pages", "1"])

    out = capsys.readouterr().out
    assert "(page 1 of" in out
    assert "(page 2 of" in out


def test_outline_pages_last_page_has_no_remaining_hint(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    main(["outline", str(root), "--max-chars", "500"])
    total = int(_outline_page_one_line(capsys.readouterr().out).split("of", 1)[1].split(";", 1)[0])

    main(["outline", str(root), "--max-chars", "500", "--pages", str(total)])

    out = capsys.readouterr().out
    assert f"(page {total} of {total})\n" in out
    assert "more: --pages" not in out


def test_outline_pages_out_of_range_is_an_error(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["outline", str(root), "--max-chars", "500", "--pages", "99"])

    assert "99 out of range: outline has" in str(exc.value.code)


def test_outline_pages_invalid_spec_is_an_error(tmp_path, capsys):
    root = _many_module_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["outline", str(root), "--pages", "x"])

    assert "invalid --pages spec 'x'" in str(exc.value.code)


def test_outline_oversized_module_reports_truncation(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    lines = []
    for i in range(300):
        lines.append(f"def f{i}():\n    return {i}\n\n\n")
    _py_module(root, "big.py", "".join(lines))

    main(["outline", str(root / "big.py"), "--max-chars", "1000"])

    out = capsys.readouterr().out
    assert "not fully shown:" in out
    assert "big.py" in out
    assert "larger than page size 1000" in out
    assert "raise --max-chars" in out
    assert "not shown:" not in out


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
        " -> MY_MODULE_ATTR [ret]\n"
        "\n"
        "C MyClass\n"
        " A my_attr\n"
        " M my_method\n"
        "  -> MyClass.my_attr [ret]\n"
        "\n"
    )
    assert capsys.readouterr().out == expected


def test_outline_no_deps_flag_prints_one_line_per_symbol(tmp_path, capsys):
    from codenav.outline import module_name

    path = _py_module(
        tmp_path,
        "m.py",
        "MY_MODULE_ATTR = 1\n\n\n"
        "def my_func():\n    return MY_MODULE_ATTR\n\n\n"
        "class MyClass:\n    my_attr = 2\n\n"
        "    def my_method(self):\n        return self.my_attr\n",
    )

    main(["outline", str(path), "--no-deps"])

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


def test_outline_top_level_flag_prints_module_roots_only(tmp_path, capsys):
    from codenav.outline import module_name

    path = _py_module(
        tmp_path,
        "m.py",
        "MY_MODULE_ATTR = 1\n\n\n"
        "def my_func():\n    return MY_MODULE_ATTR\n\n\n"
        "class MyClass:\n    my_attr = 2\n\n"
        "    def my_method(self):\n        return self.my_attr\n",
    )

    main(["outline", str(path), "--top-level"])

    expected = (
        f"{module_name(str(path))}:\n"
        "A MY_MODULE_ATTR\n"
        "\n"
        "F my_func\n"
        " -> MY_MODULE_ATTR [ret]\n"
        "\n"
        "C MyClass\n"
        "\n"
    )
    assert capsys.readouterr().out == expected


def test_outline_deps_lists_each_symbols_own_references(tmp_path, capsys):
    root = tmp_path / "project"
    _py_module(root, "alpha.py", "def alpha():\n    return 1\n")
    path = _py_module(
        root,
        "beta.py",
        "from alpha import alpha\n\n\n"
        "def beta():\n    return alpha()\n\n\n"
        "class C:\n"
        "    def m(self):\n        return alpha()\n",
    )

    main(["outline", str(path), "--deps"])

    out = capsys.readouterr().out
    # cross-file resolution: alpha lives in its own module
    assert "F beta\n -> alpha [call,ret]\n" in out
    # a reference belongs to its innermost symbol: the class does not repeat
    # what its method already shows
    assert "C C\n M m\n  -> alpha [call,ret]\n" in out
    assert "C C\n -> " not in out


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
    assert "beta [call,ret]\n" in out  # alpha's only dependent


def test_graph_accepts_several_names(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["trace", "beta", "gamma", "--root", str(root)])

    out = capsys.readouterr().out
    assert "beta:\nbeta -[call,ret]-> alpha\n" in out  # beta references alpha
    assert "gamma: no influence data (0 paths)\n" in out


def test_graph_labels_edges_with_the_relation_kind(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "mod.py").write_text(
        "def target():\n    return 1\n\n\n"
        "class Holder:\n    attr: target = None\n"
    )

    main(["trace", "target", "--root", str(root), "--depth", "2"])

    out = capsys.readouterr().out
    # Holder points at target through a field annotation (par), not a call
    assert "Holder -[par]-> target\n" in out


def _kind_mix_root(tmp_path):
    """Base referenced by an inheriting class, by an annotation, and by calls."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "kinds.py").write_text(
        textwrap.dedent(
            """\
            class Base:
                pass


            class Child(Base):
                dep: Base = None


            def make():
                return Base()


            def look():
                return Base
            """
        )
    )
    return root


def test_impact_kind_filter_takes_several_kinds(tmp_path, capsys):
    root = _kind_mix_root(tmp_path)

    main(["impact", "Base", "--root", str(root), "--kind", "call", "param"])

    out = capsys.readouterr().out
    # the ret site of make is trimmed away: only selected kinds are printed
    assert "make [call]\n" in out
    # Child is kept, but only through the param site the filter selected
    assert "Child [par]\n" in out
    assert "look" not in out

    main(["impact", "Base", "--root", str(root), "--kind", "call", "--kind", "return"])

    out = capsys.readouterr().out
    # both selected kinds of make survive: it calls Base and returns it
    assert "make [call,ret]\n" in out
    assert "look [ret]\n" in out
    assert "Child" not in out

    # depends-on follows the same rule: nothing make holds is param-typed
    main(["impact", "make", "--root", str(root), "--kind", "param"])

    out = capsys.readouterr().out
    assert "make:\n- depends-on:\n(none found)\n" in out
    assert "Base" not in out


def test_graph_kind_filter_drops_edges_of_other_kinds(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "chain.py").write_text(
        "class Base:\n    pass\n\n\ndef mid(dep: Base):\n    pass\n\n\ndef top():\n    return mid()\n"
    )

    main(["trace", "Base", "--root", str(root), "--depth", "3"])

    assert "top -[call,ret]-> mid -[par]-> Base\n" in capsys.readouterr().out

    # the only edge into Base is a param, so a call-only graph has no paths
    main(["trace", "Base", "--root", str(root), "--depth", "3", "--kind", "call"])

    assert capsys.readouterr().out == "Base: no influence data (0 paths)\n"

    main(["trace", "Base", "--root", str(root), "--depth", "3", "--kind", "param"])

    out = capsys.readouterr().out
    assert "mid -[par]-> Base\n" in out
    assert "top" not in out  # top -> mid is a call edge, filtered out mid-chain


def test_info_kind_filter_applies_to_graph_and_impact(tmp_path, capsys):
    root = _kind_mix_root(tmp_path)

    main(["info", "Base", "--root", str(root), "--kind", "call"])

    out = capsys.readouterr().out
    assert "make -[call]-> Base\n" in out
    assert "make [call]\n" in out
    assert "Child" not in out and "par" not in out


def test_unknown_kind_is_rejected(tmp_path):
    root = _kind_mix_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["impact", "Base", "--root", str(root), "--kind", "nope"])

    assert exc.value.code == 2


def test_kind_labels_are_short_and_their_short_forms_filter(tmp_path, capsys):
    root = _kind_mix_root(tmp_path)

    main(["impact", "Base", "--root", str(root)])

    out = capsys.readouterr().out
    assert "Child [inh,par]\n" in out  # field annotation + inheritance, sorted
    assert "param" not in out and "inheritance" not in out

    # the printed short form is accepted by --kind, and means the same
    main(["impact", "Base", "--root", str(root), "--kind", "par"])

    out = capsys.readouterr().out
    assert "Child [par]\n" in out
    assert "make" not in out


def test_graph_kind_filter_separates_producers_and_consumers(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "flow.py").write_text(
        textwrap.dedent(
            """\
            class Order:
                pass


            def make_order():
                return Order()


            def take_order(order: Order):
                return order
            """
        )
    )

    # ret keeps the unannotated producer (return Order()), drops the consumer
    main(["trace", "Order", "--root", str(root), "--kind", "ret"])

    out = capsys.readouterr().out
    assert "make_order -[ret]-> Order\n" in out
    assert "take_order" not in out

    # par keeps only the accepting side
    main(["trace", "Order", "--root", str(root), "--kind", "par"])

    out = capsys.readouterr().out
    assert "take_order -[par]-> Order\n" in out
    assert "make_order" not in out


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


def test_astgrep_prints_full_symbol_source(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["astgrep", r"\bjson\b", "--root", str(root)])

    out = capsys.readouterr().out
    assert "def loader(path):" in out
    assert "def save(self, data):" in out
    assert "1\timport json" in out  # module-level lines stay matched-only
    assert "def other" not in out
    assert out.count("def loader(path):") == 1
    assert out.count("def save(self, data):") == 1
    # the span header belongs to the short form; full source shows the lines
    assert "m.py:4-5::" not in out


def test_grep_prints_only_matched_lines(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", r"\bjson\b", "--root", str(root)])

    out = capsys.readouterr().out
    assert "def loader(path):" not in out
    assert "def save(self, data):" not in out
    assert "return json.load(open(path))" in out
    assert 'json.dump(data, open("out.json", "w"))' in out
    assert "1\timport json" in out


def test_grep_heads_each_block_with_symbol_span(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", r"\bjson\b", "--root", str(root)])

    out = capsys.readouterr().out
    assert "m.py:4-5::loader function" in out
    assert "m.py:9-10::Saver.save method" in out
    # module-level hit: no symbol to span, so the header stays a bare path
    assert f"{root / 'm.py'}\n" in out


def test_astgrep_several_patterns_print_matching_symbol_once(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["astgrep", r"\bjson\b", "open", "--root", str(root)])

    out = capsys.readouterr().out
    assert out.count("def loader(path):") == 1
    assert out.count("def save(self, data):") == 1
    assert out.count("---") == 2  # loader, save, module-level import


def _grep_three_blocks_root(tmp_path):
    # equal-sized functions: grep_symbols' size sort is stable, so the blocks
    # come out in source order (f0, f1, f2) and page numbers stay predictable
    (tmp_path / "m.py").write_text(
        "\n\n".join(f'def f{i}():\n    return "marker"' for i in range(3)) + "\n"
    )
    return tmp_path


def test_grep_default_prints_first_page_and_hints_at_the_rest(tmp_path, capsys):
    root = _grep_three_blocks_root(tmp_path)

    main(["grep", "marker", "--root", str(root), "--max-chars", "10"])

    out = capsys.readouterr().out
    assert "f0 function" in out
    assert "f1 function" not in out
    assert "f2 function" not in out
    assert "(page 1 of 3; 2 more: --pages 2-3)" in out


def test_grep_pages_fetches_another_page_without_repeating(tmp_path, capsys):
    root = _grep_three_blocks_root(tmp_path)

    main(
        ["grep", "marker", "--root", str(root), "--max-chars", "10", "--pages", "3"]
    )

    out = capsys.readouterr().out
    assert "f0 function" not in out  # page 1 blocks are not repeated
    assert "f2 function" in out
    assert "(page 3 of 3)" in out
    assert "more: --pages" not in out


def test_grep_pages_out_of_range_is_an_error(tmp_path, capsys):
    root = _grep_three_blocks_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "grep",
                "marker",
                "--root",
                str(root),
                "--max-chars",
                "10",
                "--pages",
                "99",
            ]
        )

    assert "99 out of range: grep has 3 page(s)" in str(exc.value.code)


def test_grep_pages_split_on_separator_and_never_cut_a_block():
    from codenav.cli import _grep_pages

    blocks = ["aaaaa", "bbbbb", "ccccc"]
    # two blocks + separator: 5 + 5 + 5 = 15 fits, the third needs 5 more
    assert _grep_pages(blocks, 15) == ["aaaaa\n---\nbbbbb", "ccccc"]
    assert _grep_pages(blocks, 10_000) == ["aaaaa\n---\nbbbbb\n---\nccccc"]
    # a block over the limit owns a page whole: hits are never truncated
    assert _grep_pages(["x" * 50], 10) == ["x" * 50]


def test_cli_dies_on_sigpipe_when_the_reader_closes_the_pipe(tmp_path):
    # more output than a pipe holds, so the CLI is still writing to it when the
    # reader goes away — the `codenav astgrep … | head` case
    body = "\n".join(f"    item_{i} = value + {i}" for i in range(6000))
    (tmp_path / "big.py").write_text(f"def big():\n{body}\n    return value\n")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from codenav.cli import main;"
            " main(['astgrep', sys.argv[1], '--root', sys.argv[2]])",
            "value",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first = proc.stdout.readline()
    proc.stdout.close()  # the reader is gone
    _, err = proc.communicate()

    assert b"big.py" in first  # output had started, the pipe is what ended it
    assert proc.returncode == -signal.SIGPIPE
    assert b"Traceback" not in err


def test_grep_no_match_is_successful_empty_result(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", "zzz", "--root", str(root)])

    assert capsys.readouterr().out == "(no matches for: 'zzz')\n"


def test_grep_no_match_lists_all_patterns(tmp_path, capsys):
    root = _grep_sample_root(tmp_path)

    main(["grep", "zzz", "nope", "--root", str(root)])

    assert capsys.readouterr().out == "(no matches for: 'zzz', 'nope')\n"


def test_grep_empty_project_is_successful_empty_result(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()

    main(["grep", "json", "--root", str(root)])

    assert capsys.readouterr().out == "(no code files found)\n"


def test_info_accumulates_symbol_graph_and_impact(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["info", "alpha", "--root", str(root)])

    out = capsys.readouterr().out
    assert "### alpha" in out  # symbol part: full source
    assert "1\tdef alpha():" in out
    assert "beta -[call,ret]-> alpha\n" in out  # graph part
    assert "impact chain for alpha:\n" in out  # impact part
    assert "- depends-on:\n(none found)\n" in out
    assert "- dependents:\nbeta [call,ret]\n" in out
    assert out.index("### alpha") < out.index("beta -[call,ret]-> alpha")
    assert out.index("beta -[call,ret]-> alpha") < out.index("impact chain for alpha:")


def test_info_reports_empty_parts_explicitly(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "isolated.py").write_text("def isolated():\n    return 1\n")

    main(["info", "isolated", "--root", str(root)])

    out = capsys.readouterr().out
    assert "### isolated" in out
    assert "isolated: no influence data (0 paths)\n" in out
    assert out.count("(none found)") == 2  # depends-on and dependents


def test_info_accepts_several_names(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["info", "alpha", "gamma", "--root", str(root)])

    out = capsys.readouterr().out
    assert "### alpha" in out
    assert "### gamma" in out
    assert "impact chain for alpha:" in out
    assert "impact chain for gamma:" in out


def test_info_with_any_missing_name_aborts_without_output(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["info", "alpha", "no_such", "--root", str(root)])

    assert "not found" in str(exc.value.code)
    assert capsys.readouterr().out == ""


def _deep_chain_root(tmp_path):
    """target with 20 referencing levels above and 20 referenced below."""
    root = tmp_path / "project"
    root.mkdir()
    lines = ["def target():\n    return d0()\n", "", ""]
    for i in range(20):
        ret = f"d{i + 1}()" if i < 19 else "1"
        lines.append(f"def d{i}():\n    return {ret}\n\n\n")
    lines.append("def c19():\n    return target()\n\n\n")
    for i in range(18, -1, -1):
        lines.append(f"def c{i}():\n    return c{i + 1}()\n\n\n")
    (root / "chain.py").write_text("".join(lines))
    return root


def test_info_graph_defaults_to_depth_20(tmp_path, capsys):
    root = _deep_chain_root(tmp_path)

    main(["info", "target", "--root", str(root)])

    out = capsys.readouterr().out
    chain_lines = [ln for ln in out.splitlines() if "->" in ln]
    # a 41-symbol path is trimmed to at most 20 symbols around the target
    assert chain_lines
    assert max(ln.count("->") for ln in chain_lines) == 19


def test_info_graph_defaults_to_max_50_paths(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    lines = ["def target():\n    return 1\n", "", ""]
    # 20 direct dependents (fanout cap) x 3 referrers each -> 60 distinct paths
    for i in range(20):
        lines.append(f"def user{i:02}():\n    return target()\n\n\n")
    for i in range(20):
        for k in range(3):
            lines.append(f"def fan{i:02}_{k}():\n    return user{i:02}()\n\n\n")
    (root / "users.py").write_text("".join(lines))

    main(["info", "target", "--root", str(root)])

    out = capsys.readouterr().out
    assert "not shown: 10 paths (max_paths=50)" in out
    assert out.count("]-> target") == 50  # 60 found, 50 shown


def test_trace_defaults_to_depth_3_and_target_first(tmp_path, capsys):
    root = _deep_chain_root(tmp_path)  # target -> d0 -> ... -> d19

    main(["trace", "target", "--root", str(root), "--direction", "down"])

    assert capsys.readouterr().out == (
        "target:\ntarget -[call,ret]-> d0 -[call,ret]-> d1\n"
    )


def test_trace_depth_truncates_from_the_target(tmp_path, capsys):
    root = _deep_chain_root(tmp_path)

    main(["trace", "target", "--root", str(root), "--depth", "7", "--direction", "down"])

    out = capsys.readouterr().out
    assert out == (
        "target:\ntarget -[call,ret]-> d0 -[call,ret]-> d1 -[call,ret]-> d2"
        " -[call,ret]-> d3 -[call,ret]-> d4 -[call,ret]-> d5\n"
    )


def test_trace_direction_up_shows_referrers_in_arrow_order(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "mod.py").write_text(
        "def base():\n    return 1\n\n\n"
        "def target():\n    return base()\n\n\n"
        "def caller():\n    return target()\n"
    )

    main(["trace", "target", "--root", str(root), "--direction", "up"])

    out = capsys.readouterr().out
    assert "caller -[call,ret]-> target\n" in out  # referrer first, kinds labelled
    assert "base" not in out  # downstream stays out


def test_graph_depth_limits_each_chain(tmp_path, capsys):
    root = _deep_chain_root(tmp_path)

    main(["trace", "target", "--root", str(root), "--depth", "2"])

    out = capsys.readouterr().out
    chain_lines = [ln for ln in out.splitlines() if "->" in ln]
    # a 41-symbol path collapses to a 2-symbol window around the target
    assert chain_lines
    assert all(ln.count("->") == 1 for ln in chain_lines)


def test_trace_isolated_symbol_is_explicit_empty_result(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "isolated.py").write_text("def isolated():\n    return 1\n")

    main(["trace", "isolated", "--root", str(root), "--direction", "down"])

    assert capsys.readouterr().out == "isolated: no dependency chains (0 paths)\n"


def test_trace_accepts_several_names(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["trace", "beta", "gamma", "--root", str(root), "--direction", "down"])

    out = capsys.readouterr().out
    assert "beta -[call,ret]-> alpha\n" in out
    assert "gamma: no dependency chains (0 paths)\n" in out


def test_trace_with_any_missing_name_aborts_without_output(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["trace", "alpha", "no_such", "--root", str(root)])

    assert "not found" in str(exc.value.code)
    assert capsys.readouterr().out == ""


def test_trace_reports_omitted_chains_at_max_paths(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    lines = [f"def leaf{i:02}():\n    return 1\n\n\n" for i in range(25)]
    calls = "\n".join(f"    out += leaf{i:02}()" for i in range(25))
    lines.append("def target():\n    out = 0\n" + calls + "\n    return out\n")
    (root / "leaves.py").write_text("".join(lines))

    main(["trace", "target", "--root", str(root), "--max-paths", "5"])

    out = capsys.readouterr().out
    # exploration fanout caps at 20 neighbors -> 20 chains found, 5 shown
    assert "not shown: 15 paths (max_paths=5)" in out
    assert out.count("target -[call]-> leaf") == 5


class _FakeStdin:
    def __init__(self, text: str = "", tty: bool = False) -> None:
        self._text = text
        self._tty = tty

    def read(self) -> str:
        return self._text

    def isatty(self) -> bool:
        return self._tty


def test_path_prints_shortest_chains_from_source_to_target(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["path", "beta", "alpha", "--root", str(root)])

    assert capsys.readouterr().out == "beta -> alpha:\nbeta -[call,ret]-> alpha\n"


def test_path_no_route_is_successful_empty_result(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    main(["path", "beta", "gamma", "--root", str(root)])

    assert capsys.readouterr().out == "beta -> gamma: no chains (0 paths)\n"


def test_path_with_any_missing_name_aborts_without_output(tmp_path, capsys):
    root = _three_symbol_root(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["path", "beta", "no_such", "--root", str(root)])

    assert "not found" in str(exc.value.code)
    assert capsys.readouterr().out == ""


def test_path_default_depth_counts_both_ends_of_the_chain(tmp_path, capsys):
    root = _deep_chain_root(tmp_path)  # target -> d0 -> ... -> d19

    main(["path", "target", "d8", "--root", str(root)])  # exactly 10 symbols

    assert "target -[call,ret]-> d0" in capsys.readouterr().out

    main(["path", "target", "d9", "--root", str(root)])  # 11 symbols, over default

    assert capsys.readouterr().out == "target -> d9: no chains (0 paths)\n"


def test_diff_reads_unified_diff_from_stdin(tmp_path, capsys, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    source = textwrap.dedent(
        """\
        import os


        def helper():
            return os.getcwd()


        def top():
            changed = True
            return helper()
        """
    )
    (root / "m.py").write_text(source)
    top_start = source.split("\n").index("def top():") + 1
    patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        f"@@ -{top_start},3 +{top_start},4 @@\n"
        " def top():\n"
        "+    changed = True\n"
        "     return helper()\n"
    )
    monkeypatch.setattr("sys.stdin", _FakeStdin(patch))

    main(["diff", str(root / "m.py")])

    out = capsys.readouterr().out
    assert f"### L{top_start}-{top_start + 2}  [function top]" in out
    assert "    return helper()" in out
    assert "def helper" not in out


def test_diff_lines_spec_still_works(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "m.py").write_text(
        textwrap.dedent(
            """\
            def top():
                changed = True
                return helper()


            def helper():
                return 1
            """
        )
    )

    main(["diff", str(root / "m.py"), "--lines", "2"])

    out = capsys.readouterr().out
    assert "### L1-3  [function top]" in out
    assert "    return helper()" in out
    assert "def helper" not in out


def test_diff_empty_stdin_is_an_error(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    monkeypatch.setattr("sys.stdin", _FakeStdin(""))

    with pytest.raises(SystemExit) as exc:
        main(["diff", str(root / "m.py")])

    assert "no unified diff on stdin" in str(exc.value.code)


def test_diff_interactive_stdin_advises_lines_or_pipe(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    monkeypatch.chdir(root)  # outside any git checkout: terminal run cannot diff
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    with pytest.raises(SystemExit) as exc:
        main(["diff", str(root / "m.py")])

    assert "pipe a unified diff" in str(exc.value.code)


def test_diff_whole_diff_slices_every_changed_code_file(tmp_path, capsys, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    x = 1\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    y = 2\n    return 2\n")
    (root / "c.py").write_text("def gone():\n    return 3\n")
    (root / "README.md").write_text("# readme\n")
    monkeypatch.chdir(root)
    patch = (
        "--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,3 @@\n def alpha():\n+    x = 1\n     return 1\n"
        "--- a/b.py\n+++ b/b.py\n"
        "@@ -1,2 +1,3 @@\n def beta():\n+    y = 2\n     return 2\n"
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1,2 @@\n # readme\n+more\n"
        "--- a/c.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-def gone():\n-    return 3\n"
    )
    monkeypatch.setattr("sys.stdin", _FakeStdin(patch))

    main(["diff"])

    out = capsys.readouterr().out
    assert "a.py\n### L1-3  [function alpha]" in out
    assert "b.py\n### L1-3  [function beta]" in out
    assert "---" in out
    assert "c.py: MODULE DELETED (not sliced)" in out
    assert "README.md" not in out


def test_diff_whole_diff_empty_stdin_is_an_error(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(""))

    with pytest.raises(SystemExit) as exc:
        main(["diff"])

    assert "no unified diff on stdin" in str(exc.value.code)


def test_diff_lines_requires_path():
    with pytest.raises(SystemExit) as exc:
        main(["diff", "--lines", "2"])

    assert "--lines requires a PATH" in str(exc.value.code)


def _init_git_repo(root) -> bool:
    """Create a git repo at root with one commit; False if git is unavailable."""
    try:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def test_diff_terminal_slices_working_tree_from_git(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    (root / "m.py").write_text("def top():\n    changed = True\n    return 1\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    main(["diff"])

    out = capsys.readouterr().out
    assert "m.py\n### L1-3  [function top]" in out
    assert "    changed = True" in out


def test_diff_terminal_path_limits_git_diff_to_subtree(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 2\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    (root / "pkg" / "a.py").write_text("def alpha():\n    x = 1\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    y = 2\n    return 2\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    main(["diff", "pkg"])

    out = capsys.readouterr().out
    assert "pkg/a.py\n### L1-3  [function alpha]" in out
    assert "b.py" not in out


def test_diff_terminal_clean_tree_reports_no_changes(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    main(["diff"])

    assert "(no changes)" in capsys.readouterr().out


def _run_git(root, *argv) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)


def test_diff_explicit_working_tree_same_result_with_and_without_tty(
    tmp_path, capsys, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    (root / "m.py").write_text("def top():\n    changed = True\n    return 1\n")
    monkeypatch.chdir(root)

    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))
    main(["diff", "--working-tree"])
    with_tty = capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", _FakeStdin(""))  # no terminal, nothing piped
    main(["diff", "--working-tree"])
    without_tty = capsys.readouterr().out

    assert with_tty == without_tty
    assert "changed = True" in with_tty


def test_diff_explicit_stdin_same_result_with_and_without_tty(tmp_path, capsys, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    changed = True\n    return 1\n")
    patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def top():\n"
        "+    changed = True\n"
        "     return 1\n"
    )

    monkeypatch.setattr("sys.stdin", _FakeStdin(patch, tty=True))
    main(["diff", "--stdin", str(root / "m.py")])
    with_tty = capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", _FakeStdin(patch))
    main(["diff", "--stdin", str(root / "m.py")])
    without_tty = capsys.readouterr().out

    assert with_tty == without_tty
    assert "changed = True" in with_tty


def test_diff_repo_flag_resolves_paths_from_another_directory(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    (root / "m.py").write_text("def top():\n    changed = True\n    return 1\n")
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    monkeypatch.chdir(root)
    main(["diff", "--working-tree"])
    from_repo = capsys.readouterr().out

    monkeypatch.chdir(elsewhere)
    main(["diff", "--working-tree", "--repo", str(root)])
    from_elsewhere = capsys.readouterr().out

    assert from_elsewhere == from_repo
    assert "m.py\n### L1-3  [function top]" in from_elsewhere


def test_diff_staged_reads_index_while_working_file_differs(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    (root / "m.py").write_text("def top():\n    staged = True\n    return 1\n")
    _run_git(root, "add", "m.py")
    (root / "m.py").write_text("def top():\n    staged = True\n    disk = True\n    return 1\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    main(["diff", "--staged"])
    staged_out = capsys.readouterr().out
    assert "staged = True" in staged_out
    assert "disk = True" not in staged_out

    main(["diff", "--working-tree"])
    work_out = capsys.readouterr().out
    assert "disk = True" in work_out


def test_diff_base_reads_head_revision_not_working_file(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    (root / "m.py").write_text("def top():\n    committed = True\n    return 1\n")
    _run_git(root, "add", "m.py")
    _run_git(root, "commit", "-qm", "second")
    (root / "m.py").write_text(
        "def top():\n    committed = True\n    disk = True\n    return 1\n"
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    main(["diff", "--base", base_ref])

    out = capsys.readouterr().out
    assert "committed = True" in out
    assert "disk = True" not in out


def test_diff_rejects_conflicting_change_sources(capsys):
    with pytest.raises(SystemExit):
        main(["diff", "--working-tree", "--staged"])

    assert "not allowed with" in capsys.readouterr().err


def test_diff_explicit_source_reports_no_changes(tmp_path, capsys, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def top():\n    return 1\n")
    if not _init_git_repo(root):
        pytest.skip("git not available")
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(""))  # no terminal

    main(["diff", "--working-tree"])
    assert "(no changes)" in capsys.readouterr().out

    main(["diff", "--staged"])
    assert "(no changes)" in capsys.readouterr().out


def test_diff_explicit_source_outside_git_reports_clear_error(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", _FakeStdin(""))  # no terminal, source explicit

    with pytest.raises(SystemExit) as exc:
        main(["diff", "--working-tree"])

    assert "needs a git checkout" in str(exc.value.code)
