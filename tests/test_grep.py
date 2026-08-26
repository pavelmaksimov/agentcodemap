import textwrap

from codenav.core import parse_file

SAMPLE = textwrap.dedent(
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


def test_grep_groups_by_symbol():
    parsed = parse_file("m.py", SAMPLE, "python")
    hits = parsed.grep_symbols(r"\bjson\b")
    names = [e.qualified_name if e else None for e, _ in hits]
    assert "loader" in names
    assert "Saver.save" in names
    # import line is module-level (not inside any entity)
    module_hits = [matched for e, matched in hits if e is None]
    assert any(ln == 1 and "import json" in text for m in module_hits for ln, text in m)
    # 'other' does not reference json -> not a hit
    assert "Saver.other" not in names


def test_grep_matched_lines_only():
    parsed = parse_file("m.py", SAMPLE, "python")
    pairs = dict((e.qualified_name if e else None, matched) for e, matched in parsed.grep_symbols(r"return 42"))
    assert pairs == {"Saver.other": [(13, "        return 42")]}


def test_grep_no_match():
    parsed = parse_file("m.py", SAMPLE, "python")
    assert parsed.grep_symbols("no_such_thing") == []
