"""Slice 3: setup wizard tests.

Covers the testable pieces (steps framework helpers, sanitize_env_pair,
manager-keyword block, finalize.commit, individual step modules' pure
functions). The interactive prompts themselves are exercised by smoke
tests rather than unit-level mocking.
"""

from __future__ import annotations

import json

import pytest

from modulatio import config, setup_state
from modulatio.setup_wizard import (
    agent_step,
    budget_step,
    embedded_llm_step,
    finalize,
    first_project_step,
    pandoc_step,
    steps,
    vault_path_step,
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg_dir / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg_dir / "auth_alerts.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.reload()
    yield


# === steps.sanitize_env_pair ===

def test_sanitize_env_pair_accepts_valid():
    assert steps.sanitize_env_pair("MY_API_KEY", "sk-abc123") == ("MY_API_KEY", "sk-abc123")


def test_sanitize_env_pair_rejects_invalid_name():
    assert steps.sanitize_env_pair("123KEY", "value") == (None, None)
    assert steps.sanitize_env_pair("MY-KEY", "value") == (None, None)
    assert steps.sanitize_env_pair("MY KEY", "value") == (None, None)


def test_sanitize_env_pair_rejects_newlines_in_value():
    assert steps.sanitize_env_pair("KEY", "val\ninjected=evil") == (None, None)
    assert steps.sanitize_env_pair("KEY", "val\rmore") == (None, None)
    assert steps.sanitize_env_pair("KEY", "val\x00null") == (None, None)


# === steps.contains_manager (CrewAI manager-keyword block) ===

def test_contains_manager_blocks_keyword():
    assert steps.contains_manager("project_manager") is True
    assert steps.contains_manager("Senior Manager") is True
    assert steps.contains_manager("MANAGER") is True


def test_contains_manager_allows_safe_alternatives():
    assert steps.contains_manager("coordinator") is False
    assert steps.contains_manager("lead") is False
    assert steps.contains_manager("director") is False
    assert steps.contains_manager("supervisor") is False


# === pandoc_step ===

def test_pandoc_install_panel_includes_all_os_rows(capsys):
    pandoc_step.render_install_panel()
    out = capsys.readouterr().out
    assert "Linux (apt)" in out
    assert "macOS (brew)" in out
    assert "Windows (choco)" in out
    assert "https://pandoc.org" in out


def test_pandoc_install_commands_data_shape():
    """Cross-OS panel data must include at minimum Linux, macOS, Windows."""
    keys = list(pandoc_step.INSTALL_COMMANDS)
    assert any("Linux" in k for k in keys)
    assert any("macOS" in k for k in keys)
    assert any("Windows" in k for k in keys)


# === vault_path_step ===

def test_suggested_paths_no_obsidian(monkeypatch, tmp_path):
    """When ~/Obsidian/ doesn't exist, fall back to neutral paths."""
    monkeypatch.setattr(vault_path_step, "Path", type("P", (), {"home": staticmethod(lambda: tmp_path)}))
    # Direct call to the inner detect to assert no-Obsidian path
    monkeypatch.setattr(vault_path_step, "detect_obsidian_root", lambda: None)
    vault, shared = vault_path_step.suggested_paths()
    assert "modulatio" in vault
    assert "modulatio" in shared
    assert "Obsidian" not in vault
    assert "Obsidian" not in shared


def test_suggested_paths_with_obsidian(monkeypatch, tmp_path):
    """When ~/Obsidian/ exists, suggest Obsidian-integrated paths."""
    obs = tmp_path / "Obsidian"
    obs.mkdir()
    monkeypatch.setattr(vault_path_step, "detect_obsidian_root", lambda: obs)
    vault, shared = vault_path_step.suggested_paths()
    assert "Obsidian" in vault
    assert "Modulatio" in vault
    assert "Obsidian" in shared


# === agent_step constants ===

def test_min_max_agent_caps():
    # Skills-first (#143): Leader + QC + at least one producer = 3.
    assert agent_step.MIN_AGENTS == 3
    assert agent_step.MAX_AGENTS == 10


def test_build_agent_from_template_round_trips():
    agent = agent_step._build_agent_from_template("writer", "ollama_chat/glm-5.1")
    assert agent["id"] == "writer"
    assert agent["name"] == "Writer"
    assert agent["tier"] == "producer"
    assert "drafter" in agent["skills"]
    assert agent["model"] == "ollama_chat/glm-5.1"
    assert agent["template_origin"] == "writer"


def test_build_agent_from_template_unknown_id_raises():
    with pytest.raises(ValueError, match="not found"):
        agent_step._build_agent_from_template("not-a-template", "stub")


# === embedded_llm_step ===

def test_is_cached_false_when_dir_empty(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cache))
    config.reload()
    assert embedded_llm_step.is_cached() is False


def test_is_cached_true_when_active_model_dir_populated(tmp_path, monkeypatch):
    """cache detection is now
    slug-aware. A populated subdir whose name contains the active
    model's leaf slug counts as cached. The prior "any subdir exists"
    heuristic was wrong — a cache from a previously-active embedder
    would falsely report the new active embedder as cached."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cache))
    config.reload()
    embeddings = embedded_llm_step.cache_dir()
    embeddings.mkdir(parents=True, exist_ok=True)
    # Default model id is the package fallback. fastembed/HF cache
    # layout uses "models--{org}--{name}" — emulate that shape.
    active = config.get_embedding_model()
    leaf = active.split("/")[-1]
    model_dir = embeddings / f"models--sentence-transformers--{leaf}"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.onnx").write_text("fake binary")
    assert embedded_llm_step.is_cached() is True


def test_is_cached_false_when_only_other_model_present(tmp_path, monkeypatch):
    """if the user previously cached a
    different embedder, a fresh active-model cache check must NOT
    report the active model as cached. The slug match prevents false
    positives that the old heuristic produced."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cache))
    config.reload()
    embeddings = embedded_llm_step.cache_dir()
    embeddings.mkdir(parents=True, exist_ok=True)
    # A different embedder cached on disk (not the active one).
    other = embeddings / "models--BAAI--bge-small-en-v1.5"
    other.mkdir(parents=True, exist_ok=True)
    (other / "model.onnx").write_text("fake binary")
    # Active model is the default `all-MiniLM-L6-v2` — slug `minilm`
    # does not appear in the bge subdir name.
    assert embedded_llm_step.is_cached() is False


def test_is_cached_respects_explicit_model_arg(tmp_path, monkeypatch):
    """Tests can probe specific model ids without monkeypatching config."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cache))
    config.reload()
    embeddings = embedded_llm_step.cache_dir()
    embeddings.mkdir(parents=True, exist_ok=True)
    (embeddings / "models--BAAI--bge-small-en-v1.5").mkdir(parents=True)
    (embeddings / "models--BAAI--bge-small-en-v1.5" / "x").write_text("y")
    assert embedded_llm_step.is_cached("BAAI/bge-small-en-v1.5") is True
    assert embedded_llm_step.is_cached("sentence-transformers/all-MiniLM-L6-v2") is False


def test_prefetch_uses_config_get_embedding_model(tmp_path, monkeypatch):
    """prefetch must resolve the model id via
    config.get_embedding_model() — single source of truth — so the
    wizard's `embedding_model` override is honored. The prior code
    tried to import a nonexistent `_ROUTING_MODEL` from semantic_router
    and fell back to a hardcoded `BAAI/bge-small-en-v1.5`, which would
    download a different model than the one routing actually used."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cache))
    # Override the embedding model so we can verify resolution.
    monkeypatch.setattr(
        config,
        "get_embedding_model",
        lambda: "test-org/test-embedder-v9",
    )
    config.reload()

    captured: dict = {}

    class _FakeTextEmbedding:
        def __init__(self, model_id, cuda=None, cache_dir=None):
            captured["model_id"] = model_id
            captured["cache_dir"] = cache_dir

    class _FakeDevice:
        CPU = "cpu"

    fake_fastembed = type(
        "_FakeFastembed", (), {"TextEmbedding": _FakeTextEmbedding}
    )()
    fake_types = type("_FakeTypes", (), {"Device": _FakeDevice})()

    import sys
    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.common", type("X", (), {})())
    monkeypatch.setitem(sys.modules, "fastembed.common.types", fake_types)

    ok = embedded_llm_step.prefetch()
    assert ok is True
    assert captured["model_id"] == "test-org/test-embedder-v9"


def test_prefetch_explicit_model_arg_overrides_config(tmp_path, monkeypatch):
    """The `model_id` arg is for advanced/testing use; if passed, it
    takes precedence over the config default."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cache))
    monkeypatch.setattr(
        config, "get_embedding_model", lambda: "config-default/model-x"
    )
    config.reload()

    captured: dict = {}

    class _FakeTextEmbedding:
        def __init__(self, model_id, cuda=None, cache_dir=None):
            captured["model_id"] = model_id

    class _FakeDevice:
        CPU = "cpu"

    fake_fastembed = type(
        "_FakeFastembed", (), {"TextEmbedding": _FakeTextEmbedding}
    )()
    fake_types = type("_FakeTypes", (), {"Device": _FakeDevice})()

    import sys
    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.common.types", fake_types)

    ok = embedded_llm_step.prefetch("explicit-arg/model-y")
    assert ok is True
    assert captured["model_id"] == "explicit-arg/model-y"


# === finalize.commit ===

def test_commit_writes_defaults_and_marks_setup_complete(tmp_path):
    """default_models is derived from triad+worker model picks at finalize,
    not asked separately. Triad agents carry tier+model; first worker
    drives specialist (and researcher when no researcher template picked)."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "configured_providers": ["Anthropic (CLI OAuth)"],
        "staged_api_keys": {},
        "triad_agents": [
            {"id": "leader", "tier": "leader", "model": "claude-cli/claude-sonnet-4-6", "template_origin": "leader"},
            {"id": "qc", "tier": "qc", "model": "ollama_chat/kimi-k2.5", "template_origin": "qc"},
        ],
        "worker_agents": [
            {"id": "writer", "tier": "producer", "model": "ollama_chat/glm-5.1", "template_origin": "writer"},
        ],
    }
    finalize.commit(state, version="2.0.0")

    # defaults.json written; default_models derived from the team
    assert config.DEFAULTS_FILE.exists()
    on_disk = json.loads(config.DEFAULTS_FILE.read_text())
    assert on_disk["vault_root"] == state["vault_root"]
    assert on_disk["default_models"]["leader"] == "claude-cli/claude-sonnet-4-6"
    assert on_disk["default_models"]["qc"] == "ollama_chat/kimi-k2.5"
    # Skills-first (#143): planner uses the Leader's model (no coordinator).
    assert on_disk["default_models"]["planner"] == "claude-cli/claude-sonnet-4-6"
    assert "coordinator" not in on_disk["default_models"]
    assert on_disk["default_models"]["producer"] == "ollama_chat/glm-5.1"
    # No researcher template picked → researcher falls back to first worker
    assert on_disk["default_models"]["researcher"] == "ollama_chat/glm-5.1"

    # team_template.json written with all 3 agents (Leader + QC + 1 producer)
    assert config.TEAM_TEMPLATE_FILE.exists()
    template = json.loads(config.TEAM_TEMPLATE_FILE.read_text())
    assert len(template) == 3
    assert template[0]["id"] == "leader"
    assert template[2]["id"] == "writer"

    # setup_state marked completed
    assert setup_state.setup_completed()
    s = setup_state.load()
    assert s["wizard_version"] == "2.0.0"

    # Shared resources tree initialized
    shared = tmp_path / "shared"
    for sub in ("templates", "skills", "standards", "research"):
        assert (shared / sub).exists()


def test_commit_derives_researcher_from_researcher_template(tmp_path):
    """When a worker has template_origin == 'researcher', that worker's
    model wins for the researcher role even if it's not the first worker."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "configured_providers": [],
        "staged_api_keys": {},
        "triad_agents": [
            {"id": "leader", "tier": "leader", "model": "model-a"},
            {"id": "qc", "tier": "qc", "model": "model-b"},
        ],
        "worker_agents": [
            {"id": "writer", "tier": "producer", "model": "model-c", "template_origin": "writer"},
            {"id": "researcher", "tier": "producer", "model": "model-d", "template_origin": "researcher"},
        ],
    }
    finalize.commit(state, version="2.0.0")
    on_disk = json.loads(config.DEFAULTS_FILE.read_text())
    assert on_disk["default_models"]["producer"] == "model-c"  # first worker
    assert on_disk["default_models"]["researcher"] == "model-d"  # researcher template wins
    assert on_disk["default_models"]["planner"] == "model-a"  # planner = leader's model


def test_derive_default_models_handles_missing_structural_tiers():
    """If a structural agent lacks tier or model, derive should silently
    skip that role rather than crash. Defensive against partial state."""
    derived = finalize._derive_default_models(
        structural=[{"template_origin": "leader"}],  # no tier, no model
        workers=[],
    )
    assert derived == {}


def test_commit_writes_no_team_template_when_no_agents(tmp_path):
    """Empty triad+workers state (e.g. user quit early or unit test) skips
    team_template.json write — fallback hardcoded roster still applies."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "configured_providers": [],
        "staged_api_keys": {},
        "triad_agents": [],
        "worker_agents": [],
    }
    finalize.commit(state, version="2.0.0")
    assert not config.TEAM_TEMPLATE_FILE.exists()


def test_commit_writes_env_keys_chmod_600(tmp_path):
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "configured_providers": [],
        "staged_api_keys": {"OPENAI_API_KEY": "sk-test"},
        "triad_agents": [],
        "worker_agents": [],
    }
    finalize.commit(state, version="2.0.0")

    env_path = tmp_path / "vault" / ".env"
    assert env_path.exists()
    assert "OPENAI_API_KEY=sk-test" in env_path.read_text()
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_commit_preserves_existing_env_keys(tmp_path):
    """Pre-existing keys in <vault>/.env survive when finalize merges in new ones."""
    vault = tmp_path / "vault"
    vault.mkdir()
    env_path = vault / ".env"
    env_path.write_text("EXISTING_KEY=already-here\n")

    state = {
        "vault_root": str(vault),
        "shared_resources_path": str(tmp_path / "shared"),
        "configured_providers": [],
        "staged_api_keys": {"NEW_KEY": "new-value"},
        "triad_agents": [],
        "worker_agents": [],
    }
    finalize.commit(state, version="2.0.0")

    content = env_path.read_text()
    assert "EXISTING_KEY=already-here" in content
    assert "NEW_KEY=new-value" in content


# === Wizard pre-population on re-invocation ===

def test_load_existing_state_populates_from_defaults():
    config.save_defaults({
        "vault_root": "/tmp/my-vault",
        "shared_resources_path": "/tmp/my-shared",
        "default_models": {"leader": "anthropic/claude-opus-4-7"},
    })
    config.reload()

    from modulatio import setup_wizard
    state = setup_wizard._load_existing_state()
    assert state["vault_root"] == "/tmp/my-vault"
    assert state["shared_resources_path"] == "/tmp/my-shared"
    assert state["default_models"]["leader"] == "anthropic/claude-opus-4-7"


def test_load_existing_state_empty_when_no_defaults():
    from modulatio import setup_wizard
    state = setup_wizard._load_existing_state()
    assert state == {}


# === first_project_step ===

def test_validate_code_accepts_typical():
    assert first_project_step._validate_code("ckb") is None
    assert first_project_step._validate_code("my_book") is None
    assert first_project_step._validate_code("q3_marketing_2026") is None
    assert first_project_step._validate_code("a") is None


def test_validate_code_rejects_uppercase():
    err = first_project_step._validate_code("MyBook")
    assert err is not None
    assert "lowercase" in err


def test_validate_code_rejects_leading_digit():
    err = first_project_step._validate_code("1book")
    assert err is not None


def test_validate_code_rejects_hyphens_and_spaces():
    assert first_project_step._validate_code("my-book") is not None
    assert first_project_step._validate_code("my book") is not None


def test_validate_code_rejects_empty_or_too_long():
    assert first_project_step._validate_code("") is not None
    assert first_project_step._validate_code("a" * 33) is not None


def test_first_project_step_captures_code_and_objective(monkeypatch):
    """Stub stdin so we walk the prompt without a real TTY."""
    answers = iter(["my_proj", "Ship the thing."])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = first_project_step.run(state)
    assert result == "captured"
    assert state["first_project_code"] == "my_proj"
    assert state["first_project_objective"] == "Ship the thing."


# === config team_template I/O ===

def test_team_template_round_trips(tmp_path):
    agents = [
        {"id": "leader", "tier": "leader", "model": "x", "skills": ["leader"]},
        {"id": "writer", "tier": "producer", "model": "y", "skills": ["drafter"]},
    ]
    config.save_team_template(agents)
    loaded = config.load_team_template()
    assert loaded == agents


def test_team_template_load_returns_none_when_absent(tmp_path):
    assert config.load_team_template() is None


def test_team_template_load_returns_none_on_malformed_json(tmp_path):
    config.TEAM_TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.TEAM_TEMPLATE_FILE.write_text("{not valid json")
    assert config.load_team_template() is None


def test_team_template_load_returns_none_when_not_a_list(tmp_path):
    """Defensive: if someone hand-edits the file into a dict, refuse it."""
    config.TEAM_TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.TEAM_TEMPLATE_FILE.write_text('{"agents": []}')
    assert config.load_team_template() is None


def test_team_template_file_is_chmod_600(tmp_path):
    """Same security hardening as defaults.json — paths/identities here
    can be considered private to the user."""
    config.save_team_template([{"id": "x"}])
    mode = config.TEAM_TEMPLATE_FILE.stat().st_mode & 0o777
    assert mode == 0o600


# === budget_step ===


def test_budget_step_parse_optional_float_blank_returns_none():
    v, err = budget_step._parse_optional_float("")
    assert v is None
    assert err is None
    v, err = budget_step._parse_optional_float("   ")
    assert v is None
    assert err is None


def test_budget_step_parse_optional_float_rejects_non_numeric():
    v, err = budget_step._parse_optional_float("forever")
    assert v is None
    assert err is not None
    assert "number" in err.lower()


def test_budget_step_parse_optional_float_rejects_zero_or_negative():
    """Caps must trip at SOME point — 0 or negative would never halt."""
    v, err = budget_step._parse_optional_float("0")
    assert v is None
    assert err is not None
    v, err = budget_step._parse_optional_float("-5")
    assert v is None
    assert err is not None


def test_budget_step_parse_optional_int_accepts_whole_number():
    v, err = budget_step._parse_optional_int("50000")
    assert v == 50000
    assert err is None


def test_budget_step_parse_optional_int_rejects_float():
    """Token cap is integer-only. 3.14 isn't a valid token count."""
    v, err = budget_step._parse_optional_int("3.14")
    assert v is None
    assert err is not None


def test_budget_step_run_skips_when_user_says_no(monkeypatch):
    """Y/N gate defaults to N. Pressing Enter skips the entire step
    without setting any caps. State stays clean."""
    answers = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = budget_step.run(state)
    assert result == "skipped"
    assert "budget_caps" not in state


def test_budget_step_run_clears_existing_caps_on_skip(monkeypatch):
    """If a re-invocation pre-filled budget_caps from defaults.json
    and the user now says no, state's prior caps are dropped so
    finalize doesn't write stale values."""
    answers = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {"budget_caps": {"max_tokens": 99999}}
    result = budget_step.run(state)
    assert result == "skipped"
    assert "budget_caps" not in state


def test_budget_step_run_captures_all_three_axes(monkeypatch):
    """Y on the gate, then a value for each of the three prompts.
    State ends with a budget_caps dict carrying all three values."""
    answers = iter(["y", "30", "50000", "5.00"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps["max_wall_clock_min"] == 30.0
    assert caps["max_tokens"] == 50000
    assert caps["max_cost_usd"] == 5.0


def test_budget_step_run_blank_values_remain_none(monkeypatch):
    """Y on the gate but blank entries on the three prompts → state
    ends with all-None caps. Distinct from the skip case: the user
    explicitly opened the section then chose unbounded across the
    board (rare but legal — preserves intent)."""
    answers = iter(["y", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps == {
        "max_wall_clock_min": None,
        "max_tokens": None,
        "max_cost_usd": None,
    }


def test_budget_step_run_rejects_invalid_then_accepts(monkeypatch):
    """Validation loop: invalid input keeps re-prompting until the
    user gives valid input. Smoke test for each axis's validator."""
    answers = iter(["y", "forever", "30", "abc", "50000", "neg-1", "5.0"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps["max_wall_clock_min"] == 30.0
    assert caps["max_tokens"] == 50000
    assert caps["max_cost_usd"] == 5.0


# === finalize budget_caps integration ===


def test_finalize_writes_budget_caps_when_set(tmp_path, monkeypatch):
    """When state carries budget_caps, finalize.commit propagates it
    to defaults.json under the ``budget_caps`` key. Subsequent
    plans.persist reads via config.get_default_budget_caps."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "triad_agents": [
            {"id": "leader", "tier": "leader", "model": "x"},
            {"id": "coordinator", "tier": "coordinator", "model": "y"},
            {"id": "qc", "tier": "qc", "model": "z"},
        ],
        "worker_agents": [
            {"id": "writer", "tier": "producer", "model": "w"},
        ],
        "budget_caps": {
            "max_wall_clock_min": 60.0,
            "max_tokens": 100000,
            "max_cost_usd": 10.0,
        },
    }
    finalize.commit(state, version="2.0.0")

    raw = json.loads(config.DEFAULTS_FILE.read_text())
    assert raw["budget_caps"]["max_wall_clock_min"] == 60.0
    assert raw["budget_caps"]["max_tokens"] == 100000
    assert raw["budget_caps"]["max_cost_usd"] == 10.0


def test_finalize_omits_budget_caps_when_none_set(tmp_path):
    """No budget_caps in state → defaults.json has no budget_caps
    key. Older configs from before this slice see no schema churn."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "triad_agents": [
            {"id": "leader", "tier": "leader", "model": "x"},
            {"id": "coordinator", "tier": "coordinator", "model": "y"},
            {"id": "qc", "tier": "qc", "model": "z"},
        ],
        "worker_agents": [],
    }
    finalize.commit(state, version="2.0.0")
    raw = json.loads(config.DEFAULTS_FILE.read_text())
    assert "budget_caps" not in raw


def test_finalize_drops_none_axes_from_budget_caps(tmp_path):
    """Only set axes get persisted. A user who only set max_tokens
    yields a budget_caps block with one key, not three."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "triad_agents": [
            {"id": "leader", "tier": "leader", "model": "x"},
            {"id": "coordinator", "tier": "coordinator", "model": "y"},
            {"id": "qc", "tier": "qc", "model": "z"},
        ],
        "worker_agents": [],
        "budget_caps": {
            "max_wall_clock_min": None,
            "max_tokens": 50000,
            "max_cost_usd": None,
        },
    }
    finalize.commit(state, version="2.0.0")
    raw = json.loads(config.DEFAULTS_FILE.read_text())
    assert raw["budget_caps"] == {"max_tokens": 50000}
