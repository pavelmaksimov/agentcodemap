import textwrap

import pytest

from codenav.core import (
    added_lines_from_unified_diff,
    parse_file,
    slice_diff,
)


@pytest.fixture()
def sample_py():
    return textwrap.dedent(
        """\
        import os

        MY_CONST = 1


        def helper(x):
            return x + 1


        class MyClass:
            attr = 2

            def my_method(self, v):
                return helper(v) + self.attr
        """
    )


def test_parse_entities(sample_py):
    parsed = parse_file("m.py", sample_py, "python")
    kinds = {(e.kind, e.name) for e in parsed.entities}
    assert ("constant", "MY_CONST") in kinds


def test_local_variables_are_not_entities():
    src = textwrap.dedent(
        """\
        MODULE_ATTR = 1


        class C:
            class_attr = 2

            def m(self):
                local_var = 3
                return local_var
        """
    )
    parsed = parse_file("m.py", src, "python")
    names = {e.name for e in parsed.entities}
    assert {"MODULE_ATTR", "class_attr", "C", "m"} <= names
    assert "local_var" not in names
    # a changed line on a local var expands to the enclosing method
    slices = slice_diff("m.py", src, {7}, "python")
    assert slices[0].name == "m"


def test_nested_functions_are_not_entities():
    src = textwrap.dedent(
        """\
        def outer():
            def inner():
                return 1

            async def inner_async():
                return 2

            return inner()
        """
    )
    parsed = parse_file("m.py", src, "python")
    names = [e.name for e in parsed.entities]
    assert names == ["outer"]


def test_qualified_name_and_find_symbol(sample_py):
    parsed = parse_file("m.py", sample_py, "python")
    method = parsed.find_symbol("my_method")[0]
    assert method.qualified_name == "MyClass.my_method"
    # dotted qualified lookup
    assert parsed.find_symbol("MyClass.my_method")[0] is method


def test_slice_diff_expands_to_enclosing_function(sample_py):
    # line inside my_method body -> whole method sliced
    changed = {sample_py.split("\n").index("        return helper(v) + self.attr") + 1}
    slices = slice_diff("m.py", sample_py, changed, "python")
    assert len(slices) == 1
    sl = slices[0]
    assert sl.name == "my_method"
    assert "return helper(v)" in sl.content
    assert "def my_method" in sl.content
    assert "class MyClass:" not in sl.content


def test_slice_diff_changed_class_attr_includes_method(sample_py):
    lines = sample_py.split("\n")
    attr_line = lines.index("    attr = 2") + 1
    slices = slice_diff("m.py", sample_py, {attr_line}, "python")
    names = [sl.name for sl in slices]
    assert "attr" not in names  # attr change expands to callable/class context
    assert any("MyClass" in n for n in names)


def test_slice_diff_import_line(sample_py):
    slices = slice_diff("m.py", sample_py, {1}, "python")
    assert len(slices) == 1
    assert slices[0].kind == "import"
    assert slices[0].content == "import os"


def test_slice_diff_blank_or_out_of_range_lines(sample_py):
    assert slice_diff("m.py", sample_py, set(), "python") == []
    assert slice_diff("m.py", sample_py, {9999}, "python") == []


def test_added_lines_from_unified_diff():
    diff = """--- a/m.py
+++ b/m.py
@@ -1,3 +1,4 @@
 import os
+import sys
 
 def f():
"""
    result = added_lines_from_unified_diff(diff)
    assert result == {"m.py": {2}}


def test_added_lines_skips_deletions():
    diff = """--- a/m.py
+++ b/m.py
@@ -1,3 +1,3 @@
-deleted
 kept
+added
"""
    assert added_lines_from_unified_diff(diff) == {"m.py": {2}}
