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
    # longest chains win
    assert "side -> my_func -> mid -> base" in paths
    assert "top -> my_func -> mid -> base" in paths
    # unrelated does not reference my_func -> never appears
    assert not any("unrelated" in p for p in paths)


def test_graph_drops_chains_contained_in_longer_ones(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("my_func", max_nodes=5))
    # every shorter chain here is a contiguous piece of a longer one
    assert "top -> my_func" not in paths
    assert "side -> my_func" not in paths
    assert "my_func -> mid" not in paths
    assert "my_func -> mid -> base" not in paths


def test_graph_nodes_limits_each_path_without_dropping_paths(tmp_path):
    index = make_index(tmp_path)
    short = index.influence_paths("my_func", max_nodes=2)
    full = index.influence_paths("my_func", max_nodes=5)

    assert len(short) == len(full) == 2
    assert all(len(path) <= 2 for path in short)


def test_graph_nodes_does_not_collapse_paths_that_share_a_short_prefix(tmp_path):
    (tmp_path / "graph.py").write_text(
        textwrap.dedent(
            """\
            def left():
                return 1


            def right():
                return 2


            def shared(flag):
                return left() if flag else right()


            def target():
                return shared(True)


            def first():
                return target()


            def second():
                return target()
            """
        )
    )
    index = RepoIndex(str(tmp_path))

    short = index.influence_paths("target", max_nodes=2)
    full = index.influence_paths("target", max_nodes=5)

    assert len(short) == 2
    assert len(full) == 4
    assert all(len(path) <= 2 for path in short)


def test_graph_keeps_short_sibling_chain(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("base", max_nodes=4))
    assert "side -> my_func -> mid -> base" in paths
    assert "unrelated -> base" in paths


def test_graph_leaf_symbol(tmp_path):
    index = make_index(tmp_path)
    paths = render(index.influence_paths("base", max_nodes=5))
    # "mid -> base" is a contiguous piece of longer chains -> dropped
    assert "mid -> base" not in paths
    assert "unrelated -> base" in paths


def test_graph_uses_impact_resolution(tmp_path):
    (tmp_path / "models.py").write_text(
        "class ChatMessage:\n    session = None\n"
    )
    (tmp_path / "database.py").write_text(
        textwrap.dedent(
            """\
            def async_sessionmaker_factory():
                return factory()


            async def asession():
                async_session = async_sessionmaker_factory()
                async with async_session() as session:
                    yield session
            """
        )
    )
    (tmp_path / "consumer.py").write_text(
        "async def read():\n    async with asession() as session:\n        return session\n"
    )

    paths = render(RepoIndex(str(tmp_path)).influence_paths("asession", max_nodes=5))

    assert any("read -> asession -> async_sessionmaker_factory" in path for path in paths)
    assert not any(path.endswith("asession -> session") for path in paths)


def test_graph_matches_impact_for_string_literal_refs(tmp_path):
    # DI wiring via a type annotation or an attribute string value creates an
    # edge; docstrings and arbitrary strings inside method bodies do not
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


            def plain_probe():
                message = "plain string mentioning MyService"
                return message
            '''
        )
    )
    index = RepoIndex(str(tmp_path))
    chains = render(index.influence_paths("MyService", max_nodes=5))
    dependents = {e.qualified_name for e in index.impact("MyService").dependents}

    assert dependents == {"Container"}
    assert "Container -> MyService" in chains
    assert "docstring_probe -> MyService" not in chains
    assert "plain_probe -> MyService" not in chains


def test_graph_does_not_guess_ambiguous_bare_dependency(tmp_path):
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
    chains = render(index.influence_paths("worker", max_nodes=5))

    assert chains == ["handler -> worker"]


def test_graph_keeps_both_directions_of_a_cycle(tmp_path):
    (tmp_path / "cycle.py").write_text(
        textwrap.dedent(
            """\
            def first():
                return second()


            def second():
                return first()
            """
        )
    )

    chains = render(RepoIndex(str(tmp_path)).influence_paths("first", max_nodes=5))

    assert chains == ["first -> second", "second -> first"]


def test_graph_unknown_symbol(tmp_path):
    index = make_index(tmp_path)
    assert index.influence_paths("nope") == []
