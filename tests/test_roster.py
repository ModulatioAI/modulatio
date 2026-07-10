"""Tests for the Agent roster (slice #6a foundation).

Agents are per-project compositions: an agent holds a set of skills,
runs on a specific model, and carries routing metadata (model_tier,
cost_class, capability_tags, capacity_cap) that slice #6c will consume
for dispatch.

Persisted at ``<project_vault>/agents/<agent_id>.md`` — no shared layer
in #6a (teams compose their own rosters; template presets for the five
common roles are a slice #6b-or-later addition).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import config, roster, vault
from modulatio.roster import Agent

PROJECT_CODE = "TST"


@pytest.fixture(autouse=True)
def isolate_config(tmp_path: Path, monkeypatch) -> None:
    """Keep config writes (defaults.json, team_template.json) out of the
    real ``~/.config/modulatio/``. Without this, a wizard-written
    team_template.json on the developer's box hijacks ``seed_default_roster``
    away from its hardcoded fallback and breaks the default-roster tests."""
    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    config.reload()


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "test")
    return tmp_path / PROJECT_CODE.lower()


# ── save / load round-trip ──────────────────────────────────────────────────

def test_agent_save_round_trip_preserves_all_routing_fields(project_vault):
    original = roster.Agent(
        id="tuned-specialist",
        name="Tuned Specialist",
        identity=(
            "Producer tuned for a specific house voice. "
            "Strong on argument-by-example and historical framing."
        ),
        skills=["drafter", "contrarian-argument", "house-style"],
        model="ollama_chat/glm-5.1",
        model_tier="reasoning-heavy",
        cost_class="paid-cloud",
        capability_tags=["writing", "contrarian", "house-style"],
        capacity_cap=2,
        template_origin="drafter",
        freshness_class="semi-stable",
        last_verified_at="2026-04-20",
    )
    roster.save(original, project_code=PROJECT_CODE)

    loaded = roster.load("tuned-specialist", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.id == original.id
    assert loaded.name == original.name
    assert loaded.identity.strip() == original.identity.strip()
    assert loaded.skills == original.skills
    assert loaded.model == original.model
    assert loaded.model_tier == original.model_tier
    assert loaded.cost_class == original.cost_class
    assert loaded.capability_tags == original.capability_tags
    assert loaded.capacity_cap == original.capacity_cap
    assert loaded.template_origin == original.template_origin
    assert loaded.freshness_class == original.freshness_class
    assert loaded.last_verified_at == original.last_verified_at


def test_agent_save_writes_under_project_agents_dir(project_vault):
    roster.save(
        roster.Agent(
            id="drafter",
            name="Default Drafter",
            identity="Generic drafter.",
            skills=["drafter"],
            model=None,
            model_tier="generalist",
            cost_class="paid-cloud",
        ),
        project_code=PROJECT_CODE,
    )
    path = project_vault / "agents" / "drafter.md"
    assert path.exists()


# ── load missing ────────────────────────────────────────────────────────────

def test_load_missing_agent_returns_none(project_vault):
    assert roster.load("no-such-agent", project_code=PROJECT_CODE) is None


# ── list ────────────────────────────────────────────────────────────────────

def test_list_agents_returns_sorted_by_id(project_vault):
    for aid in ("qc", "drafter", "researcher", "coordinator", "leader"):
        roster.save(
            roster.Agent(
                id=aid,
                name=aid.capitalize(),
                identity=f"{aid} identity",
                skills=[aid],
                model=None,
                model_tier="generalist",
                cost_class="paid-cloud",
            ),
            project_code=PROJECT_CODE,
        )
    agents = roster.list_agents(project_code=PROJECT_CODE)
    assert [a.id for a in agents] == ["coordinator", "drafter", "leader", "qc", "researcher"]


def test_list_agents_empty_when_project_has_no_roster(project_vault):
    assert roster.list_agents(project_code=PROJECT_CODE) == []


# ── model_for_tier — the SINGLE source of a seat's model ─────────────────────

def test_model_for_tier_returns_the_single_seat_model(project_vault):
    """The roster is the SOLE source of a seat's model. Both the conversational
    lane and the team/orchestration lane resolve here, so a leader can only ever
    carry one model — no split-brain (leader-converse ≠ leader-decompose)."""
    roster.save(
        roster.Agent(id="leader", name="Leader", tier="leader",
                     model="codex_gpt_5_5"),
        project_code=PROJECT_CODE,
    )
    roster.save(
        roster.Agent(id="qc", name="QC", tier="qc", model="nvidia/nemotron"),
        project_code=PROJECT_CODE,
    )
    assert roster.model_for_tier(PROJECT_CODE, "leader") == "codex_gpt_5_5"
    assert roster.model_for_tier(PROJECT_CODE, "qc") == "nvidia/nemotron"
    # No producer seeded → no model for that tier.
    assert roster.model_for_tier(PROJECT_CODE, "producer") is None


def test_model_for_tier_none_when_seat_has_no_model(project_vault):
    roster.save(
        roster.Agent(id="leader", name="Leader", tier="leader", model=None),
        project_code=PROJECT_CODE,
    )
    assert roster.model_for_tier(PROJECT_CODE, "leader") is None


# ── routing-surface helpers ────────────────────────────────────────────────

def test_agent_has_skill_checks_skill_membership():
    """Convenience helper used by slice #6c dispatch — quickly tests
    whether an agent can cover a required skill."""
    agent = roster.Agent(
        id="drafter",
        name="Drafter",
        identity="x",
        skills=["drafter", "markdown"],
        model=None,
        model_tier="generalist",
        cost_class="paid-cloud",
    )
    assert agent.has_skill("drafter") is True
    assert agent.has_skill("markdown") is True
    assert agent.has_skill("wp-cli-admin") is False


# ── Agent.tier (slice #6f-F) ───────────────────────────────────────────────

def test_agent_default_tier_is_producer():
    """New rosters default to producer tier — the most common case. Explicit
    'leader'/'coordinator'/'qc' declarations are opt-in. Back-compat for
    existing agents predating the field."""
    agent = roster.Agent(
        id="x",
        name="x",
        identity="",
        skills=["drafter"],
    )
    assert agent.tier == "producer"


def test_agent_tier_round_trip_through_save_load(project_vault):
    """Tier persists through save/load so routing decisions stay stable
    across orchestrator runs."""
    roster.save(
        roster.Agent(
            id="qc-kimi",
            name="QC Kimi",
            identity="verifier.",
            skills=["qc"],
            tier="qc",
            model="ollama_chat/kimi-k2.5:latest",
            model_tier="reasoning-heavy",
            cost_class="premium-cloud",
        ),
        project_code=PROJECT_CODE,
    )
    loaded = roster.load("qc-kimi", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.tier == "qc"


def test_agent_disable_thinking_override_round_trips(project_vault):
    """The per-agent reasoning override persists: ``false`` (a reasoning-heavy
    producer that should deliberate) round-trips, and an unset agent stays None
    (inherit the thinking-OFF default) with no frontmatter line written."""
    roster.save(
        roster.Agent(id="deep", name="Deep", model="m", disable_thinking=False),
        project_code=PROJECT_CODE,
    )
    assert roster.load("deep", project_code=PROJECT_CODE).disable_thinking is False

    path = roster.save(
        roster.Agent(id="plain", name="Plain", model="m"),
        project_code=PROJECT_CODE,
    )
    assert "disable_thinking" not in path.read_text()
    assert roster.load("plain", project_code=PROJECT_CODE).disable_thinking is None


def test_agent_covers_required_skills_all_or_nothing():
    """The dispatch predicate: agent covers the task when it holds every
    skill the task requires. Missing any single required skill → not a
    candidate (Leader opens a capability ticket in slice #6d)."""
    agent = roster.Agent(
        id="drafter",
        name="Drafter",
        identity="x",
        skills=["drafter", "markdown"],
        model=None,
        model_tier="generalist",
        cost_class="paid-cloud",
    )
    assert agent.covers(["drafter"]) is True
    assert agent.covers(["drafter", "markdown"]) is True
    assert agent.covers(["drafter", "wp-cli-admin"]) is False
    assert agent.covers([]) is True  # vacuously


# ── capability tag predicate (slice #9a) ───────────────────────────────────

def test_agent_covers_capabilities_all_or_nothing():
    """The capability-filter analogue of ``covers``. Agent covers the
    requirement iff every required capability tag is in the agent's
    declared ``capability_tags``. Missing any → not a candidate.

    Capabilities are the fine-grained attribute axis (reasoning-heavy,
    structured-output, shell-access) that #9a uses as a hard dispatch
    filter on top of skill cover. Skills name WHAT the agent does;
    capabilities name HOW it does it.
    """
    agent = roster.Agent(
        id="a",
        name="A",
        identity="x",
        skills=["producer"],
        capability_tags=["reasoning-heavy", "long-context"],
    )
    assert agent.covers_capabilities(["reasoning-heavy"]) is True
    assert agent.covers_capabilities(["reasoning-heavy", "long-context"]) is True
    assert agent.covers_capabilities(["reasoning-heavy", "shell-access"]) is False
    assert agent.covers_capabilities([]) is True  # vacuous — no constraint


# ── default roster seed (slice #11c) ───────────────────────────────────────

_DEFAULT_ROSTER_IDS = {"leader", "producer", "qc"}


def test_seed_default_roster_writes_three_agents_with_expected_shape(project_vault):
    """Skills-first (#143): a net-new project seeds three agents — Leader +
    QC structural roles plus a producer skill-holder. No coordinator agent
    and no researcher agent (research is a capability the producer composes;
    Brick A). Each agent's model binds to its CLI model flag; template-origin
    marks it as seeded."""
    written = roster.seed_default_roster(
        PROJECT_CODE,
        leader_model="ld/m",
        coordinator_model="cd/m",  # accepted for back-compat; no longer seeds an agent
        producer_model="sp/m",
        qc_model="qc/m",
        researcher_model="rs/m",
    )
    assert {a.id for a in written} == _DEFAULT_ROSTER_IDS
    by_id = {a.id: a for a in written}

    assert "coordinator" not in by_id  # role removed for skills-first

    assert by_id["leader"].model == "ld/m"
    assert by_id["leader"].tier == "leader"
    assert sorted(by_id["leader"].skills) == [
        "leader", "leader-plan", "leader-plan-approve",
        "leader-reflect", "leader-verify",
    ]

    assert by_id["producer"].model == "sp/m"
    assert by_id["producer"].tier == "producer"

    assert by_id["qc"].model == "qc/m"
    assert by_id["qc"].tier == "qc"

    for agent in written:
        assert agent.template_origin == "default-roster"

    # Persisted to disk — list_agents round-trips.
    loaded = {a.id: a for a in roster.list_agents(PROJECT_CODE)}
    assert set(loaded) == _DEFAULT_ROSTER_IDS


def test_seed_default_roster_capability_union_covers_skill_requirements(
    project_vault, monkeypatch,
):
    """Capability tags are the union of every held skill's
    ``capability_tags`` AND ``required_capabilities``. That's what makes
    the seeded roster dispatchable by construction — any task the
    Coordinator emits citing a capability tag a held skill advertises
    resolves to the seeded agent, no `ROSTER_GAP` ticket.

    Uses the shared skill definitions at the v1-era location until the
    setup wizard (slice 3) migrates them into the configured shared
    resources path. Reads the current shared set rather than asserting
    a frozen tag list, so shared skill updates don't drift this test
    out of alignment silently.
    """
    import pytest
    from pathlib import Path
    from modulatio import skills as skills_mod

    # Slice 1 (config foundations): the seeded shared skills live at the
    # v1-era Obsidian path until the setup wizard migrates them. Locate
    # them and monkeypatch _SKILLS_ROOT, or skip if not found (CI envs
    # without the dev vault populated).
    legacy_skills = Path.home() / "Obsidian" / "Claude" / "projects" / "modulatio" / "skills"
    if not (legacy_skills / "leader.md").exists():
        pytest.skip(
            "Shared skill fixtures not found. Run setup wizard (slice 3) or "
            f"populate {legacy_skills}."
        )
    monkeypatch.setattr(skills_mod, "_SKILLS_ROOT", legacy_skills)

    written = roster.seed_default_roster(
        PROJECT_CODE,
        leader_model="ld/m",
        coordinator_model="cd/m",
        producer_model="sp/m",
        qc_model="qc/m",
        researcher_model="rs/m",
    )
    by_id = {a.id: a for a in written}

    # For every seeded agent, every held skill's advertised +
    # required capabilities must appear in agent.capability_tags.
    for agent in written:
        expected: set[str] = set()
        for sname in agent.skills:
            skill = skills_mod.load_with_metadata(sname)
            expected.update(skill.capability_tags)
            expected.update(skill.required_capabilities)
        missing = expected - set(agent.capability_tags)
        assert not missing, (
            f"Agent '{agent.id}' missing capability tags {missing} from its "
            f"held skills {agent.skills}"
        )

    # Concrete sample: leader holds leader + leader-verify, so it must
    # carry tags from both skills unioned — not a subset of just one.
    leader = by_id["leader"]
    assert "goal-decomposition" in leader.capability_tags  # from leader skill
    assert "goal-verification" in leader.capability_tags  # from leader-verify skill


def test_seed_default_roster_idempotent_skips_existing_agent_files(project_vault):
    """If an agent file already exists for a seeded id, the seed leaves
    it alone — a human pre-seeding their own ``qc.md`` with a tuned
    identity must not get overwritten by a subsequent kickoff. The
    normal call site already gates on net-new projects; this defensive
    check covers pre-existing files within the agents directory."""
    # Pre-seed a custom qc agent.
    custom_qc = roster.Agent(
        id="qc",
        name="Custom QC",
        identity="House-tuned QC with project-specific rigor.",
        skills=["qc"],
        model="custom/model",
        tier="qc",
    )
    roster.save(custom_qc, project_code=PROJECT_CODE)

    roster.seed_default_roster(
        PROJECT_CODE,
        leader_model="ld/m",
        coordinator_model="cd/m",
        producer_model="sp/m",
        qc_model="qc/m",
        researcher_model="rs/m",
    )

    reloaded = roster.load("qc", project_code=PROJECT_CODE)
    assert reloaded is not None
    assert reloaded.name == "Custom QC"
    assert reloaded.identity.startswith("House-tuned QC")
    assert reloaded.model == "custom/model"
    # Other four agents still seeded.
    all_ids = {a.id for a in roster.list_agents(PROJECT_CODE)}
    assert all_ids == _DEFAULT_ROSTER_IDS


def test_seed_default_roster_dispatches_to_a_producer(
    project_vault,
):
    """Integration-level check that the seeded roster is reachable via
    `dispatch.select_agent`. Since the skill-library arc, skills don't gate:
    any skill-carrying task routes to a PRODUCER-tier agent (which checks the
    skill out of the library at run-time). Leader/QC are structural roles with
    their own selection paths, never producer-dispatch targets."""
    from modulatio import dispatch
    from modulatio.types import Task, TaskStatus
    from uuid import uuid4

    written = roster.seed_default_roster(
        PROJECT_CODE,
        leader_model="ld/m",
        coordinator_model="cd/m",
        producer_model="sp/m",
        qc_model="qc/m",
        researcher_model="rs/m",
    )
    producer_ids = {a.id for a in written if a.tier == "producer"}
    assert producer_ids, "the seeded roster must include at least one producer"

    # Every skill-carrying task — even one naming a skill no agent 'holds' —
    # routes to a producer (never None, never leader/qc).
    for required_skills in (["drafter"], ["researcher"], ["nothing-holds-this"]):
        task = Task(
            id=f"{PROJECT_CODE}-0",
            project_id=uuid4(),
            goal_id=f"{PROJECT_CODE}-G-001",
            description="x",
            status=TaskStatus.DISPATCHED,
            required_skills=required_skills,
        )
        picked = dispatch.select_agent(task, written)
        assert picked is not None, (
            f"No producer routed for required_skills={required_skills}"
        )
        assert picked.id in producer_ids, (
            f"Expected a producer for {required_skills}, got {picked.id}"
        )


# ── team_template seeding (Bug 5 / wizard-defined roster) ──────────────────

def test_seed_default_roster_uses_team_template_when_present(project_vault):
    """When ~/.config/modulatio/team_template.json exists (post-wizard),
    seed_default_roster reads from it and ignores per-role model kwargs.
    Each agent in the template becomes a real Agent under the project."""
    config.save_team_template([
        {
            "id": "leader",
            "name": "Leader",
            "tier": "leader",
            "model": "team/leader-m",
            "skills": ["leader"],
            "capability_tags": ["strategic"],
            "identity": "Custom leader identity",
            "template_origin": "leader",
        },
        {
            "id": "writer_a",
            "name": "Writer A",
            "tier": "producer",
            "model": "team/writer-m",
            "skills": ["drafter"],
            "capability_tags": ["writing"],
            "identity": "House voice writer",
            "template_origin": "writer",
        },
    ])
    config.reload()

    # Per-role kwargs should be IGNORED when template is present
    written = roster.seed_default_roster(
        PROJECT_CODE,
        leader_model="ignored",
        coordinator_model="ignored",
        producer_model="ignored",
        qc_model="ignored",
        researcher_model="ignored",
    )
    by_id = {a.id: a for a in written}
    assert set(by_id) == {"leader", "writer_a"}
    assert by_id["leader"].model == "team/leader-m"
    assert by_id["leader"].identity == "Custom leader identity"
    assert by_id["leader"].template_origin == "leader"
    assert by_id["writer_a"].model == "team/writer-m"
    assert by_id["writer_a"].tier == "producer"


def test_seed_default_roster_falls_back_to_hardcoded_when_no_template(project_vault):
    """No team_template.json (pre-wizard or template deleted) → hardcoded
    5-agent template seeded with per-role model kwargs. Preserves boot
    behavior for users who haven't run the wizard."""
    assert config.load_team_template() is None
    written = roster.seed_default_roster(
        PROJECT_CODE,
        leader_model="ld/m",
        coordinator_model="cd/m",
        producer_model="sp/m",
        qc_model="qc/m",
        researcher_model="rs/m",
    )
    assert {a.id for a in written} == {"leader", "producer", "qc"}
    by_id = {a.id: a for a in written}
    assert by_id["leader"].model == "ld/m"
    assert by_id["leader"].template_origin == "default-roster"


def test_seed_from_team_template_idempotent(project_vault):
    """Re-seeding with the same template (or any template) must NOT
    overwrite a project's existing agent files. Same defensive contract
    as the hardcoded path."""
    config.save_team_template([
        {"id": "leader", "name": "Leader", "tier": "leader", "model": "v1", "skills": ["leader"]},
    ])
    config.reload()
    roster.seed_default_roster(
        PROJECT_CODE,
        leader_model=None, coordinator_model=None, producer_model=None,
        qc_model=None, researcher_model=None,
    )
    leader_path = project_vault / "agents" / "leader.md"
    first_mtime = leader_path.stat().st_mtime

    # Update the template — second seed should skip (file exists)
    config.save_team_template([
        {"id": "leader", "name": "Leader", "tier": "leader", "model": "v2", "skills": ["leader"]},
    ])
    config.reload()
    roster.seed_default_roster(
        PROJECT_CODE,
        leader_model=None, coordinator_model=None, producer_model=None,
        qc_model=None, researcher_model=None,
    )
    # Mtime unchanged → file was not rewritten; original v1 model preserved
    assert leader_path.stat().st_mtime == first_mtime
    loaded = roster.load("leader", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.model == "v1"


def test_seed_from_team_template_skips_entries_without_id(project_vault):
    """Defensive: a malformed template entry without an ``id`` is silently
    skipped rather than crashing the whole seed."""
    config.save_team_template([
        {"id": "leader", "tier": "leader", "model": "x", "skills": []},
        {"name": "anonymous"},  # no id — skip
        {"id": "writer", "tier": "producer", "model": "y", "skills": []},
    ])
    config.reload()
    written = roster.seed_default_roster(
        PROJECT_CODE,
        leader_model=None, coordinator_model=None, producer_model=None,
        qc_model=None, researcher_model=None,
    )
    assert {a.id for a in written} == {"leader", "writer"}


# === create_project (folder + team in one, for the PROJECTS tab) ===


def test_create_project_makes_folder_and_seeds_roster(tmp_path, monkeypatch):
    """create_project bundles folder creation + team seeding so a new project
    is immediately listed (marker-complete) and team-ready."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    config.save_defaults({"default_models": {"leader": "stub", "producer": "stub", "qc": "stub"}})
    config.reload()

    root = roster.create_project("freshproj", "write a thing")

    assert root == vault.project_dir("freshproj")
    assert "freshproj" in vault.list_projects()      # seed markers present
    assert roster.list_agents("freshproj")           # install team seeded
    assert "write a thing" in (root / "index.md").read_text()


def test_create_project_rejects_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    roster.create_project("dup")
    with pytest.raises(FileExistsError):
        roster.create_project("dup")


def test_create_project_rejects_invalid_code(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    with pytest.raises(ValueError):
        roster.create_project("../etc")


def test_create_project_rolls_back_on_seed_failure(tmp_path, monkeypatch):
    """If seeding fails after the folder is made, the half-made project is
    rolled back — never stranded in list_projects with no team (which would
    block a retry with FileExistsError)."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)

    def boom(*a, **k):
        raise OSError("seed failed")

    monkeypatch.setattr(roster, "seed_default_roster", boom)
    with pytest.raises(OSError):
        roster.create_project("halfmade")
    assert "halfmade" not in vault.list_projects()
    assert not vault.project_dir("halfmade").exists()
    # and a retry ACTUALLY succeeds now (no stranded folder blocking it)
    monkeypatch.setattr(roster, "seed_default_roster", lambda *a, **k: [])
    roster.create_project("halfmade")
    assert "halfmade" in vault.list_projects()


def test_create_project_rejects_traversal_agent_id_in_template(tmp_path, monkeypatch):
    """A team_template agent id with path traversal must NOT write a file
    outside the project's agents/ dir during create seeding — the create path
    turns config into a write primitive otherwise."""
    from pydantic import ValidationError

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    config.save_team_template([
        {"id": "../../../outside_probe", "name": "outside", "identity": "x"},
    ])
    config.reload()
    with pytest.raises((ValueError, ValidationError)):
        roster.create_project("probe", "objective")
    # nothing written outside the project anywhere under tmp_path
    assert list(tmp_path.glob("**/outside_probe.md")) == []


def test_create_project_exist_ok_reuses_without_rollback(tmp_path, monkeypatch):
    """exist_ok=True reuses an existing folder (idempotent seed). If seeding
    then fails, a PRE-EXISTING project must NOT be rolled back — only a
    net-new folder this call created is."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    # pre-existing project with real content
    root = vault.init_project("keep", "Keep", "x")
    (root / "index.md").write_text("PRECIOUS", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("seed failed")

    monkeypatch.setattr(roster, "seed_default_roster", boom)
    with pytest.raises(OSError):
        roster.create_project("keep", exist_ok=True)
    # the pre-existing folder + its content survive (no rollback)
    assert root.exists()
    assert "PRECIOUS" in (root / "index.md").read_text()


def test_save_rejects_model_copy_id_bypass(tmp_path, monkeypatch):
    """model_copy bypasses the Agent.id field validator; save() must still
    refuse a traversal id at the filesystem-write boundary, no matter how the
    Agent instance was produced."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("cp", "Cp", "x")
    agent = roster.Agent(id="safe", name="safe")
    escaped = agent.model_copy(update={"id": "../../../copy_escape"})
    with pytest.raises(ValueError):
        roster.save(escaped, "cp")
    assert list(tmp_path.glob("**/copy_escape.md")) == []


def test_remove_agent_rejects_traversal_id(tmp_path, monkeypatch):
    """remove_agent unlinks <project>/agents/<id>.md; a traversal id must be
    refused — it must never delete a file outside the project."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("rmprobe", "Rm", "x")
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        roster.remove_agent(project_code="rmprobe", agent_id="../../../victim")
    assert victim.exists()  # untouched


def test_remove_agent_blocks_planted_frontmatter_traversal(tmp_path, monkeypatch):
    """A planted agents/*.md whose frontmatter id is a traversal string is
    parsed by list_agents; feeding that id back to remove_agent must NOT delete
    outside the project."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    root = vault.init_project("planted", "P", "x")
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    (root / "agents" / "display.md").write_text(
        "---\nid: ../../../victim\nname: planted\ntier: producer\n---\n\nbody\n",
        encoding="utf-8",
    )
    bad_id = roster.list_agents("planted")[0].id
    assert bad_id == "../../../victim"
    with pytest.raises(ValueError):
        roster.remove_agent(project_code="planted", agent_id=bad_id)
    assert victim.exists()


# ═══ fold: test_roster_resweep.py ═══
# 0.9.0 pre-ship re-sweep regressions for ``modulatio.roster``.
#
# Dedicated file (own-the-file contract): does not touch ``test_roster.py``
# or ``test_roster_r2_audit.py``.


def test_capacity_cap_floored_on_direct_construction() -> None:
    """Baseline: the field_validator floors ctor/parse paths (pre-existing)."""
    assert Agent(id="a", name="a", capacity_cap=0).capacity_cap == 1
    assert Agent(id="a", name="a", capacity_cap=-5).capacity_cap == 1
    assert Agent(id="a", name="a", capacity_cap=3).capacity_cap == 3


def test_capacity_cap_floored_on_model_copy_update_zero() -> None:
    """re-sweep finding 1: a sub-1 cap must not enter the roster via the
    ``model_copy(update=...)`` path. Pydantic v2 field_validators do NOT
    fire on model_copy, so before the override this returned 0 (a silent
    non-dispatchable producer)."""
    base = Agent(id="a", name="a", capacity_cap=4)
    copied = base.model_copy(update={"capacity_cap": 0})
    assert copied.capacity_cap == 1


def test_capacity_cap_floored_on_model_copy_update_negative() -> None:
    """re-sweep finding 1: negative caps via copy are floored too."""
    base = Agent(id="a", name="a", capacity_cap=4)
    assert base.model_copy(update={"capacity_cap": -7}).capacity_cap == 1


def test_model_copy_preserves_valid_capacity_cap() -> None:
    """The override must not clobber legitimate caps — only floor sub-1."""
    base = Agent(id="a", name="a", capacity_cap=8)
    assert base.model_copy().capacity_cap == 8
    assert base.model_copy(update={"capacity_cap": 5}).capacity_cap == 5


def test_model_copy_update_unrelated_field_keeps_cap() -> None:
    """The common real call sites (``add_model``/``clear_model``) copy with an
    update that does not touch capacity_cap; the cap must round-trip."""
    base = Agent(id="a", name="a", capacity_cap=2, model="m1")
    assert base.model_copy(update={"model": "m2"}).capacity_cap == 2


# ═══ fold: roster r2_audit ═══
# Round fixtures (isolate_config/project_vault/PROJECT_CODE) were copies.


@pytest.mark.parametrize("bad", [0, -1, -10])
def test_agent_capacity_cap_floored_to_one_on_direct_construction(bad):
    agent = roster.Agent(id="p", name="P", capacity_cap=bad)
    assert agent.capacity_cap == 1


@pytest.mark.parametrize("good", [1, 2, 8])
def test_agent_capacity_cap_legitimate_values_preserved(good):
    agent = roster.Agent(id="p", name="P", capacity_cap=good)
    assert agent.capacity_cap == good


def test_agent_capacity_cap_zero_in_frontmatter_floored_on_load(project_vault):
    """A hand-written / corrupt roster file declaring capacity_cap: 0 must
    not produce a non-dispatchable producer — the loader floors it to 1."""
    agents_dir = project_vault / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "stuck.md").write_text(
        "---\n"
        "id: stuck\n"
        "name: Stuck\n"
        "tier: producer\n"
        "capacity_cap: 0\n"
        "---\n\n"
        "A producer that would otherwise stall every wave it qualifies for.\n"
    )

    loaded = roster.load("stuck", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.capacity_cap == 1


def test_agent_capacity_cap_default_is_one(project_vault):
    """Omitting capacity_cap entirely stays at the dispatchable default."""
    agents_dir = project_vault / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "plain.md").write_text(
        "---\nid: plain\nname: Plain\ntier: producer\n---\n\nbody\n"
    )
    loaded = roster.load("plain", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.capacity_cap == 1
