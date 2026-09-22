# codenav agent guidance

## Impact resolver

Impact relations must be syntax- and type-aware. Do not resolve a method or
attribute solely from its short name: `client.session` is not
`ChatMessage.session`, and `items.append()` is not
`ChatMessageRepository.append`.

Keep qualified member references and declared/inferred receiver types in the
resolver. Bare-name fallback is allowed only for an unambiguous bare symbol.
Module-qualified member access must resolve module-level symbols: a receiver
bound by an import (`use_cases.start_code_review` after
`from pkg import use_cases`, or `import pkg.use_cases as uc`) or spelled as a
dotted module path (`pkg.use_cases.start_code_review`) matches symbols
defined at module level in the denoted module file only.
When changing this logic, preserve regression coverage for:

- `ChatMessage.session` versus `_patch_gitlab_connection_errors`;
- typed `ChatMessageRepository.append` versus an unrelated `items.append()`;
- keyword arguments such as `content` not resolving to `Message.content`;
- module aliases and dotted module paths (`test_module_alias_resolves_*`,
  `test_module_import_as_and_dotted_receiver`,
  `test_module_resolution_rejects_unrelated_receivers`,
  `test_relative_import_resolves`);
- reference-site kinds (`ReferenceObs`: call/inheritance/param/reference/
  return/string) surviving into impact and graph output
  (`test_relation_kinds_name_the_reference_site`,
  `test_relation_kinds_include_the_reference_lines`,
  `test_annotation_positions_split_into_param_and_return`), and `--kind`
  filtering of report lists and path walks
  (`test_graph_kind_filter_drops_edges_of_other_kinds`);
- dunder attribute/constant metadata (`__all__`, `__version__`) staying out of
  relations while dunder methods (`__init__`) stay in them
  (`test_dunder_metadata_not_in_relations`, `test_dunder_methods_stay_in_relations`).

Run `uv run pytest` after resolver changes.


benchmarks/code-master/sleep-runs-notes.md Внутри: эталонная команда прогона (уже с muse-spark-1.3-contributor), 6 
разборов ошибок (--pi-path для omp, PATH для codenav, --max-tasks не режет tasks-file, «no final answer» от omp, отсутствие реальных токенов, косметика replay: mock), наблюдения по сплитам/гейту и чеклист перед следующим прогоном.