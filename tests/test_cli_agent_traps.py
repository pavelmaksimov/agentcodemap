"""Invocations agents get wrong most often (session review): each used to fail silently or cost a turn."""

import pytest

from codenav.cli import main


def _two_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "settings.py").write_text("class Settings:\n    value = 1\n\n\ndef only_first():\n    return 1\n")
    (second / "settings.py").write_text("class Settings:\n    value = 2\n\n\ndef only_second():\n    return 2\n")
    return first, second


def test_repeated_root_flags_accumulate(tmp_path, capsys):
    first, second = _two_roots(tmp_path)

    main(["symbol", "only_first", "only_second", "--root", str(first), "--root", str(second)])

    output = capsys.readouterr().out
    assert "### only_first" in output
    assert "### only_second" in output


def test_names_swallowed_by_root_are_taken_back(tmp_path, capsys):
    first, second = _two_roots(tmp_path)

    main(["symbol", "--root", str(first), str(second), "only_first"])

    captured = capsys.readouterr()
    assert "### only_first" in captured.out
    assert "'only_first'" in captured.err
    assert "before --root" in captured.err


def test_root_without_any_name_explains_the_order(tmp_path, capsys):
    first, _ = _two_roots(tmp_path)

    with pytest.raises(SystemExit):
        main(["impact", "--root", str(first)])

    assert "before --root" in capsys.readouterr().err


def test_grep_pattern_swallowed_by_root_is_taken_back(tmp_path, capsys):
    first, _ = _two_roots(tmp_path)

    main(["grep", "--root", str(first), "only_first"])

    assert "settings.py:5-6::only_first function" in capsys.readouterr().out


def test_grep_ignore_case_flag(tmp_path, capsys):
    first, _ = _two_roots(tmp_path)

    main(["grep", "ONLY_FIRST", "-i", "--root", str(first)])

    assert "only_first" in capsys.readouterr().out


def test_grep_basic_regex_alternation_is_retried_as_python_alternation(tmp_path, capsys):
    first, _ = _two_roots(tmp_path)

    main(["grep", "only_first\\|value", "--root", str(first)])

    output = capsys.readouterr().out
    assert "literal pipe" in output
    assert "'only_first|value'" in output
    assert "only_first" in output.split("\n", 1)[1]
    assert "value = 1" in output


def test_grep_literal_pipe_that_matches_is_left_alone(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pipes.py").write_text("def pipe():\n    return 'a|b'\n")

    main(["grep", "a\\|b", "--root", str(root)])

    output = capsys.readouterr().out
    assert "literal pipe" not in output
    assert "pipes.py:1-2::pipe function" in output


def test_outline_without_path_maps_the_current_directory(tmp_path, capsys, monkeypatch):
    first, _ = _two_roots(tmp_path)
    monkeypatch.chdir(first)

    main(["outline", "--top-level"])

    assert "C Settings" in capsys.readouterr().out


def test_outline_accepts_root_like_the_other_commands(tmp_path, capsys):
    first, _ = _two_roots(tmp_path)

    main(["outline", "--root", str(first), "--top-level"])

    assert "F only_first" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["symbol", "impact", "trace", "info"])
def test_same_name_in_two_roots_is_never_hidden(tmp_path, capsys, command):
    first, second = _two_roots(tmp_path)

    main([command, "Settings", "--root", str(first), str(second)])

    output = capsys.readouterr().out
    assert "2 symbols match 'Settings'" in output
    assert "second/settings.py:1::Settings" in output


def test_path_names_other_matches_of_both_ends(tmp_path, capsys):
    first, second = _two_roots(tmp_path)

    main(["path", "Settings", "only_first", "--root", str(first), str(second)])

    assert "2 symbols match 'Settings'" in capsys.readouterr().out


def test_unique_name_prints_no_ambiguity_note(tmp_path, capsys):
    first, second = _two_roots(tmp_path)

    main(["symbol", "only_first", "--root", str(first), str(second)])

    assert "symbols match" not in capsys.readouterr().out


def test_help_shows_names_before_root(capsys):
    with pytest.raises(SystemExit):
        main(["symbol", "--help"])

    assert "usage: codenav symbol NAME [NAME ...] [--root DIR [DIR ...]]" in capsys.readouterr().out


def test_symbol_given_a_file_path_points_to_outline(tmp_path):
    first, _ = _two_roots(tmp_path)
    path = str(first / "settings.py")

    with pytest.raises(SystemExit) as raised:
        main(["symbol", path, "--root", str(first)])

    assert f"codenav outline {path}" in str(raised.value.code)
