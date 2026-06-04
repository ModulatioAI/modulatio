---
freshness_class: stable
assembler_skill: code-assembly
---
# Code standard (baseline)

Quality bar for code artifacts (`artifact_kind: code`, and code-bearing
`application` work). QC enforces this; producers follow it. Shipped BASELINE —
grows from QC self-healing fixes and human feedback. Team/project standards
override anything here.

## Correctness (load-bearing — reject on a miss)
- The code does what the task specified and runs without import or syntax errors.
- If tests are part of the deliverable, or tests exist for the code being
  touched, they pass. New non-trivial logic ships with at least a basic test.
- Edge cases and error paths are handled, not ignored — no bare `except: pass`
  swallowing failures; no unhandled None / empty / boundary input on the main path.

## Hygiene
- No linter errors; style consistent with the surrounding code.
- Types and signatures are coherent; names say what the thing is and does.
- No dead code, commented-out blocks left in, or stray debug prints.

## Safety
- No secrets or credentials committed in source.
- No obvious injection (shell / SQL / path traversal) on attacker-influenced
  input; validate external input at the boundary.

## Completeness
- No stubs, `TODO`, `pass`-only bodies, or "implement me" left where the task
  asked for working behavior. If something is genuinely out of scope, say so
  explicitly rather than shipping a silent gap.
