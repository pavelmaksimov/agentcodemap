import textwrap

from codenav.core import parse_file
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


def test_outline_with_lines():
    parsed = parse_file("m.py", SAMPLE, "python")
    out = render_outline(parsed.entities, "m.py", with_lines=True)
    assert "F my_func  L6-7" in out
    assert "C MyClass  L10-14" in out


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
    body, shown, omitted = assemble_outline(modules, roots=["proj"], max_chars=10_000)
    # depth 0 first (path tiebreak: mid < top), then depth 1 (deep < sub)
    assert body == (
        "mid:\nF mid\n\n"
        "top:\nF top\n\n"
        "deep.x:\nF deep\n\n"
        "under:\nF under"
    )
    assert (shown, omitted) == (4, 0)


def test_assemble_filters_by_module_path_regex():
    modules = [
        ("proj/schemas/order.py", "schemas.order:\nC Order"),
        ("proj/services/api.py", "services.api:\nF api"),
        ("proj/models/user.py", "models.user:\nC User"),
    ]
    body, shown, omitted = assemble_outline(
        modules, roots=["proj"], filters=["schemas|services"]
    )
    assert (shown, omitted) == (2, 0)
    assert "schemas.order:" in body
    assert "services.api:" in body
    assert "models.user:" not in body


def test_assemble_repeated_filters_are_or_ed():
    modules = [
        ("proj/schemas/order.py", "schemas.order:\nC Order"),
        ("proj/services/api.py", "services.api:\nF api"),
        ("proj/models/user.py", "models.user:\nC User"),
    ]
    body, shown, _ = assemble_outline(modules, roots=["proj"], filters=["schemas", "models"])
    assert shown == 2
    assert "schemas.order:" in body
    assert "models.user:" in body
    assert "services.api:" not in body


def test_assemble_no_modules_or_no_match_is_empty():
    assert assemble_outline([], roots=["proj"], filters=["schemas"]) == ("", 0, 0)
    modules = [("proj/schemas/order.py", "schemas.order:\nC Order")]
    assert assemble_outline(modules, roots=["proj"], filters=["zzz"]) == ("", 0, 0)


def test_assemble_cap_keeps_whole_modules_then_stops():
    modules = [("p/a.py", "AAAA"), ("p/b.py", "BBBB"), ("p/c.py", "CCCC")]
    body, shown, omitted = assemble_outline(modules, roots=["p"], max_chars=13)
    assert body == "AAAA\n\nBBBB"
    assert (shown, omitted) == (2, 1)
    assert len(body) <= 13


def test_assemble_cap_splits_last_module_at_line_boundary():
    modules = [("p/a.py", "alpha"), ("p/b.py", "beta")]
    body, shown, omitted = assemble_outline(modules, roots=["p"], max_chars=10)
    # "alpha" (5) + separator (2) leaves 3 chars: "bet" would split a line,
    # so b is dropped whole and the trailing separator is not emitted either.
    assert body == "alpha"
    assert (shown, omitted) == (1, 1)
    assert len(body) <= 10


def test_assemble_cap_truncates_oversized_module_keeping_leading_lines():
    modules = [("p/big.py", "h1:\nline2\nline3\nline4")]
    body, shown, omitted = assemble_outline(modules, roots=["p"], max_chars=14)
    # h1:(3) + line2(6) = 9; line3 needs 6 more -> 15 > 14
    assert body == "h1:\nline2"
    assert (shown, omitted) == (0, 1)
    assert len(body) <= 14


def test_assemble_cap_fits_whole_module_exactly_at_limit():
    modules = [("p/a.py", "AAAA"), ("p/b.py", "BBBB")]
    body, shown, omitted = assemble_outline(modules, roots=["p"], max_chars=10)
    assert body == "AAAA\n\nBBBB"
    assert (shown, omitted) == (2, 0)
