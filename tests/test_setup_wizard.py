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
    budget_step,
    embedded_llm_step,
    finalize,
    first_project_step,
    pandoc_step,
    steps,
    vault_path_step,
)
from unittest import mock
from modulatio import setup_wizard
from modulatio.setup_wizard import (
    clipboard_step,
    renderer_step,
    webos_step,
)
import sys
import tempfile
from pathlib import Path


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


def test_pandoc_skip_warns_user_handles_conversion_manually(monkeypatch, capsys):
    """Skipping pandoc stays allowed, but the skip message must make clear
    the user takes on document-format conversion (DOCX/PDF) manually —
    deliverables stay Markdown until pandoc is installed."""
    monkeypatch.setattr(pandoc_step, "is_installed", lambda: False)
    answers = iter(["s"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = pandoc_step.run(state)
    assert result == "skipped"
    assert state["pandoc_skipped"] is True
    out = capsys.readouterr().out.lower()
    # "yourself" is the discriminator — it appears only in the new skip
    # message, proving the warning lands there (not in incidental earlier
    # prints that already say "install manually" / "DOCX/PDF/Markdown").
    assert "yourself" in out
    assert "markdown" in out


# === vault_path_step ===

def test_suggested_paths_no_obsidian(monkeypatch, tmp_path):
    """When ~/Obsidian/ doesn't exist, fall back to neutral Documents paths.

    The neutral default must live under ~/Documents/Modulatio so it can
    never collide with a git checkout at ~/modulatio (the dev repo).
    """
    monkeypatch.setattr(vault_path_step, "Path", type("P", (), {"home": staticmethod(lambda: tmp_path)}))
    # Direct call to the inner detect to assert no-Obsidian path
    monkeypatch.setattr(vault_path_step, "detect_obsidian_root", lambda: None)
    vault, shared = vault_path_step.suggested_paths()
    home = str(tmp_path)
    assert vault == f"{home}/Documents/Modulatio/projects"
    assert shared == f"{home}/Documents/Modulatio/shared"
    # Must NOT be a bare ~/modulatio path (would land inside a repo clone).
    assert vault != f"{home}/modulatio/projects"
    assert shared != f"{home}/modulatio/shared"
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


# === embedded_llm_step ===

def test_embedded_llm_run_is_required_no_skip_offer(monkeypatch):
    """The routing embedder is REQUIRED (skill-routing + qc-history don't
    work without it), so run() must attempt the fetch when the cache is
    cold and never offer the user a skip prompt."""
    monkeypatch.setattr(
        embedded_llm_step.config, "get_embedding_model", lambda: "some-org/embed-model"
    )
    monkeypatch.setattr(embedded_llm_step, "is_cached", lambda *_a, **_k: False)
    calls = {"prefetch": 0}

    def fake_prefetch(model_id=None):
        calls["prefetch"] += 1
        return True

    monkeypatch.setattr(embedded_llm_step, "prefetch", fake_prefetch)

    def boom(*_a, **_k):
        raise AssertionError("embedded LLM step must not offer a skip prompt")

    monkeypatch.setattr(steps, "confirm_yn", boom)
    state: dict = {}
    result = embedded_llm_step.run(state)
    assert result == "prefetched"
    assert calls["prefetch"] == 1
    assert state["embedded_llm_cached"] is True


def test_is_cached_false_when_dir_empty(tmp_path, monkeypatch):
    # cache_dir() resolves to fastembed's OWN default root (the dir the
    # runtime consumer reads); pin it via FASTEMBED_CACHE_PATH so the
    # check inspects a sandboxed location.
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
    config.reload()
    assert embedded_llm_step.is_cached() is False


def test_is_cached_true_when_active_model_dir_populated(tmp_path, monkeypatch):
    """cache detection is now
    slug-aware. A populated subdir whose name contains the active
    model's leaf slug counts as cached. The prior "any subdir exists"
    heuristic was wrong — a cache from a previously-active embedder
    would falsely report the new active embedder as cached."""
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
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
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
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
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
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
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
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
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
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
    # Research is a capability the producer composes — no researcher default
    # is written (Brick A).
    assert "researcher" not in on_disk["default_models"]

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


def test_commit_folds_researcher_template_worker_into_producer_pool(tmp_path):
    """A worker from the 'researcher' template is just a producer now — its
    model does NOT get a separate 'researcher' default; research is a
    capability the producer composes (Brick A)."""
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
    assert "researcher" not in on_disk["default_models"]
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


def test_commit_writes_env_keys_to_the_one_store_chmod_600(tmp_path):
    """Keys entered during setup land in the store every surface reads.
    Writing them beside the user's work instead would put fresh keys somewhere
    the rest of the engine no longer looks, and somewhere a settings wipe
    cannot reach."""
    state = {
        "vault_root": str(tmp_path / "vault"),
        "shared_resources_path": str(tmp_path / "shared"),
        "configured_providers": [],
        "staged_api_keys": {"OPENAI_API_KEY": "sk-test"},
        "triad_agents": [],
        "worker_agents": [],
    }
    finalize.commit(state, version="2.0.0")

    env_path = config.secrets_path()
    assert env_path.exists()
    assert "OPENAI_API_KEY=sk-test" in env_path.read_text()
    assert env_path.stat().st_mode & 0o777 == 0o600
    # Never beside the user's work.
    assert not (tmp_path / "vault" / ".env").exists()


def test_commit_preserves_existing_env_keys(tmp_path):
    """Keys already in the store survive when setup adds new ones."""
    vault = tmp_path / "vault"
    vault.mkdir()
    config.set_env_secret("EXISTING_KEY", "already-here")
    env_path = config.secrets_path()

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


def test_load_existing_state_prepopulates_team_from_template():
    """Re-invocation must pre-fill the agents step (triad + workers) from the
    saved team template so its edit/keep semantics are live, not dead."""
    config.save_team_template([
        {"id": "leader", "tier": "leader", "model": "anthropic/opus", "template_origin": "leader"},
        {"id": "qc", "tier": "qc", "model": "anthropic/sonnet", "template_origin": "qc"},
        {"id": "producer_1", "tier": "producer", "model": "openrouter/fast"},
        {"id": "producer_2", "tier": "producer", "model": "openrouter/cheap"},
    ])

    from modulatio import setup_wizard
    state = setup_wizard._load_existing_state()

    assert [a["id"] for a in state["triad_agents"]] == ["leader", "qc"]
    assert [a["id"] for a in state["worker_agents"]] == ["producer_1", "producer_2"]


def test_load_existing_state_no_team_keys_when_template_absent():
    """No template on disk → no triad/worker keys (step provisions fresh)."""
    from modulatio import setup_wizard
    state = setup_wizard._load_existing_state()
    assert "triad_agents" not in state
    assert "worker_agents" not in state


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


# ═══ fold: test_setup_wizard___init___low_audit.py ═══
# LOW-audit regression tests for modulatio.setup_wizard.__init__.
#
# Finding #88 [error-path]: the wizard's abort message claimed "No changes
# written," but the models step persists model_presets.json to disk
# immediately (add_preset / remove_preset write through), before finalize.
# So an abort in a later step still leaves presets on disk — the blanket
# claim is false. The fix makes the abort message conditional on whether the
# on-disk presets actually changed during the run.


def _abort(*_args, **_kwargs):
    raise steps.WizardAborted()


def test_abort_reports_persisted_presets_when_disk_changed():
    """When the models step wrote new presets to disk, the abort message
    must NOT claim 'No changes written' — it must acknowledge the saved
    models survive."""
    # load_presets() is called: once at wizard start (snapshot), once at
    # abort. Simulate a model added in between.
    snapshots = [
        {},  # start: nothing configured
        {"my-model": {"label": "x"}},  # abort: a preset was persisted mid-run
    ]
    muted_calls: list[str] = []

    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=list(snapshots),
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()

    assert result is False
    assert len(muted_calls) == 1
    msg = muted_calls[0]
    assert "No changes written" not in msg
    assert "saved" in msg.lower() or "remain" in msg.lower()


def test_abort_claims_no_changes_when_disk_unchanged():
    """When nothing was persisted (presets identical start vs abort), the
    honest 'No changes written' message is preserved."""
    unchanged = {"pre-existing": {"label": "y"}}
    muted_calls: list[str] = []

    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=[dict(unchanged), dict(unchanged)],
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()

    assert result is False
    assert muted_calls == ["Setup aborted. No changes written."]


def test_presets_snapshot_swallows_errors():
    """The snapshot helper must never raise — a malformed/missing presets
    file is treated as empty so the abort path can't crash."""
    with mock.patch(
        "modulatio.model_presets.load_presets",
        side_effect=ValueError("corrupt json"),
    ):
        assert setup_wizard._presets_snapshot() == {}


# ═══ fold: test_setup_wizard___init___resweep.py ═══
# Re-sweep regression tests for modulatio.setup_wizard.__init__.
#
# Finding #348 [MEDIUM/correctness]: the confirm step asks "Save and complete
# setup? [Y/n]" but ``finalize.commit()`` is only called after the WHOLE step
# machine completes. The original ``step_order`` ran ``embedded_llm`` (a
# potentially multi-minute model prefetch/download) AFTER ``confirm``, so the
# user could answer Y and then sit through a long download while NOTHING had
# been written yet — defaults.json / .env / team_template all still in memory.
# An interruption in that window discarded a confirmed save.
#
# The fix reorders ``step_order`` so ``embedded_llm`` runs BEFORE ``confirm``.
# The prefetch is a pure, reusable cache warm with no dependency on commit, so
# it is safe to run ahead of confirm; once the user confirms, ``commit()`` runs
# immediately with nothing slow in between.
#
# These tests drive the REAL ``steps.run_step_machine`` with stubbed step
# functions that record dispatch order, plus a stubbed ``finalize.commit`` that
# records when it fires, and assert:
#
#   1. ``embedded_llm`` is dispatched strictly before ``confirm``.
#   2. ``finalize.commit`` runs only after ``confirm`` returns — and with no
#      slow prefetch step still pending after it.


def _run_body_recording():
    """Run the real ``_run_setup_body`` with every step + commit stubbed to
    record call order. Returns the ordered list of recorded event names.

    The wizard's own ``_dispatch`` routes each step name to the matching
    module's ``run`` (or ``finalize.confirm`` for confirm), so stubbing the
    module functions exercises the actual ``step_order`` + state machine.
    """
    events: list[str] = []

    def _rec(name, ret="ok"):
        def _fn(_state):
            events.append(name)
            return ret
        return _fn

    def _commit(_state, *, version):
        events.append("commit")

    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard, "_auto_launch_tui"),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
        mock.patch.object(setup_wizard.theme, "clear_screen"),
        mock.patch.object(setup_wizard.theme, "step_header"),
        mock.patch("builtins.print"),
        mock.patch.object(pandoc_step, "run", _rec("pandoc")),
        mock.patch.object(clipboard_step, "run", _rec("clipboard")),
        mock.patch.object(renderer_step, "run", _rec("renderer")),
        mock.patch.object(webos_step, "run", _rec("webos")),
        mock.patch.object(vault_path_step, "run", _rec("vault_path")),
        mock.patch.object(budget_step, "run", _rec("budget")),
        mock.patch.object(first_project_step, "run", _rec("first_project")),
        mock.patch.object(embedded_llm_step, "run", _rec("embedded_llm")),
        # confirm must return True for the machine to advance past it.
        mock.patch.object(finalize, "confirm", _rec("confirm", ret=True)),
        mock.patch.object(finalize, "commit", _commit),
    ):
        result = setup_wizard.run_setup()
    return result, events


def test_embedded_llm_prefetch_runs_before_confirm():
    """The (slow) embedded-LLM prefetch must be dispatched BEFORE the confirm
    prompt, so confirm is the last thing before the immediate commit."""
    result, events = _run_body_recording()
    assert result is True
    assert "embedded_llm" in events
    assert "confirm" in events
    assert events.index("embedded_llm") < events.index("confirm"), (
        f"embedded_llm must precede confirm; got order {events}"
    )


def test_commit_fires_immediately_after_confirm_with_no_slow_step_pending():
    """Once the user confirms, commit must be the very next event — no
    embedded_llm (or any other step) sitting between a confirmed save and the
    write to disk."""
    result, events = _run_body_recording()
    assert result is True
    confirm_i = events.index("confirm")
    commit_i = events.index("commit")
    assert commit_i == confirm_i + 1, (
        f"commit must immediately follow confirm; got order {events}"
    )
    # And nothing slow (embedded_llm) lingers after the confirmed save.
    assert "embedded_llm" not in events[confirm_i:]


def test_step_order_constant_places_embedded_llm_before_confirm():
    """Guard the literal ``step_order`` list inside ``_run_setup_body`` by
    exercising it: the recorded dispatch order must have embedded_llm second
    to last and confirm last."""
    _result, events = _run_body_recording()
    # commit is appended by the body after the machine; the last MACHINE step
    # is confirm, immediately preceded by embedded_llm.
    machine_events = [e for e in events if e != "commit"]
    assert machine_events[-1] == "confirm"
    assert machine_events[-2] == "embedded_llm"


# ═══ fold: test_setup_wizard___init___r2_audit.py ═══
# R2-audit regression tests for modulatio.setup_wizard.__init__.
#
# Finding (LOW/error-path): the wizard's abort message claimed "No changes
# written" even after the pandoc / clipboard steps ran ``try_auto_install``
# (or the user installed manually during the recheck loop), which mutates the
# system via its package manager *before* any configuration is written. The
# existing presets-snapshot fix (finding #88) only covered model_presets.json,
# not a system-package side effect.
#
# The fix snapshots pandoc / clipboard installed-ness at wizard start and again
# at abort; a tool that became available during the run is reported honestly so
# the abort message no longer claims nothing changed after mutating the system.




def _run_with(pandoc_seq, clipboard_seq, presets_seq):
    """Drive run_setup() to an abort with mocked probes, returning muted msgs.

    *_seq args are 2-element lists: [value-at-start, value-at-abort].
    """
    muted_calls: list[str] = []
    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch.object(pandoc_step, "is_installed", side_effect=list(pandoc_seq)),
        mock.patch.object(clipboard_step, "is_installed", side_effect=list(clipboard_seq)),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=list(presets_seq),
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()
    return result, muted_calls


def test_abort_reports_pandoc_system_install_not_no_changes():
    """pandoc absent at start, present at abort (auto-installed) → the abort
    message must NOT lie with 'No changes written'."""
    result, muted = _run_with(
        pandoc_seq=[False, True],   # installed during the run
        clipboard_seq=[True, True],  # already present, unchanged
        presets_seq=[{}, {}],        # no preset change
    )
    assert result is False
    assert len(muted) == 1
    msg = muted[0]
    assert "No changes written" not in msg
    assert "pandoc" in msg
    assert "installed" in msg.lower()


def test_abort_reports_clipboard_system_install():
    """clipboard backend installed during the run is surfaced honestly."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[False, True],
        presets_seq=[{}, {}],
    )
    assert result is False
    msg = muted[0]
    assert "No changes written" not in msg
    assert "clipboard" in msg.lower()


def test_abort_reports_both_presets_and_system_install():
    """When BOTH presets persisted AND a system tool was installed, the abort
    message acknowledges both — not just one."""
    result, muted = _run_with(
        pandoc_seq=[False, True],
        clipboard_seq=[True, True],
        presets_seq=[{}, {"m": {"label": "x"}}],
    )
    assert result is False
    msg = muted[0]
    assert "No changes written" not in msg
    assert "pandoc" in msg
    assert "model" in msg.lower() or "saved" in msg.lower()


def test_abort_no_system_change_keeps_honest_no_changes():
    """Nothing installed, no presets change → the honest 'No changes written'
    message is preserved (no spurious system-install claim)."""
    result, muted = _run_with(
        pandoc_seq=[True, True],     # stable: already present
        clipboard_seq=[False, False],  # stable: absent both times
        presets_seq=[{}, {}],
    )
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_already_present_tool_not_reported_as_installed():
    """A tool already present at start (and still present) must NOT be claimed
    as 'installed' by the wizard — guards against conflating present vs newly
    installed."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[True, True],
        presets_seq=[{}, {}],
    )
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_abort_after_preset_removal_does_not_claim_saved_and_available():
    """Pre-ship MEDIUM: remove_preset() writes through immediately, so deleting
    a preexisting preset then aborting flips presets_changed True. The abort
    message must NOT claim the (now-deleted) models 'were saved and remain
    available' — it must acknowledge the removal honestly."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[True, True],
        # a preset existed at start, gone at abort -> a removal happened
        presets_seq=[{"old": {"label": "x"}}, {}],
    )
    assert result is False
    assert len(muted) == 1
    msg = muted[0]
    assert "No changes written" not in msg
    assert "remain available" not in msg
    assert "removal" in msg.lower() or "written to disk" in msg.lower()


def test_abort_after_preset_replacement_with_removal_is_honest():
    """A delta that both adds and drops a key still counts as a removal (a
    preexisting preset went away), so the additive 'remain available' wording
    must not be used."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[True, True],
        presets_seq=[{"old": {"label": "x"}}, {"new": {"label": "y"}}],
    )
    assert result is False
    msg = muted[0]
    assert "remain available" not in msg
    assert "removal" in msg.lower() or "written to disk" in msg.lower()


def test_abort_purely_additive_presets_keeps_saved_and_available_wording():
    """Regression guard: a purely additive preset delta (no preexisting key
    dropped) keeps the original 'saved and remain available' wording."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[True, True],
        presets_seq=[{}, {"m": {"label": "x"}}],
    )
    assert result is False
    msg = muted[0]
    assert "saved and remain available" in msg
    assert "removal" not in msg.lower()


def test_abort_removal_plus_system_install_reports_both_honestly():
    """When a removal AND a system install both happened, the abort message
    acknowledges the removal (not 'remain available') alongside the install."""
    result, muted = _run_with(
        pandoc_seq=[False, True],
        clipboard_seq=[True, True],
        presets_seq=[{"old": {"label": "x"}}, {}],
    )
    assert result is False
    msg = muted[0]
    assert "pandoc" in msg
    assert "remain available" not in msg
    assert "removal" in msg.lower() or "written to disk" in msg.lower()


def test_system_tools_snapshot_swallows_probe_errors():
    """A probe that raises is treated as 'absent' so the abort path can't
    crash on a flaky is_installed()."""
    with (
        mock.patch.object(pandoc_step, "is_installed", side_effect=RuntimeError("boom")),
        mock.patch.object(clipboard_step, "is_installed", return_value=True),
        mock.patch.object(renderer_step, "is_installed", return_value=False),
        mock.patch.object(webos_step, "is_installed", return_value=False),
    ):
        snap = setup_wizard._system_tools_snapshot()
    assert snap == {
        "pandoc": False, "clipboard": True, "renderer": False, "webos": False,
    }


# ═══ fold: test_setup_wizard_finalize_low_audit.py ═══
# LOW-audit regression tests for setup_wizard.finalize.
#
# Finding #89: ``finalize.confirm`` printed a Providers line from a state key
# (``configured_providers``) the wizard never sets, so it always rendered
# ``(none)``. Providers are now derived from ``staged_api_keys`` (env-var keyed).


def test_derive_providers_from_staged_keys():
    state = {
        "staged_api_keys": {
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "sk-y",
            "XAI_API_KEY": "sk-z",
        }
    }
    assert finalize._derive_providers(state) == ["anthropic", "openai", "xai"]


def test_derive_providers_dedup_and_sorted():
    # Two differently-cased entries for the same provider collapse to one.
    state = {"staged_api_keys": {"openai_api_key": "a", "OPENAI_API_KEY": "b"}}
    assert finalize._derive_providers(state) == ["openai"]


def test_derive_providers_empty_when_nothing_staged():
    assert finalize._derive_providers({}) == []
    assert finalize._derive_providers({"staged_api_keys": {}}) == []


def test_derive_providers_non_standard_env_var():
    # A custom key without the _API_KEY suffix still yields a lowercased name
    # rather than being dropped.
    state = {"staged_api_keys": {"GROQ_TOKEN": "v"}}
    assert finalize._derive_providers(state) == ["groq_token"]


def test_confirm_renders_providers_not_none(monkeypatch, capsys):
    # The bug: with staged keys present the summary still showed "(none)".
    # Drive confirm() to the print path, auto-confirming the prompt.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    state = {
        "vault_root": "/tmp/v",
        "shared_resources_path": "/tmp/s",
        "staged_api_keys": {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"},
        "configured_models": ["m1"],
        "triad_agents": [],
        "worker_agents": [],
    }
    result = finalize.confirm(state)
    assert result is True
    out = capsys.readouterr().out
    assert "Providers:" in out
    # Both staged providers appear; the line is no longer "(none)".
    assert "openai" in out
    assert "anthropic" in out
    # Locate the Providers line specifically and confirm it isn't empty.
    providers_line = next(ln for ln in out.splitlines() if "Providers:" in ln)
    assert "(none)" not in providers_line


# --- r2 audit: providers derived from model presets (OAuth/local-only) ---


def test_provider_from_base_url_variants():
    f = finalize._provider_from_base_url
    assert f("https://api.openai.com/v1") == "openai"
    assert f("https://api.anthropic.com") == "anthropic"
    assert f("https://api.x.ai/v1") == "x"
    assert f("https://openrouter.ai/api/v1") == "openrouter"
    assert f("http://127.0.0.1:11434/v1") == "local"
    assert f("http://localhost:1234/v1") == "local"
    assert f("not a url") is None
    assert f("") is None


def test_derive_providers_includes_oauth_only_preset(monkeypatch):
    # OAuth-only setup stages NO api keys; the old code rendered (none).
    # Now the endpoint behind each configured model represents the provider.
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "claude-oauth": {
                "base_url": "https://api.anthropic.com",
                "auth_type": "oauth_openai",
            },
        },
    )
    state = {"staged_api_keys": {}, "configured_models": ["claude-oauth"]}
    assert finalize._derive_providers(state) == ["anthropic"]


def test_derive_providers_includes_local_only_preset(monkeypatch):
    # Local-only setup: auth_type none, no api key, loopback endpoint.
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "ollama-llama": {
                "base_url": "http://127.0.0.1:11434/v1",
                "auth_type": "none",
            },
        },
    )
    state = {"staged_api_keys": {}, "configured_models": ["ollama-llama"]}
    assert finalize._derive_providers(state) == ["local"]


def test_derive_providers_unions_keys_and_presets(monkeypatch):
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "ollama-llama": {"base_url": "http://127.0.0.1:11434/v1"},
            "claude-oauth": {"base_url": "https://api.anthropic.com"},
        },
    )
    state = {
        "staged_api_keys": {"OPENAI_API_KEY": "sk-x"},
        "configured_models": ["ollama-llama", "claude-oauth"],
    }
    # openai (from key) + anthropic + local (from presets), sorted/deduped.
    assert finalize._derive_providers(state) == ["anthropic", "local", "openai"]


def test_derive_providers_ignores_unconfigured_presets(monkeypatch):
    # A preset on disk that the user did NOT pick (not in configured_models)
    # is not represented in the summary.
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "picked": {"base_url": "https://api.anthropic.com"},
            "unpicked": {"base_url": "https://api.openai.com"},
        },
    )
    state = {"staged_api_keys": {}, "configured_models": ["picked"]}
    assert finalize._derive_providers(state) == ["anthropic"]


def test_derive_providers_survives_presets_load_failure(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr("modulatio.model_presets.load_presets", boom)
    state = {"staged_api_keys": {"OPENAI_API_KEY": "sk-x"}}
    # Degrades to staged-keys-only rather than crashing the summary.
    assert finalize._derive_providers(state) == ["openai"]


# --- r2 audit: Producers line no longer renders '?, ?, ?' ---


def test_producer_label_prefers_model():
    assert finalize._producer_label({"model": "gpt-4o-mini", "skills": []}) == "gpt-4o-mini"


def test_producer_label_falls_back_to_caps_then_name():
    assert finalize._producer_label(
        {"model": "", "capability_tags": ["fast", "cheap"]}
    ) == "fast, cheap"
    assert finalize._producer_label({"model": "", "name": "Scout"}) == "Scout"
    assert finalize._producer_label({}) == "?"


def test_confirm_producers_line_not_all_question_marks(monkeypatch, capsys):
    # The bug: 'Skill-holders' rendered '?, ?, ?' because Agent.skills is
    # always empty (skills are checked out from the library per task).
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    state = {
        "vault_root": "/tmp/v",
        "shared_resources_path": "/tmp/s",
        "staged_api_keys": {},
        "configured_models": [],
        "triad_agents": [],
        "worker_agents": [
            {"name": "p1", "model": "gpt-4o-mini", "skills": [], "tier": "producer"},
            {"name": "p2", "model": "claude-haiku", "skills": [], "tier": "producer"},
        ],
    }
    assert finalize.confirm(state) is True
    out = capsys.readouterr().out
    prod_line = next(ln for ln in out.splitlines() if "Producers:" in ln)
    assert "?" not in prod_line
    assert "gpt-4o-mini" in prod_line
    assert "claude-haiku" in prod_line


# ═══ fold: test_setup_wizard_finalize_resweep.py ═══
# Re-sweep regression tests for setup_wizard.finalize (0.9.0 pre-ship).
#
# Finding 1 [LOW]: a key staged under the neutral ``API_KEY`` sentinel
# (``default_env_var_for``'s fallback for a malformed base_url) rendered a bogus
# ``api_key`` provider on the confirm line. ``API_KEY`` (7 chars) does not end
# with ``_API_KEY`` (8 chars), so the suffix strip missed it. ``_derive_providers``
# now skips the sentinel entirely.


def test_derive_providers_skips_neutral_api_key_sentinel():
    # The sentinel must contribute no provider name (not "api_key").
    state = {"staged_api_keys": {"API_KEY": "sk-x"}}
    assert finalize._derive_providers(state) == []


def test_derive_providers_skips_sentinel_case_insensitive():
    state = {"staged_api_keys": {"api_key": "sk-x", "Api_Key": "sk-y"}}
    assert finalize._derive_providers(state) == []


def test_derive_providers_sentinel_dropped_real_keys_kept():
    # A mixed bag: the sentinel is dropped, real providers survive.
    state = {
        "staged_api_keys": {
            "API_KEY": "sk-x",
            "OPENAI_API_KEY": "sk-y",
            "XAI_API_KEY": "sk-z",
        }
    }
    assert finalize._derive_providers(state) == ["openai", "xai"]


def test_finalize_skips_neutral_api_key_sentinel_directly():
    # finalize._derive_providers drops the neutral 'API_KEY' sentinel (the
    # malformed-url fallback) so it never renders as a real provider.
    state = {"staged_api_keys": {"API_KEY": "sk-x"}}
    assert finalize._derive_providers(state) == []


# ═══ fold: test_setup_wizard_budget_step_resweep_r3.py ═══
# Round-3 re-sweep regressions for ``setup_wizard.budget_step``.
#
# Covers the re-invocation clear-an-axis defect: on re-invocation the step
# pre-fills ``budget_caps`` from defaults, and the old code passed each
# prior value as ``prompt_nav``'s ``default`` — so a blank Enter returned
# the prior value (steps.py:133) and silently KEPT the cap, contradicting
# the "blank = unbounded" prompt copy. A single axis could not be cleared.
#
# The fix shows the prior value as a hint (not a ``default``); blank now
# truly clears the axis to unbounded, while typing ``k`` keeps the prior
# value without retyping.


def _feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(it))


def test_reinvocation_blank_clears_single_axis(monkeypatch):
    """Re-invocation with all three caps pre-set; user blanks the
    wall-clock axis and keeps the other two with 'k'. Blank must clear
    the axis to None (unbounded), not retain the prior value."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    # gate, wall-clock (blank=clear), tokens (k=keep), cost (k=keep)
    _feed(monkeypatch, ["y", "", "k", "k"])
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps["max_wall_clock_min"] is None  # the bug: was 30.0
    assert caps["max_tokens"] == 50000
    assert caps["max_cost_usd"] == 5.0


def test_reinvocation_blank_clears_all_axes(monkeypatch):
    """Blank on every axis during re-invocation clears every cap, even
    though all three were previously set."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    _feed(monkeypatch, ["y", "", "", ""])
    result = budget_step.run(state)
    assert result == "captured"
    assert state["budget_caps"] == {
        "max_wall_clock_min": None,
        "max_tokens": None,
        "max_cost_usd": None,
    }


def test_reinvocation_keep_sentinel_retains_prior(monkeypatch):
    """'k' on an axis with a prior value keeps it; the convenience path
    that replaces the old implicit blank-keeps behavior."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    _feed(monkeypatch, ["y", "k", "k", "k"])
    result = budget_step.run(state)
    assert result == "captured"
    assert state["budget_caps"] == {
        "max_wall_clock_min": 30.0,
        "max_tokens": 50000,
        "max_cost_usd": 5.0,
    }


def test_reinvocation_new_value_overrides_prior(monkeypatch):
    """Typing a fresh value replaces the prior cap on that axis."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    _feed(monkeypatch, ["y", "45", "k", "k"])
    result = budget_step.run(state)
    assert result == "captured"
    assert state["budget_caps"]["max_wall_clock_min"] == 45.0


def test_keep_sentinel_inert_when_no_prior(monkeypatch):
    """On a first run (no prior caps) there is nothing to keep, so 'k'
    is just invalid text: the validator rejects it and re-prompts. This
    guards against 'k' being silently treated as a value."""
    # gate=y, wall-clock: 'k' (rejected) then '30'; tokens blank; cost blank
    _feed(monkeypatch, ["y", "k", "30", "", ""])
    state: dict = {}
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps["max_wall_clock_min"] == 30.0
    assert caps["max_tokens"] is None
    assert caps["max_cost_usd"] is None


def test_prompt_axis_blank_returns_none_with_prior():
    """Unit-level: the per-axis helper returns None on blank input even
    when a prior value exists (the heart of the clear-an-axis fix)."""
    import builtins

    orig = builtins.input
    try:
        builtins.input = lambda *_a, **_k: ""
        v = budget_step._prompt_axis(
            "Wall-clock cap in minutes",
            prior=30.0,
            prior_display="30",
            parse=budget_step._parse_optional_float,
        )
    finally:
        builtins.input = orig
    assert v is None


def test_prompt_axis_keep_returns_prior():
    """Unit-level: 'k' returns the exact prior value."""
    import builtins

    orig = builtins.input
    try:
        builtins.input = lambda *_a, **_k: "k"
        v = budget_step._prompt_axis(
            "Token cap",
            prior=50000,
            prior_display="50000",
            parse=budget_step._parse_optional_int,
        )
    finally:
        builtins.input = orig
    assert v == 50000


# ═══ fold: test_setup_wizard_embedded_llm_step_r2_audit.py ═══
# R2 audit regression: embedded_llm_step cache-dir / runtime agreement.
#
# Finding (MEDIUM/integration): the wizard prefetched the routing embedder
# into ``get_cache_root()/embeddings`` — a directory the runtime consumer
# (``semantic_router``) NEVER reads. ``semantic_router`` calls
# ``TextEmbedding(..., cuda=Device.CPU)`` with no ``cache_dir``, so it lands
# in fastembed's DEFAULT root (``$FASTEMBED_CACHE_PATH`` or
# ``<tmpdir>/fastembed_cache``). Net effect: the prefetch was a no-op (the
# first task re-downloaded) and ``is_cached()`` reported "not cached" even
# after a real run had populated fastembed's default cache.
#
# These tests pin the bug: ``cache_dir()`` MUST resolve to fastembed's own
# default root so the prefetch populates — and ``is_cached()`` inspects —
# the directory the runtime actually reads.


def test_cache_dir_honors_fastembed_cache_path_env(tmp_path, monkeypatch):
    """When FASTEMBED_CACHE_PATH is set, cache_dir() must point there —
    exactly the dir fastembed (and thus semantic_router) will use."""
    target = tmp_path / "fe"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(target))
    config.reload()
    assert embedded_llm_step.cache_dir() == target


def test_cache_dir_defaults_to_fastembed_tmp_root(tmp_path, monkeypatch):
    """With no FASTEMBED_CACHE_PATH, cache_dir() must equal fastembed's
    documented default (<tmpdir>/fastembed_cache) — NOT
    get_cache_root()/embeddings (the dir the runtime never reads)."""
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    # Point get_cache_root somewhere obvious so a regression to the old
    # behavior would resolve under it and fail this assertion.
    cfg_cache = tmp_path / "modulatio-cache"
    monkeypatch.setattr(config, "_fallback_cache_root", lambda: str(cfg_cache))
    config.reload()

    resolved = embedded_llm_step.cache_dir()
    expected = Path(tempfile.gettempdir()) / "fastembed_cache"
    assert resolved == expected
    # Must NOT be the old wizard-private dir the runtime ignores.
    assert resolved != config.get_cache_root() / "embeddings"


def test_cache_dir_matches_fastembed_define_cache_dir_default(tmp_path, monkeypatch):
    """cache_dir() must agree with fastembed's OWN resolution helper for
    the no-cache_dir call shape semantic_router uses. If fastembed is
    available, assert byte-for-byte agreement; otherwise the constants
    above already pin the default."""
    import importlib.util

    if importlib.util.find_spec("fastembed") is None:  # pragma: no cover
        return
    from fastembed.common.utils import define_cache_dir

    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "fe2"))
    config.reload()
    # define_cache_dir(None) is exactly what semantic_router triggers
    # (TextEmbedding called without cache_dir).
    assert embedded_llm_step.cache_dir() == define_cache_dir(None)


def test_prefetch_writes_into_runtime_cache_dir(tmp_path, monkeypatch):
    """prefetch() must pass the runtime cache dir to TextEmbedding so the
    download lands where the runtime reads. Regression guard: the
    captured cache_dir must equal cache_dir() (fastembed's default
    root), proving the prefetch is no longer a no-op."""
    target = tmp_path / "fe3"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(target))
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

    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.common", type("X", (), {})())
    monkeypatch.setitem(sys.modules, "fastembed.common.types", fake_types)

    ok = embedded_llm_step.prefetch("some-org/embed-model")
    assert ok is True
    # The download MUST target the runtime cache dir, not a wizard-private
    # location.
    assert captured["cache_dir"] == str(embedded_llm_step.cache_dir())
    assert captured["cache_dir"] == str(target)


def test_is_cached_sees_runtime_populated_cache(tmp_path, monkeypatch):
    """A model already cached at fastembed's default location (as a real
    runtime task would populate) must be detected as cached on a later
    wizard reconfigure — the no-op bug previously checked a different
    dir and always re-prompted."""
    target = tmp_path / "fe4"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(target))
    monkeypatch.setattr(
        config, "get_embedding_model", lambda: "sentence-transformers/all-MiniLM-L6-v2"
    )
    config.reload()

    # Emulate a runtime-populated fastembed cache for the active model.
    model_dir = embedded_llm_step.cache_dir() / "models--sentence-transformers--all-MiniLM-L6-v2"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.onnx").write_text("fake onnx")

    assert embedded_llm_step.is_cached() is True


# ═══ fold: test_setup_wizard_steps_resweep.py ═══
# Re-sweep regression for the Models-step BACK / staged-api-keys defect.
#
# Finding 1 [MEDIUM/correctness], filed at steps.py:228 (the
# ``pop_state(steps[step_idx], state)`` call site in ``run_step_machine``).
#
# Root cause is NOT in the product-agnostic state-machine framework
# (steps.py) — that module correctly knows nothing about ``staged_api_keys``.
# The data-specific defect lives one layer up, in
# ``modulatio.setup_wizard.__init__._pop_state``: its ``keys_per_step``
# table pops BOTH ``staged_api_keys`` and ``configured_models`` for the
# "models" step on BACK.
#
# But the models step (provider_step.run) persists model_presets.json to
# disk *immediately* (add_preset writes through). The pasted API-key VALUES,
# however, live ONLY in ``state['staged_api_keys']``. So pressing BACK on the
# Models step drops the in-memory key values while the presets that reference
# them survive on disk. On re-entry ``run()`` does
# ``setdefault('staged_api_keys', {})`` — a fresh empty dict — leaving a
# half-configured model: the preset is present, but its key is no longer
# staged, so finalize won't write it to .env and the provider summary loses
# it. The fix: do NOT pop ``staged_api_keys`` on BACK from the models step
# (mirroring how the presets themselves persist on disk).
#
# These tests pin both halves of the contract:
#
#   * the steps.py framework faithfully invokes the caller-supplied
#     ``pop_state`` on BACK (the call site named in the finding), and
#   * the caller's ``_pop_state`` must PRESERVE ``staged_api_keys`` for the
#     models step so the on-disk presets and the in-memory key values stay
#     consistent across a BACK/re-enter.
#
# The second test FAILS until __init__._pop_state stops popping
# 'staged_api_keys' for the 'models' step.


# === Framework-level: run_step_machine BACK -> pop_state contract ===

def test_run_step_machine_calls_pop_state_on_back():
    """BACK from a non-first step pops the PREVIOUS-... — actually the
    current step's state — via the caller's pop_state, then steps back.

    Pins the exact behaviour at steps.py:227-228 the finding references:
    on BACK, ``pop_state(steps[step_idx], state)`` is invoked for the step
    the user is leaving, and the machine decrements the index.
    """
    state: dict = {"a_key": 1, "b_key": 2}
    popped: list[str] = []

    def pop_state(step_name: str, st: dict) -> None:
        popped.append(step_name)

    # Step "a" always advances. Step "b" goes BACK on its FIRST visit only,
    # then advances on its second visit so the machine terminates (else an
    # always-BACK "b" oscillates a<->b forever).
    calls: dict[str, int] = {"a": 0, "b": 0}

    def dispatch(step_name: str, st: dict):
        calls[step_name] += 1
        if step_name == "a":
            return "ok"  # always advance to b
        # step "b": BACK on first visit, advance on the second
        if calls["b"] == 1:
            return steps.BACK
        return "ok"

    steps.run_step_machine(
        state, ["a", "b"], dispatch, pop_state=pop_state
    )

    # BACK was pressed once on "b", so pop_state was called once for "b".
    assert popped == ["b"]
    assert calls["a"] == 2  # entered, advanced to b, returned to and re-advanced
    assert calls["b"] == 2  # first visit BACK, second visit advance


def test_run_step_machine_back_on_first_step_does_not_pop():
    """BACK on the first step is a no-op (no pop, no underflow)."""
    popped: list[str] = []

    def pop_state(step_name: str, st: dict) -> None:
        popped.append(step_name)

    seen = {"n": 0}

    def dispatch(step_name: str, st: dict):
        seen["n"] += 1
        if seen["n"] == 1:
            return steps.BACK  # ignored at first step
        return "ok"  # then complete

    steps.run_step_machine(state={}, steps=["only"], dispatch=dispatch, pop_state=pop_state)
    assert popped == []  # never popped — we were at the first step


# === Caller-level: the actual defect — staged keys must survive BACK ===

# The one-line fix lives in modulatio.setup_wizard.__init__._pop_state: the
# 'models' step's pop list no longer contains 'staged_api_keys'. The symptom
# surfaces at steps.py:228 (the pop_state call site), but the data-specific
# defect is the caller's pop table — fixed in lockstep with this resweep.
def test_pop_state_models_preserves_staged_api_keys():
    """BACK out of the Models step must NOT discard staged API-key values.

    The presets that reference these keys are already on disk and survive
    re-entry; dropping the key values leaves a half-configured model whose
    key is never written to .env. _pop_state must keep 'staged_api_keys'.
    """
    state = {
        "staged_api_keys": {"OPENAI_API_KEY": "sk-live-value"},
        "configured_models": ["gpt-x"],
    }

    setup_wizard._pop_state("models", state)

    # The pasted key value must remain in memory so finalize still writes it
    # and it stays consistent with the on-disk preset that references it.
    assert state.get("staged_api_keys") == {"OPENAI_API_KEY": "sk-live-value"}


# ═══ fold: test_setup_wizard___init___resweep_r3.py ═══
# Round-3 re-sweep regression tests for modulatio.setup_wizard.__init__.
#
# Finding 1 [LOW/correctness]: per finding #348 the embedded_llm prefetch step
# now runs BEFORE confirm (step 7 of 8). ``embedded_llm_step.prefetch()``
# downloads the routing embedder (potentially hundreds of MB) into fastembed's
# cache — a durable, reusable on-disk side effect. The abort handler only
# inspected model presets and pandoc/clipboard system installs; it did not
# account for a freshly-downloaded embedder. On a re-invocation where presets +
# system tools are unchanged, an abort AFTER a successful prefetch could claim
# "No changes written" even though a model was just written to the cache.
#
# The fix snapshots ``embedded_llm_step.is_cached(active_model)`` at wizard start
# (mirroring ``_system_tools_snapshot``) and, on abort, if it flipped to True,
# the message owns the (reusable) cache warm instead of claiming nothing changed.
#
# These tests drive the REAL ``run_setup`` to an abort with the cache probe +
# presets + system tools mocked, and assert the abort message tells the truth.
# They also guard back-compat: a stable cache (no flip) keeps the prior wording.




def _run_with_r3(
    *,
    pandoc_seq=(True, True),
    clipboard_seq=(True, True),
    presets_seq=({}, {}),
    embed_cached_seq=(False, False),
    embed_model="BAAI/bge-small-en-v1.5",
):
    """Drive run_setup() to an abort with mocked probes; return muted msgs.

    ``embed_cached_seq`` is [value-at-start, value-at-abort] for
    ``embedded_llm_step.is_cached`` — the embedder cache snapshot.
    """
    muted_calls: list[str] = []
    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch.object(pandoc_step, "is_installed", side_effect=list(pandoc_seq)),
        mock.patch.object(clipboard_step, "is_installed", side_effect=list(clipboard_seq)),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=[dict(p) for p in presets_seq],
        ),
        mock.patch.object(
            embedded_llm_step, "is_cached", side_effect=list(embed_cached_seq)
        ),
        mock.patch.object(
            setup_wizard.config, "get_embedding_model", return_value=embed_model
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()
    return result, muted_calls


def test_abort_after_prefetch_does_not_claim_no_changes():
    """Embedder absent at start, cached at abort (prefetched mid-run) → the
    abort message must NOT lie with 'No changes written'."""
    result, muted = _run_with_r3(embed_cached_seq=(False, True))
    assert result is False
    assert len(muted) == 1
    msg = muted[0]
    assert "No changes written" not in msg
    assert "cache" in msg.lower()
    # The active model id is surfaced so the user knows what was downloaded.
    assert "BAAI/bge-small-en-v1.5" in msg


def test_abort_after_prefetch_reports_download_to_reusable_cache():
    """The message frames the side effect as a reusable cache warm (a download
    that survives + is reused), not a destructive write."""
    _result, muted = _run_with_r3(embed_cached_seq=(False, True))
    msg = muted[0]
    assert "downloaded" in msg.lower()
    assert "reusable" in msg.lower()


def test_abort_with_stable_cache_keeps_honest_no_changes():
    """Cache already present at start (and still present) → no flip, so the
    honest 'No changes written' message is preserved (no spurious claim)."""
    result, muted = _run_with_r3(embed_cached_seq=(True, True))
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_abort_with_no_cache_either_time_keeps_honest_no_changes():
    """Embedder never cached during the run → no embedded clause added."""
    result, muted = _run_with_r3(embed_cached_seq=(False, False))
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_abort_reports_prefetch_alongside_presets():
    """When BOTH presets persisted AND the embedder was downloaded, the abort
    message acknowledges both."""
    result, muted = _run_with_r3(
        presets_seq=({}, {"m": {"label": "x"}}),
        embed_cached_seq=(False, True),
    )
    assert result is False
    msg = muted[0]
    assert "No changes written" not in msg
    assert "saved" in msg.lower() or "model changes" in msg.lower()
    assert "cache" in msg.lower()


def test_abort_reports_prefetch_alongside_system_install():
    """Embedder download + a system-tool install are both surfaced; because a
    durable cache write happened, the tail is 'no other settings' not 'no
    configuration'."""
    result, muted = _run_with_r3(
        pandoc_seq=(False, True),
        embed_cached_seq=(False, True),
    )
    assert result is False
    msg = muted[0]
    assert "pandoc" in msg
    assert "cache" in msg.lower()
    assert "no other settings were written" in msg


def test_embedded_model_snapshot_swallows_probe_errors():
    """A cache probe that raises is treated as 'not cached' so the abort path
    can't crash on a flaky is_cached()."""
    with (
        mock.patch.object(
            setup_wizard.config, "get_embedding_model", return_value="x/y"
        ),
        mock.patch.object(
            embedded_llm_step, "is_cached", side_effect=RuntimeError("boom")
        ),
    ):
        model_id, cached = setup_wizard._embedded_model_snapshot()
    assert model_id == "x/y"
    assert cached is False


def test_embedded_model_snapshot_swallows_config_errors():
    """If even resolving the active model id raises, the snapshot returns a
    safe ('', False) rather than crashing the abort path."""
    with mock.patch.object(
        setup_wizard.config,
        "get_embedding_model",
        side_effect=RuntimeError("no config"),
    ):
        model_id, cached = setup_wizard._embedded_model_snapshot()
    assert model_id == ""
    assert cached is False


def test_abort_prefetch_with_unknown_model_id_omits_label_gracefully():
    """If the model id resolves empty (config error path), the clause still
    reads cleanly without a dangling '()' label."""
    # is_cached can't flip to True without a model id in practice, but guard the
    # rendering: an empty model id must not produce '... model () was ...'.
    result, muted = _run_with_r3(embed_cached_seq=(False, True), embed_model="")
    assert result is False
    msg = muted[0]
    assert "()" not in msg
    assert "cache" in msg.lower()


# ═══ fold: test_setup_wizard___init___resweep_r4.py ═══
# Round-4 re-sweep regression tests for modulatio.setup_wizard.__init__.
#
# Finding 1 [LOW/correctness]: BACK out of the agents step popped the
# disk-loaded team pre-fill. ``_load_existing_state`` pre-populates
# ``triad_agents`` / ``worker_agents`` from the saved team_template.json so the
# agents step starts on the user's current team with edit/keep semantics. The
# ``_pop_state('agents', ...)`` entry removed BOTH keys on a BACK-out, so a user
# who entered the agents step and then backed out lost the pre-fill (and any
# in-progress picks) on re-entry, restarting on an empty re-provision. Fix:
# ``agents`` pops nothing — the step overwrites both keys on re-entry.
#
# Finding 2 [LOW/correctness]: the abort handler re-capitalized the lead clause
# only ``if presets_changed``. On a run whose ONLY durable side effect was the
# embedded-LLM cache warm (no presets, no system-tool install), the single
# clause starts lowercase ('the embedded routing model ...'), producing
# "Setup aborted. the embedded routing model ...". Fix: capitalize the lead
# unless the LEADING clause is a verbatim tool-name match (tool-install-only
# must stay lowercase for the verbatim 'pandoc'/'clipboard' test contract).


# === Finding 1: _pop_state('agents', ...) ===


def test_pop_state_agents_preserves_team_prefill():
    """BACK out of the agents step must NOT drop the disk-loaded team."""
    state = {
        "triad_agents": [{"tier": "leader"}, {"tier": "qc"}],
        "worker_agents": [{"tier": "producer", "model": "m"}],
    }
    setup_wizard._pop_state("agents", state)
    # Both keys survive so the next entry starts on the current team.
    assert state["triad_agents"] == [{"tier": "leader"}, {"tier": "qc"}]
    assert state["worker_agents"] == [{"tier": "producer", "model": "m"}]


def test_pop_state_agents_is_a_noop():
    """The agents step pops nothing — its keys are rebuilt on re-entry."""
    assert setup_wizard._pop_state.__doc__  # smoke: the helper still exists
    before = {"triad_agents": ["x"], "worker_agents": ["y"], "other": 1}
    state = dict(before)
    setup_wizard._pop_state("agents", state)
    assert state == before


def test_pop_state_other_steps_still_clear():
    """Wizard steps clear their own keys on BACK. (The models + agents steps were
    removed — model/agent config lives in the TUI Config tab now.)"""
    state = {"budget_caps": {"wall": 1}}
    setup_wizard._pop_state("budget", state)
    assert "budget_caps" not in state


# === Finding 2: abort-message lead capitalization ===




def _run_with_r4(
    *,
    pandoc_seq=(True, True),
    clipboard_seq=(True, True),
    presets_seq=({}, {}),
    embed_cached_seq=(False, False),
    embed_model="BAAI/bge-small-en-v1.5",
):
    """Drive run_setup() to an abort with mocked probes; return muted msgs.

    ``embed_cached_seq`` is [value-at-start, value-at-abort] for
    ``embedded_llm_step.is_cached`` — the embedder cache snapshot.
    """
    muted_calls: list[str] = []
    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch.object(pandoc_step, "is_installed", side_effect=list(pandoc_seq)),
        mock.patch.object(
            clipboard_step, "is_installed", side_effect=list(clipboard_seq)
        ),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=[dict(p) for p in presets_seq],
        ),
        mock.patch.object(
            embedded_llm_step, "is_cached", side_effect=list(embed_cached_seq)
        ),
        mock.patch.object(
            setup_wizard.config, "get_embedding_model", return_value=embed_model
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()
    return result, muted_calls


def test_embed_only_abort_lead_is_capitalized():
    """Embed cache warm is the ONLY side effect → the lead must be capitalized,
    not 'Setup aborted. the embedded routing model ...'."""
    result, muted = _run_with_r4(embed_cached_seq=(False, True))
    assert result is False
    msg = muted[0]
    # The regressing string: lowercase 'the' immediately after the period.
    assert "Setup aborted. the embedded" not in msg
    assert "Setup aborted. The embedded" in msg


def test_embed_only_abort_keeps_clause_wording():
    """Capitalizing the lead must not disturb the rest of the prose."""
    _result, muted = _run_with_r4(embed_cached_seq=(False, True))
    msg = muted[0]
    assert "downloaded to a reusable cache" in msg
    assert "no other settings were written" in msg


def test_tool_install_only_abort_lead_stays_lowercase():
    """Back-compat: a tool-install-only abort keeps the verbatim lowercase
    tool name ('pandoc was installed ...') so existing matchers hold."""
    result, muted = _run_with_r4(pandoc_seq=(False, True))
    assert result is False
    msg = muted[0]
    assert "Setup aborted. pandoc was installed" in msg


def test_tool_install_plus_embed_lead_stays_lowercase():
    """When a tool-install clause LEADS (no presets) but an embed cache warm
    also happened, the lead is still the verbatim tool name → lowercase."""
    result, muted = _run_with_r4(pandoc_seq=(False, True), embed_cached_seq=(False, True))
    assert result is False
    msg = muted[0]
    assert "Setup aborted. pandoc was installed" in msg
    assert "cache" in msg.lower()


def test_preset_led_abort_lead_capitalized():
    """Back-compat: a preset-led abort still capitalizes its lead clause."""
    result, muted = _run_with_r4(presets_seq=({}, {"m": {"label": "x"}}))
    assert result is False
    msg = muted[0]
    assert msg.startswith("Setup aborted. Configured models")


def test_embed_only_abort_with_empty_model_id_capitalizes_cleanly():
    """An empty model id (config probe failure) still capitalizes the lead and
    omits a dangling '()' label."""
    result, muted = _run_with_r4(embed_cached_seq=(False, True), embed_model="")
    assert result is False
    msg = muted[0]
    assert "Setup aborted. The embedded" in msg
    assert "()" not in msg
