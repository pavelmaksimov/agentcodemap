import textwrap

from codenav.core import RepoIndex, parse_file


def make_repo(tmp_path):
    (tmp_path / "utils.py").write_text(
        textwrap.dedent(
            """\
            def calc(x):
                return x * 2
            """
        )
    )
    (tmp_path / "app.py").write_text(
        textwrap.dedent(
            """\
            from utils import calc

            MAX = 10


            class Runner:
                def run(self, v):
                    return calc(v) + MAX


            def main():
                r = Runner()
                return r.run(1)
            """
        )
    )
    return tmp_path


def test_find_symbol_across_repo(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    found = index.find_symbol("calc")
    assert len(found) == 1
    assert found[0].file.endswith("utils.py")
    assert found[0].kind == "function"


def test_impact_dependents_and_depends_on(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    report = index.impact("calc")
    assert report is not None
    # Runner.run and nothing else calls calc
    dependents = {e.qualified_name for e in report.dependents}
    assert "Runner.run" in dependents
    assert "main" not in dependents
    # calc depends on nothing user-defined
    assert report.depends_on == []


def test_impact_of_class_method(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    report = index.impact("run")
    assert report is not None
    assert {e.qualified_name for e in report.dependents} >= {"main"}
    depends = {e.qualified_name for e in report.depends_on}
    assert "calc" in depends
    # MAX is a constant symbol referenced inside run
    assert "MAX" in depends


def test_impact_missing_symbol(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    assert index.impact("no_such_symbol") is None


def test_refs_exclude_definition_name(tmp_path):
    parsed = parse_file("u.py", "def calc(x):\n    return x\n", "python")
    assert "calc" not in parsed.refs or all(
        e.qualified_name != "calc" for e in parsed.refs.get("calc", set())
    )


def test_skip_dirs(tmp_path):
    root = make_repo(tmp_path)
    junk = root / "node_modules"
    junk.mkdir()
    (junk / "bad.py").write_text("def noise():\n    pass\n")
    index = RepoIndex(str(root))
    assert not index.find_symbol("noise")
