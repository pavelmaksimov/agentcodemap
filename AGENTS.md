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
  `test_relative_import_resolves`).

Run `uv run pytest` after resolver changes.
