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
    dependents = {e.qualified_name for e in report.dependents}
    assert {"use", "test_use"} <= dependents
    assert "other_user" not in dependents


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
    depends = {e.qualified_name for e in report.depends_on}
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
        e.qualified_name for e in container.depends_on
    }

    job_store = index.impact("JobStore")
    assert job_store is not None
    assert "Services" in {e.qualified_name for e in job_store.dependents}

    chat_repo = index.impact("ChatMessageRepository")
    assert chat_repo is not None
    # MODE constant value and plain_probe's local string are not DI positions;
    # the module-level fallback_repo registration is
    assert {e.qualified_name for e in chat_repo.dependents} == {
        "Services",
        "fallback_repo",
    }

    fallback = index.impact("fallback_repo")
    assert fallback is not None
    assert {"ChatMessageRepository"} == {
        e.qualified_name for e in fallback.depends_on
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
        entity.qualified_name for entity in report.dependents
    }
    assert "_patch_gitlab_connection_errors" not in {
        entity.qualified_name for entity in report.dependents
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
    dependents = {entity.qualified_name for entity in report.dependents}
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
    depends_on = {entity.qualified_name for entity in report.depends_on}
    assert "Message" in depends_on
    assert "Message.content" not in depends_on


def test_skip_dirs(tmp_path):
    root = make_repo(tmp_path)
    junk = root / "node_modules"
    junk.mkdir()
    (junk / "bad.py").write_text("def noise():\n    pass\n")
    index = RepoIndex(str(root))
    assert not index.find_symbol("noise")
