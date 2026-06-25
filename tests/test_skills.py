"""Tests for the Skill registry (slice #6a foundation).

Skills live in two locations, searched in order:

1. Shared defaults at ``~/Obsidian/Claude/projects/modulatio/skills/<name>.md``.
2. Per-project overrides at ``<project_vault>/skills/<name>.md`` — fully
   replaces the shared entry when present (not a stack — a skill is a
   coherent whole like a research snapshot, not a rules list).

Front-matter carries the full routing schema: tool_loadout (narrow tool
list for efficient dispatch), model_tier (which LLM tier runs this skill),
cost_class (free-local / paid-cloud / premium-cloud), capability_tags
(free-form taxonomy consumed by slice #8 capability-floor routing),
standards_domain (fixed or None for artifact-kind-agnostic skills), and
Research-First freshness metadata.

Orchestrator still uses hardcoded templates in slice #6a; the registry
is declarative only until slice #6b wires it in.
"""

from __future__ import annotations

from pathlib import Path


from modulatio import skills, vault


def _write_skill(
    path: Path,
    *,
    name: str,
    description: str = "",
    tool_loadout: str = "",
    standards_domain: str = "",
    model_tier: str = "generalist",
    cost_class: str = "paid-cloud",
    capability_tags: str = "",
    required_capabilities: str = "",
    freshness_class: str = "semi-stable",
    last_verified_at: str = "2026-04-20",
    body: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"tool_loadout: {tool_loadout}",
        f"standards_domain: {standards_domain}",
        f"model_tier: {model_tier}",
        f"cost_class: {cost_class}",
        f"capability_tags: {capability_tags}",
        f"required_capabilities: {required_capabilities}",
        f"freshness_class: {freshness_class}",
        f"last_verified_at: {last_verified_at}",
        "---",
        "",
        body,
    ]
    path.write_text("\n".join(lines))


# ── load ────────────────────────────────────────────────────────────────────

def test_skills_load_missing_returns_empty_entry(tmp_path, monkeypatch):
    """No shared file, no project file → empty entry, not an error. Skills
    are additive leverage; a missing skill is not a crash."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    entry = skills.load_with_metadata("no-such-skill")
    assert entry.prompt_template == ""
    assert entry.sources == ()
    assert entry.tool_loadout == ()
    assert entry.capability_tags == ()
    assert entry.required_capabilities == ()
    assert entry.model_tier is None
    assert entry.cost_class is None


def test_skills_load_shared_only(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    _write_skill(
        shared / "drafter.md",
        name="drafter",
        description="Produces artifacts of the declared kind",
        tool_loadout="fs",
        model_tier="generalist",
        cost_class="paid-cloud",
        capability_tags="writing, prose, markdown",
        body="Drafter prompt template canonical pointer: orchestration._DRAFTER_EXECUTE_PROMPT",
    )
    entry = skills.load_with_metadata("drafter")
    assert "canonical pointer" in entry.prompt_template
    assert entry.tool_loadout == ("fs",)
    assert entry.model_tier == "generalist"
    assert entry.cost_class == "paid-cloud"
    assert entry.capability_tags == ("writing", "prose", "markdown")
    assert entry.freshness_class == "semi-stable"
    assert entry.last_verified_at == "2026-04-20"
    assert str(shared / "drafter.md") in entry.sources


def test_skills_project_local_replaces_shared_when_present(tmp_path, monkeypatch):
    """A skill is a coherent whole — project-local does NOT stack on shared
    (unlike standards). When both exist, the project-local file fully
    replaces the shared file. Teams customizing a skill ship their own
    coherent definition; they don't append lines to the baseline."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")

    _write_skill(
        shared / "drafter.md",
        name="drafter",
        description="shared baseline",
        tool_loadout="fs",
        model_tier="generalist",
        capability_tags="writing",
        body="SHARED template body.",
    )
    _write_skill(
        tmp_path / "projects" / "tst" / "skills" / "drafter.md",
        name="drafter",
        description="house-style tuned",
        tool_loadout="fs, web",
        model_tier="reasoning-heavy",
        cost_class="premium-cloud",
        capability_tags="writing, contrarian-argument, house-style",
        body="PROJECT template body (house-style voice tuning).",
    )

    entry = skills.load_with_metadata("drafter", project_code="TST")
    # Project body fully replaces shared.
    assert entry.prompt_template.strip() == "PROJECT template body (house-style voice tuning)."
    assert "SHARED" not in entry.prompt_template
    # Routing fields come from the project-local file.
    assert entry.tool_loadout == ("fs", "web")
    assert entry.model_tier == "reasoning-heavy"
    assert entry.cost_class == "premium-cloud"
    assert entry.capability_tags == ("writing", "contrarian-argument", "house-style")
    # source_path identifies the winning file so callers can audit.
    assert str(tmp_path / "projects" / "tst" / "skills" / "drafter.md") in entry.sources


def test_skills_load_returns_prompt_template_string(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    _write_skill(shared / "qc.md", name="qc", body="QC prompt body.")
    assert skills.load("qc").strip() == "QC prompt body."


# ── save ────────────────────────────────────────────────────────────────────

def test_skills_save_round_trip_preserves_all_routing_fields(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)

    original = skills.Skill(
        name="researcher",
        description="Cache-first web research with source rigor",
        prompt_template="Researcher prompt body goes here.\n",
        tool_loadout=("web", "http", "fs"),
        standards_domain=None,
        model_tier="tool-using",
        cost_class="paid-cloud",
        capability_tags=("web-research", "citation", "freshness"),
        freshness_class="active",
        last_verified_at="2026-04-20",
        sources=(),
    )
    path = skills.save(original)

    assert path.exists()
    loaded = skills.load_with_metadata("researcher")
    assert loaded.prompt_template.strip() == original.prompt_template.strip()
    assert loaded.tool_loadout == original.tool_loadout
    assert loaded.model_tier == original.model_tier
    assert loaded.cost_class == original.cost_class
    assert loaded.capability_tags == original.capability_tags
    assert loaded.freshness_class == original.freshness_class
    assert loaded.last_verified_at == original.last_verified_at


def test_skills_save_to_project_writes_under_project_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")

    s = skills.Skill(
        name="drafter",
        description="custom",
        prompt_template="team tuned",
        tool_loadout=("fs",),
        standards_domain=None,
        model_tier="generalist",
        cost_class="paid-cloud",
        capability_tags=("writing",),
        freshness_class=None,
        last_verified_at=None,
        sources=(),
    )
    path = skills.save(s, project_code="TST")
    assert path == tmp_path / "projects" / "tst" / "skills" / "drafter.md"
    assert path.exists()


# ── list ────────────────────────────────────────────────────────────────────

def test_skills_list_returns_sorted_names_from_shared(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    for n in ("researcher", "drafter", "qc", "coordinator", "leader"):
        _write_skill(shared / f"{n}.md", name=n)

    names = skills.list_skills()
    # Shared entries must all appear, sorted. Package seed dir
    # (coding, code-review) may also appear — assert subset rather
    # than equality so the existing intent (sorted-from-shared) holds
    # under the bundled-seed change.
    expected_subset = {"coordinator", "drafter", "leader", "qc", "researcher"}
    assert expected_subset.issubset(set(names))
    assert names == sorted(names)


def test_skills_list_merges_shared_and_project(tmp_path, monkeypatch):
    """list_skills returns the union of shared + project names (sorted,
    deduped). Gives Leader one call to see everything available for
    skill-gap detection in slice #6b."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")

    _write_skill(shared / "drafter.md", name="drafter")
    _write_skill(shared / "qc.md", name="qc")
    _write_skill(
        tmp_path / "projects" / "tst" / "skills" / "house-style.md",
        name="house-style",
    )
    _write_skill(
        tmp_path / "projects" / "tst" / "skills" / "drafter.md",
        name="drafter",  # override, should dedupe
    )

    names = skills.list_skills(project_code="TST")
    # Subset assertion (seed dir adds canonical bundled skills) +
    # dedupe check on the override case + sortedness.
    assert {"drafter", "house-style", "qc"}.issubset(set(names))
    assert names == sorted(names)
    assert names.count("drafter") == 1, "override must dedupe"


# ── required_capabilities floor (slice #9b) ────────────────────────────────

def test_skill_required_capabilities_defaults_to_empty_tuple(tmp_path, monkeypatch):
    """Skills without a required_capabilities frontmatter entry load as
    the empty tuple — back-compat with every skill file seeded before
    #9b. No floor = no dispatch impact."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    shared.mkdir()
    (shared / "legacy.md").write_text(
        "---\nname: legacy\n---\n\nlegacy prompt body.\n"
    )
    entry = skills.load_with_metadata("legacy")
    assert entry.required_capabilities == ()


def test_skill_required_capabilities_loads_from_frontmatter(tmp_path, monkeypatch):
    """A skill can declare a capability floor on the agent that executes
    it. Dispatch (slice #9b) reads this as a hard filter independent of
    whatever the task's own required_capabilities declares."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    _write_skill(
        shared / "shell-runner.md",
        name="shell-runner",
        required_capabilities="shell-access, structured-output",
    )
    entry = skills.load_with_metadata("shell-runner")
    assert entry.required_capabilities == ("shell-access", "structured-output")


def test_skill_executor_defaults_to_llm(tmp_path, monkeypatch):
    """Slice #9e: skills default to ``executor = "llm"`` — back-compat
    for every seeded skill. Only skills that explicitly declare
    ``executor: tool`` in frontmatter dispatch through the tool
    registry."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    shared.mkdir()
    (shared / "legacy.md").write_text(
        "---\nname: legacy\n---\n\nlegacy prompt body.\n"
    )
    entry = skills.load_with_metadata("legacy")
    assert entry.executor == "llm"


def test_skill_executor_parses_tool_type_from_frontmatter(tmp_path, monkeypatch):
    """A skill declaring ``executor: tool`` in frontmatter loads it
    correctly. The orchestrator uses this to route execution to the
    tool registry instead of an LLM runner."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    _write_skill(
        shared / "url-fetcher.md",
        name="url-fetcher",
        body="URL fetch skill — tool-executed.",
    )
    # _write_skill doesn't know about executor yet; append it directly.
    path = shared / "url-fetcher.md"
    text = path.read_text().replace(
        "name: url-fetcher\n", "name: url-fetcher\nexecutor: tool\n"
    )
    path.write_text(text)
    entry = skills.load_with_metadata("url-fetcher")
    assert entry.executor == "tool"


def test_skill_save_round_trips_executor(tmp_path, monkeypatch):
    """save() serializes executor and the loader reads it back."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    original = skills.Skill(
        name="url-fetcher",
        description="Fetch URLs as text",
        prompt_template="(tool-only, no LLM prompt)\n",
        tool_loadout=("http_get",),
        executor="tool",
    )
    skills.save(original)
    loaded = skills.load_with_metadata("url-fetcher")
    assert loaded.executor == "tool"
    assert loaded.tool_loadout == ("http_get",)


def test_skill_save_round_trips_required_capabilities(tmp_path, monkeypatch):
    """save() serializes required_capabilities and the loader reads it
    back. Round-trip preserves order and both tag entries."""
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)

    original = skills.Skill(
        name="reasoning-producer",
        description="producer skill that needs heavy reasoning",
        prompt_template="reasoning-producer prompt body.\n",
        tool_loadout=(),
        standards_domain=None,
        model_tier="reasoning-heavy",
        cost_class="paid-cloud",
        capability_tags=("producer",),
        required_capabilities=("reasoning-heavy", "long-context"),
        freshness_class="active",
        last_verified_at="2026-04-21",
        sources=(),
    )
    skills.save(original)

    loaded = skills.load_with_metadata("reasoning-producer")
    assert loaded.required_capabilities == ("reasoning-heavy", "long-context")


# ── Package-bundled seed skills (ship-with-the-code) ───────────────────────
#
# Skills referenced by the team_template (engineer's `coding`, QC's
# `code-review`) need to ship as package data — without them, a fresh
# install's agents would point at missing skills. The loader's
# resolution chain falls back to ``modulatio/_seed_skills/`` when the
# user's shared and project-local dirs don't have the file. User
# edits override; the seed is read-only canonical defaults.


def test_seed_skills_dir_ships_coding_and_code_review():
    """Sanity: the canonical skills referenced by the engineer + QC
    agent templates exist in the shipped package data."""
    from modulatio.skills import _SEED_SKILLS_ROOT

    assert (_SEED_SKILLS_ROOT / "coding.md").exists()
    assert (_SEED_SKILLS_ROOT / "code-review.md").exists()


def test_seed_planning_skills_carry_sweep_bounding_guidance():
    """Plan-time bounding (2026-05-30, reconciled with the task cap): the
    planning skills must tell the LLM to bound an enumerable 'X for each
    of N items' sweep into a FEW cap-compliant batched tasks (NOT one per
    item — that busts the per-sub-objective cap and is unneeded now that
    fetches are size-bounded), with a scout-first step for unknown items
    and a deferred phase when the set is wider than the cap."""
    from modulatio.skills import _SEED_SKILLS_ROOT

    task_plan = (_SEED_SKILLS_ROOT / "task-plan.md").read_text()
    assert "SWEEP" in task_plan
    assert "cap" in task_plan          # cap-aware, not one-per-item
    assert "BATCH" in task_plan
    assert "PHASE" in task_plan        # defer the rest when wider than cap
    assert "SCOUT" in task_plan        # unknown-items-first path

    leader_plan = (_SEED_SKILLS_ROOT / "leader-plan.md").read_text()
    assert "sweep" in leader_plan.lower()
    assert "batch" in leader_plan.lower()


def test_load_falls_back_to_seed_when_shared_empty(tmp_path, monkeypatch):
    """Empty shared dir + missing project-local → seed wins. The
    motivating use case: fresh install, user hasn't populated their
    Obsidian vault yet, but the team_template references `coding`.
    The engineer agent must still resolve the skill."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "empty-shared")
    sk = skills.load_with_metadata("coding")
    assert sk.name == "coding"
    assert sk.prompt_template, "expected non-empty body from seed"
    assert "run_shell" in sk.prompt_template


def test_coding_skill_carries_the_three_part_discipline_in_order(tmp_path, monkeypatch):
    """The code-production skill is the full three-part working
    discipline, in strict order: (1) the cognitive runbook — name the
    operation, commit the right bar, verify by observed reality; then
    (2) the reuse-first / smallest-change ladder; then (3) the craft
    (tests, don't-bloat, house idiom). A producer that only knows the
    ladder and the craft writes tidy code to the wrong bar; the runbook
    is what makes it the *right* work. The three must appear, and the
    runbook must lead — it's how you think before you reach for either
    of the others."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "empty-shared")
    body = skills.load_with_metadata("coding").prompt_template
    low = body.lower()

    # (1) cognitive runbook — the think-first layer
    assert "name the operation" in low
    assert "the bar" in low
    assert "observed reality" in low
    # (2) reuse-first ladder
    assert "reuse before you write" in low
    # (3) craft
    assert "tests are proof" in low

    # strict order: runbook leads, then the ladder, then the craft.
    runbook_at = low.index("name the operation")
    ladder_at = low.index("reuse before you write")
    craft_at = low.index("tests are proof")
    assert runbook_at < ladder_at < craft_at, (
        "the cognitive runbook must lead, before the reuse ladder and the craft"
    )


def test_load_user_shared_overrides_seed(tmp_path, monkeypatch):
    """Override semantics: when the user has authored their own
    `coding.md` in the shared dir, that wins over the seed. Lets the
    user customize the skill without forking the package."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "coding.md").write_text(
        "---\n"
        "name: coding\n"
        "description: user override\n"
        "---\n\n"
        "USER-OVERRIDDEN BODY\n"
    )
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    sk = skills.load_with_metadata("coding")
    assert sk.description == "user override"
    assert "USER-OVERRIDDEN BODY" in sk.prompt_template


def test_list_skills_includes_seed_entries(tmp_path, monkeypatch):
    """list_skills consumed by Leader's skill-gap detection — bundled
    seeds must show up as available alongside user shared + project
    skills."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "empty-shared")
    names = skills.list_skills()
    assert "coding" in names
    assert "code-review" in names


def test_seed_load_resolves_metadata_correctly(tmp_path, monkeypatch):
    """The seed file's frontmatter must round-trip through the loader.
    Specifically: ``coding`` must declare ``executor: llm`` +
    ``tool_loadout: run_shell`` so the orchestrator's tool-loadout
    detection finds it on the engineer agent."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "empty-shared")
    sk = skills.load_with_metadata("coding")
    assert sk.executor == "llm"
    assert "run_shell" in sk.tool_loadout


def test_rigorous_sourcing_skill_ships_for_producers():
    """The rigorous-sourcing producer skill (2026-05-30) ships as seed data:
    fetch real sources, cite with resolvable locators, never fabricate, flag
    what couldn't be verified. The positive complement to dropping verify goals."""
    from modulatio.skills import _SEED_SKILLS_ROOT
    sk = (_SEED_SKILLS_ROOT / "rigorous-sourcing.md")
    assert sk.exists()
    body = sk.read_text()
    assert "http_get" in body              # fetch real sources
    assert "References" in body            # cite with a resolvable locator
    assert "fabricat" in body.lower()      # never fabricate


def test_rigorous_sourcing_leads_with_the_cognitive_runbook():
    """The sourcing skill leads with the cognitive runbook (name the operation
    → commit the bar → verify by OBSERVED REALITY) so the producer grounds
    citations on the FIRST pass instead of fabricating-then-getting-rejected.
    Includes the date discipline (don't stamp training-cutoff dates) that fixes
    the impossible-access-date defect QC keeps flagging."""
    from modulatio.skills import _SEED_SKILLS_ROOT
    body = (_SEED_SKILLS_ROOT / "rigorous-sourcing.md").read_text().lower()
    assert "name the operation" in body          # runbook lead (mirrors coding.md)
    assert "observed reality" in body            # the spine: cite what you fetched
    assert "training cutoff" in body             # the date discipline


# === delete_skill (Feng-Tui SKILLS overhaul) ================================


def test_delete_skill_removes_a_shared_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", tmp_path / "no-seed")
    skills.create_skill(name="drafter", description="d", prompt_template="body")
    assert "drafter" in skills.list_skills()
    assert skills.delete_skill("drafter") is True
    assert "drafter" not in skills.list_skills()


def test_delete_skill_removes_a_project_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", tmp_path / "no-seed")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project("STA", "x", "obj")
    skills.create_skill(name="local", description="d", prompt_template="b",
                        project_code="STA")
    assert skills.delete_skill("local", project_code="STA") is True
    assert not (vault.project_dir("STA") / "skills" / "local.md").exists()


def test_delete_skill_unknown_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", tmp_path / "no-seed")
    assert skills.delete_skill("ghost") is False


def test_delete_skill_does_not_touch_seed_skills(tmp_path, monkeypatch):
    """A bundled seed skill has no writable copy → delete is a no-op (False),
    and the seed file survives."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "coding.md").write_text("---\nname: coding\n---\nseed body\n")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", seed)
    assert skills.delete_skill("coding") is False
    assert (seed / "coding.md").exists()


def test_delete_skill_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    import pytest
    with pytest.raises(Exception):
        skills.delete_skill("../escape")
