import os

import pytest

from codenav.cli import main


def test_doctor_counts_indexed_files_and_languages(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "alpha.py").write_text("def alpha():\n    return 1\n")
    (root / "beta.py").write_text("class Beta:\n    value = 1\n")
    (root / "gamma.js").write_text("function gamma() { return 1; }\n")
    (root / "notes.txt").write_text("notes\n")

    main(["doctor", "--root", str(root)])

    out = capsys.readouterr().out
    assert f"{root} -> {os.path.realpath(root)}: 3 files indexed" in out
    assert "files: 3 indexed of 3 code files; 1 non-code files skipped" in out
    assert "python 2 files/3 symbols" in out
    assert "javascript 1 files/1 symbols" in out
    assert "notes.txt" not in out


def test_doctor_missing_root_is_reported_and_fails_the_command(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "alpha.py").write_text("def alpha():\n    return 1\n")
    missing = tmp_path / "vendor"

    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--root", str(root), str(missing)])

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"{missing}: does not exist" in out
    # the root that can be scanned still reports its files
    assert "files: 1 indexed of 1 code files" in out


def test_doctor_root_that_is_a_file_is_reported(tmp_path, capsys):
    module = tmp_path / "module.py"
    module.write_text("def alpha():\n    return 1\n")

    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--root", str(module)])

    assert exc.value.code == 1
    assert f"{module}: not a directory" in capsys.readouterr().out


def test_doctor_reports_a_root_covered_by_another(tmp_path, capsys):
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "alpha.py").write_text("def alpha():\n    return 1\n")

    main(["doctor", "--root", str(root), str(root / "pkg")])

    out = capsys.readouterr().out
    assert f"{root / 'pkg'}: redundant (covered by another root)" in out
    # the shared file is counted once, under the root that was kept
    assert "files: 1 indexed of 1 code files" in out


def test_doctor_reports_skip_reasons_and_paths_only_when_verbose(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "ok.py").write_text("def ok():\n    return 1\n")
    (root / "huge.py").write_text("x = 1\n" * 100_000)
    (root / "binary.py").write_bytes(b"def broken():\n\xff\xfe\x00\n")
    junk = root / "node_modules"
    junk.mkdir()
    (junk / "hidden.py").write_text("def hidden():\n    return 1\n")

    main(["doctor", "--root", str(root)])

    compact = capsys.readouterr().out
    assert "files: 1 indexed of 3 code files; 0 non-code files skipped" in compact
    assert "skipped: too large 1, unreadable 1, parse failed 0, ignored dirs 1" in compact
    assert "huge.py" not in compact
    assert "binary.py" not in compact
    assert "(paths behind these counts: --verbose)" in compact

    main(["doctor", "--root", str(root), "--verbose"])

    verbose = capsys.readouterr().out
    assert "huge.py" in verbose
    assert "binary.py" in verbose
    assert "node_modules" in verbose


def test_doctor_reports_extraction_warnings(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "bad.py").write_text("def broken(:\n    value = 1\n")
    (root / "types.go").write_text("package main\n\ntype T struct {\n\tA int\n}\n")
    (root / "plain.c").write_text("struct S {\n  int a;\n};\n")
    (root / "imports.py").write_text("import os\n")

    main(["doctor", "--root", str(root), "--verbose"])

    out = capsys.readouterr().out
    assert "1 <unknown> names in 1 files" in out
    assert "syntax errors in 1 files" in out
    assert "files parsed without symbols" in out
    assert f"  <unknown> names:\n    {root / 'types.go'} (1)" in out
    assert f"  syntax errors:\n    {root / 'bad.py'}" in out
