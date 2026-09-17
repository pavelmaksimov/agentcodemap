# codenav-research skill — durable facts

## What it is
A `skill://`-style agent skill for codebase research, installed at
`/home/user/dev/codenav/.agents/skills/codenav-research/SKILL.md` (also symlinked
from `.omp/agents/codenav-researcher.md`). It is the replacement for the
`grep`-based `codenav-researcher` subagent: the subagent is a thin wrapper that
delegates to the skill, so the skill is the single source of truth.

## Measured behaviour (2026-09-12, 3 complete passes, 42 paired tasks)
Model: `openrouter/z-ai/glm-5.3-flash`, `--thinking high`, `code-master-sleep`
checkout. Arms: `base` (read/grep/glob/bash, no codenav), `nav` (same + skill +
hint), `navx` (read/glob/bash — no grep — + skill + hint).

| Arm  | turns | tokens | cost | hard | chars |
|------|-------|--------|------|------|-------|
| base | 8.9   | 2.87M  | 0.133 | 0.90 | 914k |
| nav  | 9.8   | 3.00M  | 0.141 | 0.95 | 1.00M |
| navx | 10.2  | 2.15M  | 0.132 | 0.90 | 528k |

Paired navx vs base: median −12.7% tokens (pooled 42 tasks), −42% output
chars, cost flat. Median is the honest statistic here — the mean is +1.8%,
dragged up by a few tasks where navx went long (the skill's `impact`/`graph`
commands produce long structured output on big graphs).

## Key findings
- **navx is the only arm that consistently wins.** nav (skill + grep tool) is
  neutral-to-worse on tokens because the skill text itself is loaded into every
  system prompt (cacheable but still billed on cache miss) and the agent
  sometimes over-calls codenav.
- **The grep tool is the single biggest lever.** Removing it from the skill
  (navx) cuts output chars by 42% with no accuracy loss. The skill's own
  `grep` section is dead weight for this corpus: the CLI already exposes
  `codenav grep`, and the agent prefers `bash grep` when both are available.
- **Accuracy is saturated.** `hard` is 0.85–1.00 across all arms; the
  difference between arms is noise, not signal. The correctness axis is not a
  discriminator — any arm that reaches for codenav is fine.
- **Adoption is the bottleneck, not the skill text.** The agent reaches for
  codenav only when the system prompt explicitly advertises the commands
  (`CODENAV_HINT` block). Skill text alone does not trigger adoption
  (`codenav_calls` is 0 on the 16-task no-advertisement pass for all arms).

## Decisions
- Ship the v2 skill (replace + turn rules), drop the `codenav-researcher`
  subagent's grep tool, keep the skill as the single source of truth.
- The benchmark runner (`benchmarks/codebase-research/ab_tokens.py`) parses
  real omp usage from the JSONL stream (turn_end message.usage), not from
  stderr.

## Caveats
- The 16-task advertised pass (`z-final-16`) failed: the opencode workspace
  hit its weekly usage limit mid-run (429, retry-after ~31h). Only the base arm
  completed. The numbers above are from the 3 complete passes.
- Pass-to-pass variance is large (base tokens 1.9M–3.8M for the same tasks),
  so single-pass means are unreliable; medians and paired deltas are the
  honest summary.