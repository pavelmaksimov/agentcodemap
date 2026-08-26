import textwrap

from codenav.core import RepoIndex

CHAIN = textwrap.dedent(
    """\
    def base():
        return 1


    def mid():
        return base()


    def my_func():
        return mid()


    def top():
        return my_func()


    def side():
        return my_func()


    def unrelated():
        return base()
    """
)


def make_index(tmp_path):
    (tmp_path / "chain.py").write_text(CHAIN)
    return RepoIndex(str(tmp_path))


def render(paths):
    return [" -> ".join(e.name for e in p) for p in paths]


def test_graph_upstream_downstream_and_merged(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("my_func", max_nodes=4))
    # upstream dependents
    assert "top -> my_func" in paths
    assert "side -> my_func" in paths
    # downstream dependencies
    assert "my_func -> mid" in paths
    assert "my_func -> mid -> base" in paths
    # merged through-target chains
    assert "top -> my_func -> mid" in paths
    assert "side -> my_func -> mid -> base" in paths
    # unrelated does not reference my_func -> never appears
    assert not any("unrelated" in p for p in paths)


def test_graph_node_limit(tmp_path):
    index = make_index(tmp_path)
    for path in index.influence_paths("my_func", max_nodes=2):
        assert len(path) <= 2


def test_graph_leaf_symbol(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("base", max_nodes=4))
    # base is referenced by mid and unrelated; references nothing
    assert "mid -> base" in paths
    assert "unrelated -> base" in paths
    assert "mid -> base ->" not in [p + "-" for p in paths]
    assert all(p.startswith("base") is False for p in paths)


def test_string_literal_refs_and_cross_file_edges(tmp_path):
    # DI-style wiring: class referenced only via string literals must still
    # produce an edge, including across files (line numbers must not be
    # compared between different files)
    (tmp_path / "svc.py").write_text(
        textwrap.dedent(
            """\
            class MyService:
                def run(self):
                    return 1
            """
        )
    )
    (tmp_path / "wire.py").write_text(
        textwrap.dedent(
            '''\
            class Container:
                svc: "MyService" = build("app.svc:MyService")

                def get(self):
                    return self.svc


            def docstring_probe():
                """
                docstring mentioning MyService must not create an edge
                """
            '''
        )
    )
    index = RepoIndex(str(tmp_path))
    chains = render(index.influence_paths("MyService", max_nodes=5))
    assert "Container -> MyService" in chains
    # docstring mention alone must not create a reference edge
    assert "docstring_probe -> MyService" not in chains


def test_graph_unknown_symbol(tmp_path):
    index = make_index(tmp_path)
    assert index.influence_paths("nope") == []
