import textwrap

from codenav.index import RepoIndex
from codenav.parse import parse_file


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


def test_index_two_sibling_roots_exclude_other_dirs(tmp_path):
    project = tmp_path / "project"
    tests = tmp_path / "tests"
    other = tmp_path / "other"
    for d in (project, tests, other):
        d.mkdir()
    (project / "target.py").write_text("def target():\n    return 1\n")
    (project / "user.py").write_text(
        "from target import target\n\n\ndef use():\n    return target()\n"
    )
    (tests / "test_user.py").write_text("def test_use():\n    return target()\n")
    (other / "noise.py").write_text("def other_user():\n    return target()\n")

    index = RepoIndex([str(project), str(tests)])

    # sibling directory on the same level is not walked
    assert not index.find_symbol("other_user")
    assert all(
        f.startswith(str(project)) or f.startswith(str(tests)) for f in index.files
    )
    report = index.impact("target")
    dependents = {r.entity.qualified_name for r in report.dependents}
    assert {"use", "test_use"} <= dependents
    assert "other_user" not in dependents


def test_impact_dependents_and_depends_on(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    report = index.impact("calc")
    assert report is not None
    # Runner.run and nothing else calls calc
    dependents = {r.entity.qualified_name for r in report.dependents}
    assert "Runner.run" in dependents
    assert "main" not in dependents
    # calc depends on nothing user-defined
    assert report.depends_on == []


def test_impact_of_class_method(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    report = index.impact("run")
    assert report is not None
    assert {r.entity.qualified_name for r in report.dependents} >= {"main"}
    depends = {r.entity.qualified_name for r in report.depends_on}
    assert "calc" in depends
    # MAX is a constant symbol referenced inside run
    assert "MAX" in depends


def test_impact_missing_symbol(tmp_path):
    index = RepoIndex(str(make_repo(tmp_path)))
    assert index.impact("no_such_symbol") is None


def test_bare_refs_exclude_definition_name(tmp_path):
    # a definition's own name node is not a self-reference; body uses still are
    parsed = parse_file("u.py", "def calc(x):\n    return x\n", "python")
    assert "calc" not in parsed.bare_refs or all(
        e.qualified_name != "calc" for e in parsed.bare_refs.get("calc", set())
    )
    assert any(e.qualified_name == "calc" for e in parsed.bare_refs.get("x", ()))


def test_impact_class_aggregates_method_refs(tmp_path):
    # depends-on of a CLASS must include symbols used inside its methods,
    # not only identifiers from the class body itself
    (tmp_path / "a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x):
                return x


            class Service:
                attr = 1

                def run(self, v):
                    return helper(v) + self.attr
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    report = index.impact("Service")
    assert report is not None
    depends = {r.entity.qualified_name for r in report.depends_on}
    assert "helper" in depends


def test_impact_di_registration_strings_in_attr_positions(tmp_path):
    # Services-style container: attributes wire services only via string
    # literals (LazyService paths, forward annotations). Those DI-position
    # strings must resolve in both directions; docstrings and plain string
    # values inside method bodies and module constants must not.
    (tmp_path / "svc.py").write_text(
        textwrap.dedent(
            """\
            class JobStore:
                pass

            class ChatMessageRepository:
                pass
            """
        )
    )
    (tmp_path / "container.py").write_text(
        textwrap.dedent(
            '''\
            class Services:
                job_store: "JobStore" = LazyService(
                    "project.components.job_store.service:JobStore"
                )
                chat_message_repo = LazyService(
                    "project.components.chat.repositories:ChatMessageRepository"
                )


            MODE = "prod JobStore"

            # module-level registration is a DI position too
            fallback_repo = LazyService(
                "project.components.chat.repositories:ChatMessageRepository"
            )


            def plain_probe():
                text = "unrelated ChatMessageRepository"
                return text
            '''
        )
    )
    index = RepoIndex(str(tmp_path))

    container = index.impact("Services")
    assert container is not None
    assert {"JobStore", "ChatMessageRepository"} <= {
        r.entity.qualified_name for r in container.depends_on
    }
    job_store_relation = next(
        r for r in container.depends_on if r.entity.qualified_name == "JobStore"
    )
    # two sites: the quoted forward ref sits in a field annotation (param),
    # the LazyService value is a DI string (text-only candidate)
    assert job_store_relation.kinds == ("param", "string")

    job_store = index.impact("JobStore")
    assert job_store is not None
    assert "Services" in {r.entity.qualified_name for r in job_store.dependents}

    chat_repo = index.impact("ChatMessageRepository")
    assert chat_repo is not None
    # MODE constant value and plain_probe's local string are not DI positions;
    # the module-level fallback_repo registration is
    assert {r.entity.qualified_name for r in chat_repo.dependents} == {
        "Services",
        "fallback_repo",
    }

    fallback = index.impact("fallback_repo")
    assert fallback is not None
    assert {"ChatMessageRepository"} == {
        r.entity.qualified_name for r in fallback.depends_on
    }


def test_quoted_forward_reference_is_an_annotation_not_a_string(tmp_path):
    # "Foo" in annotation position is a forward reference: it keeps the
    # position's kind (param after `:`, return after `->`), never degrading
    # to a plain string. A DI wiring value (LazyService path) has no syntactic
    # role and stays a string candidate; the two must not be conflated.
    (tmp_path / "svc.py").write_text("class Foo:\n    pass\n")
    (tmp_path / "wire.py").write_text(
        textwrap.dedent(
            '''\
            def typed(body: "Foo") -> "Foo":
                return body


            class Container:
                svc = LazyService("app.svc:Foo")
            '''
        )
    )
    index = RepoIndex(str(tmp_path))

    typed = index.impact("typed")
    assert {r.entity.qualified_name: set(r.kinds) for r in typed.depends_on} == {
        "Foo": {"param", "return"}
    }
    container = index.impact("Container")
    assert {r.entity.qualified_name: set(r.kinds) for r in container.depends_on} == {
        "Foo": {"string"}
    }


def test_annotation_positions_split_into_param_and_return(tmp_path):
    # `-> T` is the producer position, a parameter the consumer position:
    # who returns a data object and who accepts it must be separable
    (tmp_path / "dto.py").write_text("class DTO:\n    pass\n")
    (tmp_path / "use.py").write_text(
        textwrap.dedent(
            """\
            def producer() -> DTO:
                return DTO()


            def consumer(body: DTO):
                return body
            """
        )
    )
    index = RepoIndex(str(tmp_path))

    kinds = {
        r.entity.qualified_name: set(r.kinds)
        for r in index.impact("DTO").dependents
    }
    assert kinds == {
        "producer": {"call", "return"},
        "consumer": {"param"},
    }


def test_impact_attr_does_not_match_unrelated_attribute_name(tmp_path):
    (tmp_path / "models.py").write_text(
        textwrap.dedent(
            """\
            class ChatMessage:
                session = None
            """
        )
    )
    (tmp_path / "gitlab.py").write_text(
        textwrap.dedent(
            """\
            def _patch_gitlab_connection_errors(client):
                session = client.session
                return session
            """
        )
    )
    (tmp_path / "consumer.py").write_text(
        "def read_chat_session():\n    return ChatMessage.session\n"
    )

    index = RepoIndex(str(tmp_path))
    report = index.impact("ChatMessage.session")

    assert report is not None
    assert "read_chat_session" in {
        r.entity.qualified_name for r in report.dependents
    }
    assert "_patch_gitlab_connection_errors" not in {
        r.entity.qualified_name for r in report.dependents
    }


def test_impact_method_matches_typed_receiver_not_unrelated_append(tmp_path):
    (tmp_path / "repo.py").write_text(
        textwrap.dedent(
            """\
            class ChatMessageRepository:
                def append(self):
                    return 1


            class Services:
                repo: "ChatMessageRepository" = None


            def uses_chat_repository():
                return Services().repo.append()


            def uses_list(items):
                return items.append(1)
            """
        )
    )

    index = RepoIndex(str(tmp_path))
    report = index.impact("ChatMessageRepository.append")

    assert report is not None
    dependents = {r.entity.qualified_name for r in report.dependents}
    assert "uses_chat_repository" in dependents
    assert "uses_list" not in dependents


def test_impact_method_does_not_resolve_bare_keyword_as_attribute(tmp_path):
    (tmp_path / "repo.py").write_text(
        textwrap.dedent(
            """\
            class Message:
                content = None


            class Repository:
                def append(self, content):
                    return Message(content=content)
            """
        )
    )

    index = RepoIndex(str(tmp_path))
    report = index.impact("Repository.append")

    assert report is not None
    depends_on = {r.entity.qualified_name for r in report.depends_on}
    assert "Message" in depends_on
    assert "Message.content" not in depends_on


def test_skip_dirs(tmp_path):
    root = make_repo(tmp_path)
    junk = root / "node_modules"
    junk.mkdir()
    (junk / "bad.py").write_text("def noise():\n    pass\n")
    index = RepoIndex(str(root))
    assert not index.find_symbol("noise")


def _make_module_alias_repo(tmp_path):
    """Package tree mirroring the code-master layout:

    ``from project.components.code_review import use_cases`` followed by
    ``use_cases.start_code_review(body)`` — a member access whose receiver is
    a module alias, not a typed object.
    """
    pkg = tmp_path / "project" / "components" / "code_review"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "use_cases.py").write_text(
        textwrap.dedent(
            """\
            async def start_code_review(body):
                return body
            """
        )
    )
    (pkg / "endpoints.py").write_text(
        textwrap.dedent(
            """\
            from project.components.code_review import use_cases


            async def code_review_endpoint(body):
                return await use_cases.start_code_review(body)
            """
        )
    )
    return tmp_path


def test_module_alias_resolves_both_directions(tmp_path):
    index = RepoIndex(str(_make_module_alias_repo(tmp_path)))

    use_case = index.impact("start_code_review")
    assert use_case is not None
    assert {"code_review_endpoint"} == {
        r.entity.qualified_name for r in use_case.dependents
    }

    endpoint = index.impact("code_review_endpoint")
    assert endpoint is not None
    assert "start_code_review" in {
        r.entity.qualified_name for r in endpoint.depends_on
    }


def test_module_alias_resolves_when_root_is_package_dir(tmp_path):
    # `--root <pkg>` with imports spelled from above the root: the import
    # prefix ('project.') has no directory under the root, so the module path
    # must match as a suffix of the import path.
    repo = _make_module_alias_repo(tmp_path)
    index = RepoIndex(str(repo / "project"))
    report = index.impact("start_code_review")
    assert report is not None
    assert {"code_review_endpoint"} == {
        r.entity.qualified_name for r in report.dependents
    }


def test_module_import_as_and_dotted_receiver(tmp_path):
    repo = _make_module_alias_repo(tmp_path)
    pkg = repo / "project" / "components" / "code_review"
    (pkg / "aliased.py").write_text(
        textwrap.dedent(
            """\
            import project.components.code_review.use_cases as uc


            def via_alias(body):
                return uc.start_code_review(body)
            """
        )
    )
    (pkg / "dotted.py").write_text(
        textwrap.dedent(
            """\
            import project.components.code_review.use_cases


            def via_dotted(body):
                return project.components.code_review.use_cases.start_code_review(body)
            """
        )
    )
    index = RepoIndex(str(repo))
    report = index.impact("start_code_review")
    assert report is not None
    assert {"code_review_endpoint", "via_alias", "via_dotted"} == {
        r.entity.qualified_name for r in report.dependents
    }


def test_module_resolution_rejects_unrelated_receivers(tmp_path):
    repo = _make_module_alias_repo(tmp_path)
    pkg = repo / "project" / "components" / "code_review"
    (pkg / "noise.py").write_text(
        textwrap.dedent(
            """\
            from project.components.code_review import use_cases


            async def unrelated(body, obj):
                return await obj.start_code_review(body)


            async def other_member(body):
                return use_cases.some_other_symbol(body)
            """
        )
    )
    (repo / "other.py").write_text(
        "async def some_other_symbol(body):\n    return body\n"
    )
    index = RepoIndex(str(repo))

    # a local parameter named obj is not the use_cases module
    report = index.impact("start_code_review")
    assert report is not None
    assert "unrelated" not in {
        r.entity.qualified_name for r in report.dependents
    }
    # some_other_symbol lives in other.py, not in the use_cases module
    other = index.impact("some_other_symbol")
    assert other is not None
    assert "other_member" not in {
        r.entity.qualified_name for r in other.dependents
    }


def test_relative_import_resolves(tmp_path):
    pkg = tmp_path / "code_review"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "use_cases.py").write_text(
        textwrap.dedent(
            """\
            def start(body):
                return body
            """
        )
    )
    (pkg / "endpoints.py").write_text(
        textwrap.dedent(
            """\
            from . import use_cases


            def endpoint(body):
                return use_cases.start(body)
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    report = index.impact("start")
    assert report is not None
    assert "endpoint" in {r.entity.qualified_name for r in report.dependents}


def test_relation_kinds_name_the_reference_site(tmp_path):
    # every relation carries the syntactic role of the reference that made it
    (tmp_path / "kinds.py").write_text(
        textwrap.dedent(
            """\
            class Base:
                pass


            class Child(Base):
                dep: Base = None

                def run(self):
                    return Base
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    dependents = {
        r.entity.qualified_name: set(r.kinds)
        for r in index.impact("Base").dependents
    }

    # the base list and the field annotation both belong to the class body;
    # run returns Base directly, so its site reads 'return'
    assert dependents["Child"] == {"param", "inheritance"}
    assert dependents["Child.run"] == {"return"}


def test_depends_on_relation_kind_is_the_reference_kind(tmp_path):
    (tmp_path / "kinds.py").write_text(
        textwrap.dedent(
            """\
            def helper():
                return 1


            def caller():
                return helper()
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    depends = {
        r.entity.qualified_name: set(r.kinds)
        for r in index.impact("caller").depends_on
    }

    assert depends["helper"] == {"call", "return"}


def test_relation_kinds_include_the_reference_lines(tmp_path):
    (tmp_path / "kinds.py").write_text(
        textwrap.dedent(
            """\
            def helper():
                return 1


            def caller():
                return helper()
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    relation = next(
        r for r in index.impact("caller").depends_on if r.entity.qualified_name == "helper"
    )

    # line 6 is the call site inside caller(), not the helper definition line;
    # the same site also stacks 'return' because the call is what returns
    assert [(o.kind, o.line) for o in relation.observations] == [
        ("call", 6),
        ("return", 6),
    ]


def test_impact_kind_filter_is_a_view_not_a_cached_report(tmp_path):
    (tmp_path / "kinds.py").write_text(
        textwrap.dedent(
            """\
            class Base:
                pass


            class Child(Base):
                dep: Base = None


            def make():
                return Base()
            """
        )
    )
    index = RepoIndex(str(tmp_path))
    target = index.find_symbol("Base")[0]

    filtered = index.impact_entity(target, kinds=("call",))
    assert {r.entity.qualified_name for r in filtered.dependents} == {"make"}

    # filtering must not replace the memoized report other queries share
    everything = index.impact_entity(target)
    assert {r.entity.qualified_name: set(r.kinds) for r in everything.dependents} == {
        "Child": {"param", "inheritance"},
        "make": {"call", "return"},
    }


def test_dunder_metadata_not_in_relations(tmp_path):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            """\
            __all__ = ["Worker", "start"]

            class Worker:
                def run(self):
                    return start()


            def start():
                return Worker()
            """
        )
    )
    (tmp_path / "user.py").write_text(
        textwrap.dedent(
            """\
            import mod


            def peek():
                return mod.__all__
            """
        )
    )
    index = RepoIndex(str(tmp_path))

    # __all__ names its exports, but is not a dependent of any of them
    worker = index.find_symbol("Worker")[0]
    dependents = {r.entity.name for r in index.impact_entity(worker).dependents}
    assert "__all__" not in dependents

    # code reading mod.__all__ does not gain it as a dependency either
    peek_report = index.impact("peek")
    assert peek_report is not None
    assert all(r.entity.name != "__all__" for r in peek_report.depends_on)

    # the metadata node itself participates in no relation at all
    report = index.impact("__all__")
    assert report is not None
    assert report.depends_on == []
    assert report.dependents == []

    # and graph walks never route through it
    paths = index.influence_paths("Worker")
    assert paths
    assert all("__all__" not in [e.name for e in p] for p in paths)


def test_dunder_methods_stay_in_relations(tmp_path):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            """\
            def load_config():
                return 1


            class Worker:
                def __init__(self):
                    self.cfg = load_config()
            """
        )
    )
    index = RepoIndex(str(tmp_path))

    # only attribute/constant metadata is excluded: __init__ is real code
    report = index.impact("load_config")
    assert report is not None
    dependents = {r.entity.qualified_name for r in report.dependents}
    assert "Worker.__init__" in dependents
