"""Seed-skill ↔ fallback-constant sync guard.

Every orchestrator prompt is loaded via ``Orchestrator._prompt(name, fallback)``:
the shared-vault seed skill (``_seed_skills/<name>.md``) is the source of truth
when present; the inline ``_*_PROMPT`` constant is the backstop for fresh clones
and unseeded tests. The two MUST stay byte-identical or an edit to one silently
diverges from the other — the only defense was a "keep in sync" comment.

This guard turns that comment into an enforced invariant for EVERY seed/fallback
pair, not just the runbook (Nemo, exec-widen code review 2026-06-19 — generalized).
Constant-only prompts with no shipped seed (``qc-patch``, ``wave-reflect``) have
nothing to sync and are intentionally absent.
"""
import re
from pathlib import Path

import pytest

from modulatio import orchestration

_SEED_DIR = Path(orchestration.__file__).parent / "_seed_skills"
_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

# (seed-skill name, fallback constant) — only pairs that SHIP a seed .md.
#
# KNOWN DEBT (2026-06-19): 11 of these 14 pairs have already DIVERGED in content
# — the seed .md (source of truth, used when the vault is seeded = normal installs)
# was iterated while the inline constant (the unseeded backstop, exercised by this
# test suite + fresh-install edge cases) was left stale. This is a real
# test-fidelity + maintenance hazard, surfaced by generalizing Nemo's runbook nit.
# The diverged pairs are marked xfail so this guard (a) protects the in-sync pairs
# from future drift and (b) DOCUMENTS the debt rather than hiding it. Reconciling
# them (pick source of truth per prompt → sync → re-review) is tracked separately;
# when one is reconciled, drop its xfail so the guard locks it in.
_DIVERGED = {
    "coding-diff", "drafter", "drafter-edit", "leader", "leader-converse",
    "leader-verify", "qc", "researcher", "skill-create", "task-plan", "win-codify",
}

_RAW_PAIRS = [
    ("coding-diff", orchestration._DRAFTER_DIFF_PROMPT),
    ("drafter", orchestration._DRAFTER_EXECUTE_PROMPT),
    ("drafter-edit", orchestration._DRAFTER_EDIT_PROMPT),
    ("drafter-patch", orchestration._DRAFTER_PATCH_PROMPT),
    ("drafter-revise", orchestration._DRAFTER_REVISE_PROMPT),
    ("leader", orchestration._LEADER_DECOMPOSE_PROMPT),
    ("leader-converse", orchestration._LEADER_CONVERSE_PROMPT),
    ("leader-runbook", orchestration._LEADER_RUNBOOK),
    ("leader-verify", orchestration._LEADER_VERIFY_PROMPT),
    ("qc", orchestration._QC_REVIEW_PROMPT),
    ("researcher", orchestration._RESEARCHER_FETCH_PROMPT),
    ("skill-create", orchestration._SKILL_CREATE_PROMPT),
    ("task-plan", orchestration._TASK_PLAN_PROMPT),
    ("win-codify", orchestration._WIN_CODIFY_PROMPT),
]

_PAIRS = [
    pytest.param(
        name, const,
        marks=pytest.mark.xfail(
            reason="known seed/constant drift — tracked for reconciliation",
            strict=True,
        ),
    ) if name in _DIVERGED else pytest.param(name, const)
    for name, const in _RAW_PAIRS
]


@pytest.mark.parametrize("name,constant", _PAIRS, ids=[p[0] for p in _RAW_PAIRS])
def test_seed_skill_matches_fallback_constant(name, constant):
    """The shipped seed .md body equals its inline fallback constant.

    Source of truth is the seed file; the constant is the unseeded backstop —
    if this fails, an edit landed in one but not the other. Fix BOTH.
    """
    seed = _SEED_DIR / f"{name}.md"
    assert seed.exists(), f"seed skill missing: {seed}"
    body = _FRONTMATTER.sub("", seed.read_text(), count=1)
    assert body.strip() == constant.strip(), (
        f"{name}.md has DIVERGED from its fallback constant "
        f"(orchestration._*_PROMPT). Edit BOTH — the seed file is the source of "
        f"truth, the constant is the unseeded backstop."
    )
