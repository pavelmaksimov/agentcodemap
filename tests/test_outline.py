import textwrap

from codenav.parse import parse_file
from codenav.outline import assemble_outline, module_depth, module_name, render_outline

SAMPLE = textwrap.dedent(
    """\
    import os

    MY_MODULE_ATTR = 1


    def my_func():
        return MY_MODULE_ATTR


    class MyClass:
        my_attr = 2

        def my_method(self):
            return self.my_attr
    """
)


def test_outline_format():
    parsed = parse_file("project/mymodule.py", SAMPLE, "python")
    out = render_outline(parsed.entities, "project/mymodule.py")
    assert out == (
        "project.mymodule:\n"
        "A MY_MODULE_ATTR\n"
        "\n"
        "F my_func\n"
        "\n"
        "C MyClass\n"
        " A my_attr\n"
        " M my_method"
    )


def test_outline_skips_empty_module():
    parsed = parse_file("empty.py", "import os\n", "python")
    assert render_outline(parsed.entities, "empty.py") == ""


def test_parse_without_refs_keeps_entities_only():
    # outline/diff/grep parse files just to render entities: the reference walk
    # (identifier + string-literal scanning) must be skippable without changing
    # what those commands consume
    full = parse_file("project/mymodule.py", SAMPLE, "python")
    light = parse_file("project/mymodule.py", SAMPLE, "python", collect_refs=False)
    assert light is not None and full is not None
    assert [(e.kind, e.name) for e in light.entities] == [
        (e.kind, e.name) for e in full.entities
    ]
    assert light.imports == full.imports
    assert light.bare_refs == {} and light.string_refs == {}
    assert full.bare_refs  # sanity: the default path still collects references


def test_outline_with_lines():
    parsed = parse_file("m.py", SAMPLE, "python")
    out = render_outline(parsed.entities, "m.py", with_lines=True)
    assert "F my_func  L6-7" in out
    assert "C MyClass  L10-14" in out


def test_outline_top_level_skips_nested_members():
    parsed = parse_file("project/mymodule.py", SAMPLE, "python")
    out = render_outline(parsed.entities, "project/mymodule.py", top_level=True)
    assert out == (
        "project.mymodule:\n"
        "A MY_MODULE_ATTR\n"
        "\n"
        "F my_func\n"
        "\n"
        "C MyClass"
    )
    assert "my_attr" not in out
    assert "my_method" not in out


def test_outline_top_level_with_lines():
    parsed = parse_file("m.py", SAMPLE, "python")
    out = render_outline(parsed.entities, "m.py", with_lines=True, top_level=True)
    assert out == (
        "m:\n"
        "A MY_MODULE_ATTR  L3-3\n"
        "\n"
        "F my_func  L6-7\n"
        "\n"
        "C MyClass  L10-14"
    )


def test_outline_skips_dunders():
    parsed = parse_file(
        "m.py",
        textwrap.dedent(
            """\
            __all__ = ["Box"]
            __version__ = "1.0"


            class Box:
                def __init__(self):
                    self.x = 1

                def __repr__(self):
                    return "Box"

                def __call__(self):
                    return self.x

                def render(self):
                    return self.x
            """
        ),
        "python",
    )
    out = render_outline(parsed.entities, "m.py")
    assert "M __init__" not in out
    assert "M __repr__" not in out
    assert "M __call__" not in out
    assert "M render" in out
    assert "A __all__" not in out  # metadata attrs go too
    assert "__version__" not in out


def test_outline_renders_dependencies_below_their_symbol():
    parsed = parse_file("m.py", SAMPLE, "python")
    by_name = {e.name: e for e in parsed.entities}
    out = render_outline(
        parsed.entities,
        "m.py",
        deps={
            by_name["my_func"]: ["MY_MODULE_ATTR [reference]"],
            by_name["my_method"]: ["my_attr [reference]"],
        },
    )
    assert out == (
        "m:\n"
        "A MY_MODULE_ATTR\n"
        "\n"
        "F my_func\n"
        " -> MY_MODULE_ATTR [reference]\n"
        "\n"
        "C MyClass\n"
        " A my_attr\n"
        " M my_method\n"
        "  -> my_attr [reference]"
    )


def test_module_name_variants():
    assert module_name("project/mymodule.py") == "project.mymodule"
    assert module_name("./a/b.ts") == "a.b"
    assert module_name("single.go") == "single"


def test_module_depth_counts_nesting_below_root():
    assert module_depth("proj/top.py", ["proj"]) == 0
    assert module_depth("proj/sub/mid.py", ["proj"]) == 1
    assert module_depth("proj/sub/deep/bot.py", ["proj"]) == 2
    # closest containing root wins
    assert module_depth("proj/sub/mid.py", ["proj", "proj/sub"]) == 0
    assert module_depth("proj/top.py", ["proj", "proj/sub"]) == 0


def test_module_depth_unrelated_or_file_roots_default_to_zero():
    assert module_depth("proj/top.py", []) == 0
    assert module_depth("proj/top.py", ["other"]) == 0
    assert module_depth("proj/other.py", ["proj/top.py"]) == 0  # file root
    assert module_depth("proj/top.py", ["proj/top.py"]) == 0  # file root, same file


def test_module_depth_dot_root():
    assert module_depth("top.py", ["."]) == 0
    assert module_depth("sub/mid.py", ["."]) == 1
    assert module_depth("/abs/out.py", ["."]) == 0


def test_assemble_orders_modules_shallowest_first():
    modules = [
        ("proj/deep/x.py", "deep.x:\nF deep"),
        ("proj/top.py", "top:\nF top"),
        ("proj/mid.py", "mid:\nF mid"),
        ("proj/sub/under.py", "under:\nF under"),
    ]
    pages, oversized = assemble_outline(modules, roots=["proj"], max_chars=10_000)
    # depth 0 first (path tiebreak: mid < top), then depth 1 (deep < sub)
    assert pages == [
        "mid:\nF mid\n\n"
        "top:\nF top\n\n"
        "deep.x:\nF deep\n\n"
        "under:\nF under"
    ]
    assert oversized == []


def test_assemble_filters_by_module_path_regex():
    modules = [
        ("proj/schemas/order.py", "schemas.order:\nC Order"),
        ("proj/services/api.py", "services.api:\nF api"),
        ("proj/models/user.py", "models.user:\nC User"),
    ]
    pages, oversized = assemble_outline(
        modules, roots=["proj"], filters=["schemas|services"]
    )
    assert oversized == []
    assert len(pages) == 1
    assert "schemas.order:" in pages[0]
    assert "services.api:" in pages[0]
    assert "models.user:" not in pages[0]


def test_assemble_repeated_filters_are_or_ed():
    modules = [
        ("proj/schemas/order.py", "schemas.order:\nC Order"),
        ("proj/services/api.py", "services.api:\nF api"),
        ("proj/models/user.py", "models.user:\nC User"),
    ]
    pages, _ = assemble_outline(modules, roots=["proj"], filters=["schemas", "models"])
    assert len(pages) == 1
    assert "schemas.order:" in pages[0]
    assert "models.user:" in pages[0]
    assert "services.api:" not in pages[0]


def test_assemble_no_modules_or_no_match_is_empty():
    assert assemble_outline([], roots=["proj"], filters=["schemas"]) == ([], [])
    modules = [("proj/schemas/order.py", "schemas.order:\nC Order")]
    assert assemble_outline(modules, roots=["proj"], filters=["zzz"]) == ([], [])


def test_assemble_overflowing_module_moves_to_the_next_page_whole():
    modules = [("p/a.py", "AAAA"), ("p/b.py", "BBBB"), ("p/c.py", "CCCC")]
    pages, oversized = assemble_outline(modules, roots=["p"], max_chars=13)
    # A+B take 10 chars; C needs 4+2 more -> starts page 2 instead of being cut
    assert pages == ["AAAA\n\nBBBB", "CCCC"]
    assert oversized == []
    assert all(len(page) <= 13 for page in pages)


def test_assemble_pages_split_only_between_modules():
    modules = [("p/a.py", "alpha"), ("p/b.py", "beta")]
    pages, oversized = assemble_outline(modules, roots=["p"], max_chars=10)
    # "alpha" (5) + separator (2) would overflow with "beta": page break, no cut
    assert pages == ["alpha", "beta"]
    assert oversized == []
    assert all(len(page) <= 10 for page in pages)


def test_assemble_oversized_module_keeps_leading_lines_and_is_listed():
    modules = [("p/big.py", "h1:\nline2\nline3\nline4")]
    pages, oversized = assemble_outline(modules, roots=["p"], max_chars=14)
    # h1:(3) + line2(6) = 9; line3 needs 6 more -> 15 > 14
    assert pages == ["h1:\nline2"]
    assert oversized == ["p/big.py"]


def test_assemble_multiple_oversized_modules_each_get_their_own_page():
    modules = [
        ("p/big1.py", "h1:\nline2\nline3\nline4"),
        ("p/small.py", "s1:\nx"),
        ("p/big2.py", "g1:\nline2\nline3\nline4\nline5"),
    ]
    pages, oversized = assemble_outline(modules, roots=["p"], max_chars=14)
    # sorted by path: big1, big2, small — each oversized module owns a page
    assert oversized == ["p/big1.py", "p/big2.py"]
    assert pages == ["h1:\nline2", "g1:\nline2", "s1:\nx"]
    assert all(len(page) <= 14 for page in pages)


def test_assemble_single_line_longer_than_page_still_owns_a_page():
    modules = [("p/wide.py", "x" * 20)]
    pages, oversized = assemble_outline(modules, roots=["p"], max_chars=10)
    # lines are never split, so nothing fits; the module is still reported
    assert pages == [""]
    assert oversized == ["p/wide.py"]


def test_assemble_fits_whole_module_exactly_at_limit():
    modules = [("p/a.py", "AAAA"), ("p/b.py", "BBBB")]
    pages, oversized = assemble_outline(modules, roots=["p"], max_chars=10)
    assert pages == ["AAAA\n\nBBBB"]
    assert oversized == []

