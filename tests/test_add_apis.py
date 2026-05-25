"""Tests for slice #18 — roster / skill programmatic APIs.

Thin wrappers around the existing file-based create paths so TUI wizards
(slices #24-#26) can construct agents, update agent models, and create
skills without duplicating the YAML format or reaching into internals.

Scope discipline: basic, no extra code. Three functions, each raises
on duplicate-target. No partial-update helpers beyond add_model. No
batch-create, no validation layer beyond what existing save() does.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import roster, skills, vault
from modulatio.roster import Agent
from modulatio.skills import Skill


PROJECT_CODE = "WIZ"


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    vault.init_project(PROJECT_CODE, "Wizard fixture", "draft 1")
    # Seed one shared skill so add_agent can derive caps.
    shared_root = tmp_path / "shared"
    shared_root.mkdir(parents=True, exist_ok=True)
    (shared_root / "drafter.md").write_text(
        "---\n"
        "name: drafter\n"
        "description: writes artifacts\n"
        "tool_loadout: fs\n"
        "standards_domain: \n"
        "model_tier: tactical\n"
        "cost_class: paid-cloud\n"
        "capability_tags: writing, structured-output\n"
        "required_capabilities: reasoning-heavy\n"
        "executor: llm\n"
        "---\n\n"
        "Stub prompt body.\n"
    )
    return PROJECT_CODE


# ─── roster.add_agent ───────────────────────────────────────────────────────

def test_add_agent_creates_file_and_returns_agent(project):
    """Happy path: wizard supplies id + name + identity + skills + model.
    Caps/tier/cost derived from skills (same policy as seed_default_roster)."""
    agent = roster.add_agent(
        project_code=project,
        agent_id="writer-01",
        name="Writer One",
        identity="Drafts general-purpose artifacts.",
        skills=["drafter"],
        model="ollama_chat/glm-5.1",
    )
    assert isinstance(agent, Agent)
    assert agent.id == "writer-01"
    assert agent.name == "Writer One"
    assert agent.identity == "Drafts general-purpose artifacts."
    assert agent.skills == ["drafter"]
    assert agent.model == "ollama_chat/glm-5.1"
    # File exists on disk.
    loaded = roster.load("writer-01", project)
    assert loaded is not None


def test_add_agent_round_trips_via_load(project):
    """Writing then loading produces an equivalent Agent — proves the
    format on-disk matches what load() expects."""
    created = roster.add_agent(
        project_code=project,
        agent_id="writer-02",
        name="Writer Two",
        identity="Specialist.",
        skills=["drafter"],
        model="stub",
    )
    loaded = roster.load("writer-02", project)
    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.skills == created.skills
    assert loaded.model == created.model


def test_add_agent_derives_capabilities_from_skills(project):
    """Caps + model_tier + cost_class computed from the skills dict the
    same way seed_default_roster does — reuses _derive_agent_caps."""
    agent = roster.add_agent(
        project_code=project,
        agent_id="writer-03",
        name="Writer Three",
        identity="",
        skills=["drafter"],
        model="stub",
    )
    # drafter.md advertises capability_tags=[writing,structured-output]
    # and required_capabilities=[reasoning-heavy]; the seeded agent
    # unions both so dispatch sees the agent covering every filter the
    # skill might demand.
    assert "writing" in agent.capability_tags
    assert "structured-output" in agent.capability_tags
    assert "reasoning-heavy" in agent.capability_tags
    assert agent.model_tier == "tactical"
    assert agent.cost_class == "paid-cloud"


def test_add_agent_raises_when_agent_id_exists(project):
    """Wizards opt into explicit failure on dup rather than silent
    overwrite — prevents human surprise + preserves customizations."""
    roster.add_agent(
        project_code=project,
        agent_id="writer-04",
        name="First",
        identity="",
        skills=["drafter"],
        model="stub",
    )
    with pytest.raises(FileExistsError):
        roster.add_agent(
            project_code=project,
            agent_id="writer-04",
            name="Second",
            identity="",
            skills=["drafter"],
            model="stub",
        )


# ─── roster.add_model ───────────────────────────────────────────────────────

def test_add_model_updates_existing_agent(project):
    """Happy path: upgrade an existing agent's model id. Other fields
    preserved. This is how the Models-tab wizard will work."""
    roster.add_agent(
        project_code=project,
        agent_id="writer-05",
        name="Writer Five",
        identity="",
        skills=["drafter"],
        model="stub",
    )
    updated = roster.add_model(
        project_code=project,
        agent_id="writer-05",
        model="ollama_chat/glm-5.1",
    )
    assert updated.model == "ollama_chat/glm-5.1"
    # Other fields preserved.
    assert updated.skills == ["drafter"]
    # Persisted: fresh load returns same model.
    loaded = roster.load("writer-05", project)
    assert loaded.model == "ollama_chat/glm-5.1"


def test_add_model_raises_when_agent_missing(project):
    """Update on a non-existent agent is a wizard-flow bug — raise."""
    with pytest.raises(FileNotFoundError):
        roster.add_model(
            project_code=project,
            agent_id="nonexistent",
            model="ollama_chat/glm-5.1",
        )


# ─── roster.clear_model (#21 — TUI Models tab remove button) ───────────────

def test_clear_model_sets_agent_model_to_none(project):
    """Clear an agent's model assignment without removing the agent.
    Backs the Models-tab Clear button."""
    roster.add_agent(
        project_code=project,
        agent_id="writer-clr",
        name="Writer Clear",
        identity="",
        skills=["drafter"],
        model="ollama_chat/glm-5.1",
    )
    cleared = roster.clear_model(project_code=project, agent_id="writer-clr")
    assert cleared.model is None
    # Other fields preserved.
    assert cleared.skills == ["drafter"]
    assert cleared.name == "Writer Clear"
    # Persisted.
    loaded = roster.load("writer-clr", project)
    assert loaded.model is None


def test_clear_model_raises_when_agent_missing(project):
    """Same wizard-flow precondition as add_model — caller validates."""
    with pytest.raises(FileNotFoundError):
        roster.clear_model(project_code=project, agent_id="nonexistent")


# ─── skills.create_skill ────────────────────────────────────────────────────

def test_create_skill_creates_shared_file(project, tmp_path):
    """No project_code → writes to the shared skills root. The TUI's
    Skills tab offers scope=shared for team-wide skills."""
    created = skills.create_skill(
        name="my-skill",
        description="Does a thing",
        prompt_template="The template body.",
        capability_tags=("research",),
        required_capabilities=("web-research",),
        model_tier="tactical",
        cost_class="paid-cloud",
    )
    assert isinstance(created, Skill)
    assert created.name == "my-skill"
    # File on shared path.
    shared_path = tmp_path / "shared" / "my-skill.md"
    assert shared_path.exists()


def test_create_skill_creates_project_local_file(project, tmp_path):
    """project_code scope → writes under that project's vault skills/,
    overriding any shared skill of the same name for that project."""
    created = skills.create_skill(
        name="project-only",
        description="Only for this project",
        prompt_template="body",
        project_code=project,
    )
    assert created.name == "project-only"
    project_path = tmp_path / "projects" / project.lower() / "skills" / "project-only.md"
    assert project_path.exists()


def test_create_skill_round_trips_via_load_with_metadata(project):
    """Create → load_with_metadata returns the same logical skill. Proves
    the wrapper uses the canonical save format."""
    skills.create_skill(
        name="round-trip",
        description="Roundtrip fixture",
        prompt_template="prompt body",
        tool_loadout=("fs", "web"),
        capability_tags=("research",),
        required_capabilities=("long-context",),
        model_tier="reasoning-heavy",
        cost_class="premium-cloud",
    )
    loaded = skills.load_with_metadata("round-trip")
    assert loaded.name == "round-trip"
    assert loaded.description == "Roundtrip fixture"
    assert loaded.tool_loadout == ("fs", "web")
    assert "research" in loaded.capability_tags
    assert "long-context" in loaded.required_capabilities
    assert loaded.model_tier == "reasoning-heavy"
    assert loaded.cost_class == "premium-cloud"


def test_create_skill_raises_when_name_exists(project):
    """Dup-name create is an error. Wizard validates before calling."""
    skills.create_skill(
        name="dup-check",
        description="First",
        prompt_template="first",
    )
    with pytest.raises(FileExistsError):
        skills.create_skill(
            name="dup-check",
            description="Second",
            prompt_template="second",
        )


def test_create_skill_raises_on_project_dup(project):
    """Dup check also applies within a single project scope."""
    skills.create_skill(
        name="project-dup",
        description="First",
        prompt_template="first",
        project_code=project,
    )
    with pytest.raises(FileExistsError):
        skills.create_skill(
            name="project-dup",
            description="Second",
            prompt_template="second",
            project_code=project,
        )
