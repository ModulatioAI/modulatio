---
# Uncomment to cap daily producer-escalation spend per cost class.
# Missing or commented fields = unlimited for that tier (back-compat).
# paid_cloud_escalations_per_day: 10
# premium_cloud_escalations_per_day: 3
tags: [modulatio, comptroller, testing4]
---

# Comptroller — escalation budget

Daily caps on producer escalations. Comptroller reads this file's
frontmatter (`paid_cloud_escalations_per_day`,
`premium_cloud_escalations_per_day`) and appends each authorized
escalation to `comptroller-ledger.md`. Denied escalations fall through
to a last-ditch same-agent retry and open a `BLOCKER` ticket whose
`refresh_at` is tomorrow's UTC midnight — the orchestrator's
auto-resume picks it up on the next billing-cycle rollover.

`free-local` escalations bypass the gate entirely (no API cost to
meter). Uncomment a cap above to activate it; leave all commented
to preserve unlimited spend.
