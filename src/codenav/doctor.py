"""Indexing diagnostics: what a scan of the roots actually did.

A bare "not found" / "(no matches)" says nothing about why: the symbol may be
absent from the source, or its file may never have been indexed — unsupported
extension, size cap, unreadable bytes, failed parse, pruned directory, or a
root that does not exist at all.  `codenav doctor` reports which of those
happened, so those situations can be told apart instead of guessed at.

Two rules shape the report:

* the walk is the index's own (``RepoIndex.scan_tree``), so it explains exactly
  the file set the other commands search, not a second implementation of the
  same filters;
* it reports only measured facts — the walk's decisions and the extraction
  signals the parser records (symbol count, ``<unknown>`` names, tree-sitter
  ERROR/MISSING nodes).  A clean parse is reported as a clean parse, never as
  proof that extraction is complete.

Compact summary by default; ``--verbose`` adds the paths behind the counts.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from codenav.index import (
    IGNORED_DIR,
    PARSE_FAILED,
    TOO_LARGE,
    UNREADABLE,
    UNSUPPORTED,
    RepoIndex,
)

# Root problems.  The first two make the command exit non-zero: the scan over
# such a root is empty for a reason that must not be read as "no code there",
# and a mistyped root must not pass for a successfully indexed empty project.
ROOT_MISSING = "does not exist"
ROOT_NOT_DIR = "not a directory"
ROOT_REDUNDANT = "redundant (covered by another root)"
BLOCKING_ROOT_PROBLEMS = (ROOT_MISSING, ROOT_NOT_DIR)

# Code-file skip reasons, in the order the report prints them.  Pruned
# directories are counted separately: one of them hides a whole subtree, not a
# single file.
SKIP_REASONS = (TOO_LARGE, UNREADABLE, PARSE_FAILED)
IGNORED_DIRS = f"{IGNORED_DIR}s"
# JSON spells the reasons as snake_case keys.
JSON_SKIP_KEYS = {
    TOO_LARGE: "too_large",
    UNREADABLE: "unreadable",
    PARSE_FAILED: "parse_failed",
}
UNKNOWN = "<unknown>"
# Extraction is never certified complete: the report lists the gaps it can
# detect and says so, both in --verbose text and in JSON details.
NO_COMPLETENESS_CLAIM = (
    "a clean parse does not prove that extraction is complete"
)


@dataclass
class RootReport:
    """One --root as given: where it points, whether it can be scanned, its yield."""

    given: str
    path: str  # realpath of the given root
    problem: str = ""  # "" when the root was scanned
    indexed: int = 0


@dataclass
class LangCount:
    files: int = 0
    symbols: int = 0


@dataclass
class Diagnostics:
    """Everything one scan of the roots produced."""

    roots: list[RootReport] = field(default_factory=list)
    languages: dict[str, LangCount] = field(default_factory=dict)
    indexed: int = 0
    symbols: int = 0
    # skip reason -> paths, in walk order (code files only)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    ignored_dirs: list[str] = field(default_factory=list)
    non_code: list[str] = field(default_factory=list)
    # indexed file -> number of symbols extracted there under the <unknown> name
    unknown_names: dict[str, int] = field(default_factory=dict)
    syntax_errors: list[str] = field(default_factory=list)
    without_symbols: list[str] = field(default_factory=list)

    @property
    def code_files(self) -> int:
        """Files with a supported language: every file the index could take."""
        return self.indexed + sum(len(paths) for paths in self.skipped.values())

    @property
    def blocking_roots(self) -> list[RootReport]:
        """Roots whose empty result must not read as a successful empty index."""
        return [r for r in self.roots if r.problem in BLOCKING_ROOT_PROBLEMS]

    @property
    def has_details(self) -> bool:
        """True when --verbose would add anything to the summary."""
        return bool(
            self.ignored_dirs
            or any(self.skipped.values())
            or self.unknown_names
            or self.syntax_errors
            or self.without_symbols
        )


def diagnose(roots: Sequence[str]) -> Diagnostics:
    """Scan ``roots`` the way RepoIndex does and describe the outcome.

    Every root is reported, including the ones that cannot be scanned and the
    ones another root already covers, so every file count keeps a visible
    numerator and denominator.  A file that cannot be read or parsed is
    reported and skipped; it never hides the other files of the scan.
    """
    kept = {os.path.realpath(r) for r in RepoIndex.canonical_roots(roots)}
    report = Diagnostics()
    seen: set[str] = set()
    for given in roots:
        if given in seen:
            continue
        seen.add(given)
        if not os.path.exists(given):
            problem = ROOT_MISSING
        elif not os.path.isdir(given):
            problem = ROOT_NOT_DIR
        elif os.path.realpath(given) not in kept:
            problem = ROOT_REDUNDANT
        else:
            problem = ""
        report.roots.append(RootReport(given, os.path.realpath(given), problem))

    scanned = {r.path: r for r in report.roots if not r.problem}
    for root in RepoIndex.canonical_roots(roots):
        entry = scanned.get(os.path.realpath(root))
        if entry is None:
            continue
        for item in RepoIndex.scan_tree(root):
            if item.skip == IGNORED_DIR:
                report.ignored_dirs.append(item.path)
                continue
            if item.skip == UNSUPPORTED:
                report.non_code.append(item.path)
                continue
            if item.parsed is None:
                report.skipped.setdefault(item.skip, []).append(item.path)
                continue
            report.indexed += 1
            entry.indexed += 1
            counts = report.languages.setdefault(item.lang or "", LangCount())
            counts.files += 1
            symbols = len(item.parsed.entities)
            counts.symbols += symbols
            report.symbols += symbols
            unknown = sum(1 for e in item.parsed.entities if e.name == UNKNOWN)
            if unknown:
                report.unknown_names[item.path] = unknown
            if item.parsed.syntax_errors:
                report.syntax_errors.append(item.path)
            if not item.parsed.entities:
                report.without_symbols.append(item.path)
    return report


def render_text(report: Diagnostics, verbose: bool = False) -> str:
    """Compact summary; ``verbose`` appends the paths behind the counts."""
    lines = ["roots:"]
    for root in report.roots:
        if root.problem:
            lines.append(f"  {root.given}: {root.problem}")
        else:
            lines.append(f"  {root.given} -> {root.path}: {root.indexed} files indexed")
    lines.append(
        f"files: {report.indexed} indexed of {report.code_files} code files; "
        f"{len(report.non_code)} non-code files skipped"
    )
    languages = ", ".join(
        f"{lang} {count.files} files/{count.symbols} symbols"
        for lang, count in sorted(report.languages.items())
    )
    lines.append(f"languages: {languages or 'none'}")
    skips = [f"{reason} {len(report.skipped.get(reason, []))}" for reason in SKIP_REASONS]
    skips.append(f"{IGNORED_DIRS} {len(report.ignored_dirs)}")
    lines.append(f"skipped: {', '.join(skips)}")
    lines.append(
        f"extraction: {report.symbols} symbols; "
        f"{sum(report.unknown_names.values())} <unknown> names in "
        f"{len(report.unknown_names)} files; syntax errors in "
        f"{len(report.syntax_errors)} files; {len(report.without_symbols)} files "
        "parsed without symbols"
    )
    if verbose:
        lines.extend(_detail_lines(report))
    elif report.has_details:
        lines.append("(paths behind these counts: --verbose)")
    return "\n".join(lines)


def _detail_lines(report: Diagnostics) -> list[str]:
    """Indented path lists, one block per non-empty group."""
    groups: list[tuple[str, list[str]]] = [
        (IGNORED_DIRS, report.ignored_dirs),
        *((reason, report.skipped.get(reason, [])) for reason in SKIP_REASONS),
        (
            "<unknown> names",
            [f"{path} ({count})" for path, count in report.unknown_names.items()],
        ),
        ("syntax errors", report.syntax_errors),
        ("parsed without symbols", report.without_symbols),
    ]
    lines: list[str] = []
    for title, paths in groups:
        if not paths:
            continue
        lines.append(f"  {title}:")
        lines.extend(f"    {path}" for path in paths)
    return ["details:", *lines, f"  note: {NO_COMPLETENESS_CLAIM}"]


def render_json(report: Diagnostics, verbose: bool = False) -> str:
    """Machine-readable form of the same report (``--format json``)."""
    data: dict = {
        "roots": [
            {
                "given": root.given,
                "path": root.path,
                "problem": root.problem,
                "files_indexed": root.indexed,
            }
            for root in report.roots
        ],
        "files": {
            "indexed": report.indexed,
            "code": report.code_files,
            "skipped_code": report.code_files - report.indexed,
            "skipped_non_code": len(report.non_code),
        },
        "languages": {
            lang: {"files": count.files, "symbols": count.symbols}
            for lang, count in sorted(report.languages.items())
        },
        "skipped": {
            **{
                JSON_SKIP_KEYS[reason]: len(report.skipped.get(reason, []))
                for reason in SKIP_REASONS
            },
            "ignored_dirs": len(report.ignored_dirs),
        },
        "extraction": {
            "symbols": report.symbols,
            "unknown_names": sum(report.unknown_names.values()),
            "files_with_unknown_names": len(report.unknown_names),
            "files_with_syntax_errors": len(report.syntax_errors),
            "files_without_symbols": len(report.without_symbols),
        },
    }
    if verbose:
        data["details"] = {
            "ignored_dirs": report.ignored_dirs,
            "skipped": {
                JSON_SKIP_KEYS[reason]: report.skipped[reason]
                for reason in SKIP_REASONS
                if report.skipped.get(reason)
            },
            "unknown_names": report.unknown_names,
            "syntax_errors": report.syntax_errors,
            "files_without_symbols": report.without_symbols,
            "note": NO_COMPLETENESS_CLAIM,
        }
    return json.dumps(data, indent=2)
