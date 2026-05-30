#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Deterministic smoke: the verify-goal wall (no LLM).

The engine drops standalone verify/review goals (the Leader can mint them but
they never reach a producer); a producing goal that merely DEMANDS rigorous
sources is kept. Gates on the primary verb.

Run from repo root:  .venv/bin/python scripts/smoke/qc-thesis/smoke_verify_goal_wall.py
"""
from modulatio.orchestration import _is_standalone_verification_goal as v

DROP = [
    "Verify that all claims are correctly sourced",
    "Review the analysis for accuracy and completeness",
    "Validate the dataset against the schema",
    "Audit the report's citations",
    "Fact-check the figures",
    "QA the final document",
]
KEEP = [
    "Produce the analysis, grounded in rigorous, credible sources",
    "Research current sources on the conflict and summarize",
    "Draft the paper with proper citations to primary sources",
    "Build a data validator module",          # produces a tool, not a check
    "Develop a verification harness",          # leads with a produce verb
]

fails = []
for d in DROP:
    print(f"[drop?] {v(d)!s:5} <- {d}")
    if not v(d):
        fails.append(f"FALSE NEGATIVE (verify goal slipped through): {d!r}")
for d in KEEP:
    print(f"[keep?] {(not v(d))!s:5} <- {d}")
    if v(d):
        fails.append(f"FALSE POSITIVE (dropped a producing goal): {d!r}")

print("RESULT:", "PASS" if not fails else "FAIL " + "; ".join(fails))
raise SystemExit(1 if fails else 0)
