import textwrap

from codenav.core import parse_file
from codenav.outline import module_name, render_outline

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
        "project.mymodule\n"
        "A MY_MODULE_ATTR\n"
        "\n"
        "F my_func\n"
        "\n"
        "C MyClass\n"
        "    A my_attr\n"
        "    M my_method"
    )


def test_outline_with_lines():
    parsed = parse_file("m.py", SAMPLE, "python")
    out = render_outline(parsed.entities, "m.py", with_lines=True)
    assert "F my_func  L6-7" in out
    assert "C MyClass  L10-14" in out


def test_module_name_variants():
    assert module_name("project/mymodule.py") == "project.mymodule"
    assert module_name("./a/b.ts") == "a.b"
    assert module_name("single.go") == "single"
