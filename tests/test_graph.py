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
    paths = render(index.influence_paths("my_func", max_nodes=5))
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


def test_graph_node_budget_limits_distinct_symbols(tmp_path):
    index = make_index(tmp_path)
    paths = index.influence_paths("my_func", max_nodes=3)
    distinct = {e.qualified_name for p in paths for e in p}
    assert len(distinct) <= 3
    assert len(paths) > 0


def test_graph_budget_prefers_closest_nodes(tmp_path):
    index = make_index(tmp_path)
    # budget 2: target plus exactly one neighbor; chains are single edges only
    paths = render(index.influence_paths("my_func", max_nodes=2))
    for chain in paths:
        assert " -> " not in chain.split(" -> ", 1)[-1] or len(chain.split(" -> ")) == 2


def test_graph_leaf_symbol(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("base", max_nodes=5))
    # longest upstream chain consumes most of the budget...
    assert "side -> my_func -> mid -> base" in paths
    assert "mid -> base" in paths
    # ...so the sibling dependent does not fit anymore
    assert "unrelated -> base" not in paths
    assert all(p.startswith("base") is False for p in paths)

    # tighter budget: the 3-node chain wins over two 2-node chains
    raw = index.influence_paths("base", max_nodes=3)
    assert "my_func -> mid -> base" in render(raw)
    distinct = {e.qualified_name for p in raw for e in p}
    assert len(distinct) <= 3


def test_graph_budget_spent_on_longest_chain_first(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("base", max_nodes=4))
    assert "side -> my_func -> mid -> base" in paths
    # a sibling dependent does not fit into the remaining budget
    assert "unrelated -> base" not in paths

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


def test_same_name_definitions_merge_into_one_node(tmp_path):
    # two same-named functions in different files are one graph node:
    # no self-chain like "handler -> handler"
    (tmp_path / "a.py").write_text(
        textwrap.dedent(
            """\
            def handler():
                return worker()
            """
        )
    )
    (tmp_path / "b.py").write_text(
        textwrap.dedent(
            """\
            def handler():
                return 2


            def worker():
                return handler() + 1
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    chains = render(index.influence_paths("worker", max_nodes=4))
    assert not any("handler -> handler" in c for c in chains)


def test_graph_unknown_symbol(tmp_path):
    index = make_index(tmp_path)
    assert index.influence_paths("nope") == []
