# Smoke round: the QC-thesis arc (2026-05-30)

Durable evidence for the 9-commit arc on `feat/concurrent-waves-eval` since `51570a1`.
Hull review: `~/Message in a Bottle/2026-05-30-cowboy-claude-to-nemo-qc-thesis-hull.md`.

## Reproducible deterministic smokes (no LLM, no network)
- `smoke_overflow_cures.py` — http_get cap (39× on a 1.2M-char page) + truncate_tool_result bound.
- `smoke_verify_goal_wall.py` — the verify-goal detector: drops verify goals, keeps producing goals.

Run from repo root: `.venv/bin/python scripts/smoke/qc-thesis/<smoke>.py`

## Behavior → proving test(s) (the suite is the primary evidence; 2349 passed)
| Behavior | Commit | Tests |
|---|---|---|
| http_get cap + HTML→text | 15f669a | test_tools.py::test_http_get_caps_huge_body, _strips_html_to_text, _does_not_strip_json, _caps_error_body |
| truncation on arrival | d157caa | test_tool_summarization.py::test_truncate_*, test_runner_truncates_* |
| sweep bounding (cap-aware) | 15f669a/c1bc9e1 | test_skills.py::test_seed_planning_skills_carry_sweep_bounding_guidance |
| default standards seed tier | 33102a9 | test_standards.py::test_seed_baseline_*, _curated_stack_over_seed, _bundled_seed_*_teeth |
| verify-goal wall | 3934eac | test_orchestration.py::test_is_standalone_verification_goal_*, test_decompose_drops_*, _keeps_all_when_only_* |
| rigorous-sourcing skill | 3934eac | test_skills.py::test_rigorous_sourcing_skill_ships_for_producers |
| Product Quality Report (no tickets) | b1ee3e7 | test_orchestration.py::test_leader_verify_satisfied_completes_goal_no_ticket, _on_the_fence_ships_and_records_recommendations, _disappointed_auto_redo_then_ships; test_delivery.py::test_quality_report_* |
| withhold on blocked task/goal | c1bc9e1 | test_delivery.py::test_blocked_task_ids_*, test_blocked_goal_ids_* |

## Live acceptance run (the real proof — the arc end-to-end)
`~/modulatio/cliftest/modulatio1/runs/20260530T195221Z-a057ea/` — current-events objective (Israel/US–Iran war), full lineup (deepseek leader, grok-build producers, grok-4.3 QC):
- 2 goals, both COMPLETED · 4 tasks · **0 storm fragments · 0 tickets · 0 refusals**
- truncation fired (raw persisted to `<run>/tool_calls/*.txt`)
- shipped a grounded on-topic analysis (14 source markers) + Product Quality Report.docx with honest caveats.
- Contrast: the same objective earlier off-topic-hallucinated, then ticket-looped, then storm-failed — one root cause peeled at a time.
