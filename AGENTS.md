# codenav agent guidance

## Impact resolver

Impact relations must be syntax- and type-aware. Do not resolve a method or
attribute solely from its short name: `client.session` is not
`ChatMessage.session`, and `items.append()` is not
`ChatMessageRepository.append`.

Keep qualified member references and declared/inferred receiver types in the
resolver. Bare-name fallback is allowed only for an unambiguous bare symbol.
When changing this logic, preserve regression coverage for:

- `ChatMessage.session` versus `_patch_gitlab_connection_errors`;
- typed `ChatMessageRepository.append` versus an unrelated `items.append()`;
- keyword arguments such as `content` not resolving to `Message.content`.

Run `uv run pytest` after resolver changes.
