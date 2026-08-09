"""Tests for the semantic routing fallback (slice #6e.a).

All tests use ``StubEmbedder`` with deterministic vectors — no MiniLM
download, no ONNX. The real ``FastEmbedder`` is exercised by manual
smoke tests after install, not by the unit suite (80MB download +
~200ms per embed would be CI-hostile).

``config.get_cache_root`` is monkeypatched to ``tmp_path`` so tests never
touch ``~/.cache/modulatio/semantic/``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import config, roster, semantic_router, vault
from modulatio.semantic_router import StubEmbedder
from modulatio.types import Task
import threading
import time
from modulatio.semantic_router import FastEmbedder
import sys
import types


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(config, "get_cache_root", lambda: tmp_path / "cache")
    vault.init_project(PROJECT_CODE, "Test", "test")
    return PROJECT_CODE


def _task(required_skills: list[str], description: str = "x") -> Task:
    return Task(
        id="X-T-001",
        project_id=uuid4(),
        goal_id="X-G-001",
        description=description,
        required_skills=required_skills,
    )


def _save_agent(
    project_code: str,
    id: str,
    skills: list[str],
    identity: str = "",
    capability_tags: list[str] | None = None,
) -> roster.Agent:
    a = roster.Agent(
        id=id,
        name=id,
        identity=identity,
        skills=skills,
        capability_tags=capability_tags or [],
        cost_class="paid-cloud",
    )
    roster.save(a, project_code)
    return a


# ── StubEmbedder basics ────────────────────────────────────────────────────

def test_stub_embedder_is_deterministic():
    e = StubEmbedder(dim=16)
    v1 = e.embed_text("hello")
    v2 = e.embed_text("hello")
    assert v1 == v2
    # Different text → different vector.
    assert e.embed_text("world") != v1


def test_stub_embedder_vectors_are_unit_norm():
    e = StubEmbedder(dim=16)
    v = e.embed_text("anything")
    norm = sum(x * x for x in v) ** 0.5
    assert 0.99 < norm < 1.01


def test_stub_embedder_overrides_take_precedence():
    v = [1.0] + [0.0] * 15
    e = StubEmbedder(dim=16, overrides={"pinned": v})
    assert e.embed_text("pinned") == v
    # Non-overridden text still hashes normally.
    other = e.embed_text("unpinned")
    assert other != v


# ── ensure_agent_vectors ──────────────────────────────────────────────────

def test_ensure_agent_vectors_builds_index_on_first_call(project):
    _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)

    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True

    meta_path = semantic_router._meta_path(project)
    assert meta_path.exists()


def test_ensure_agent_vectors_stable_on_second_call_with_same_roster(project):
    _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)

    semantic_router.ensure_agent_vectors(project, embedder)
    rebuilt_again = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt_again is False


def test_ensure_agent_vectors_rebuilds_when_agent_added(project):
    _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)
    semantic_router.ensure_agent_vectors(project, embedder)

    _save_agent(project, "researcher", ["researcher"])
    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True


def test_ensure_agent_vectors_rebuilds_when_skills_change(project):
    a = _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)
    semantic_router.ensure_agent_vectors(project, embedder)

    # Same id, different skill set.
    a.skills = ["drafter", "contrarian-argument"]
    roster.save(a, project)
    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True


def test_ensure_agent_vectors_empty_roster_saves_meta_and_returns_true(project):
    """Cold project with no agents: record the hash (so the next call
    short-circuits if still empty) but don't try to create a table.
    LanceDB won't create an empty table without rows."""
    embedder = StubEmbedder(dim=16)

    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True
    assert semantic_router._meta_path(project).exists()

    # Next call with still-empty roster is a no-op.
    rebuilt_again = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt_again is False


def test_ensure_agent_vectors_rebuilds_when_dim_changes(project):
    """Switching to an embedder with a different output dimension must
    rebuild — the LanceDB schema is fixed-width and stale vectors would
    be unusable."""
    _save_agent(project, "drafter", ["drafter"])

    semantic_router.ensure_agent_vectors(project, StubEmbedder(dim=16))
    rebuilt = semantic_router.ensure_agent_vectors(project, StubEmbedder(dim=32))
    assert rebuilt is True


# ── semantic_match ────────────────────────────────────────────────────────

def test_semantic_match_returns_none_when_index_missing(project):
    """ensure_agent_vectors never called → no index → no match. Caller
    (plan_dispatch) falls through to ROSTER_GAP."""
    embedder = StubEmbedder(dim=16)
    result = semantic_router.semantic_match(
        _task(["drafter"]), project, embedder
    )
    assert result is None


def test_semantic_match_returns_none_when_roster_empty(project):
    """Empty roster → ensure_agent_vectors writes meta but no table →
    semantic_match must handle missing table gracefully."""
    embedder = StubEmbedder(dim=16)
    semantic_router.ensure_agent_vectors(project, embedder)

    result = semantic_router.semantic_match(
        _task(["drafter"]), project, embedder
    )
    assert result is None


def test_semantic_match_returns_best_agent_above_threshold(project):
    """Task query vector exactly matches one agent → cosine 1.0 → top
    hit returned above any reasonable threshold."""
    a = _save_agent(project, "custom-agent", ["drafter", "contrarian-argument"])
    _save_agent(project, "researcher", ["researcher"])

    # Force matching vectors for the specific agent-text and task-text.
    unit_a = [1.0] + [0.0] * 15
    unit_b = [0.0, 1.0] + [0.0] * 14
    agent_text_a = semantic_router._agent_capability_text(a)
    from modulatio.roster import load as load_agent
    agent_text_b = semantic_router._agent_capability_text(
        load_agent("researcher", project)
    )

    task = _task(["drafter", "contrarian-argument"], description="contrarian artifact")
    task_text = semantic_router._task_query_text(task)

    embedder = StubEmbedder(
        dim=16,
        overrides={
            agent_text_a: unit_a,
            agent_text_b: unit_b,
            task_text: unit_a,  # identical to agent A
        },
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    result = semantic_router.semantic_match(task, project, embedder, threshold=0.5)
    assert result is not None
    matched_agent, score = result
    assert matched_agent.id == "custom-agent"
    assert score > 0.99  # near-perfect cosine match


def test_semantic_match_returns_none_below_threshold(project):
    """Top hit's similarity under threshold → None. Caller opens a
    ticket instead of forcing a bad match."""
    a = _save_agent(project, "drafter", ["drafter"])

    unit_a = [1.0] + [0.0] * 15
    orthogonal = [0.0, 1.0] + [0.0] * 14

    agent_text = semantic_router._agent_capability_text(a)
    task = _task(["drafter"], description="x")
    task_text = semantic_router._task_query_text(task)

    embedder = StubEmbedder(
        dim=16,
        overrides={agent_text: unit_a, task_text: orthogonal},
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    # Orthogonal vectors → cosine similarity 0 → below any positive threshold.
    result = semantic_router.semantic_match(task, project, embedder, threshold=0.1)
    assert result is None


def test_semantic_match_picks_closer_of_multiple_agents(project):
    """Two agents, one a close semantic match and one distant — best
    hit wins. Confirms the ranking, not just "any match"."""
    close = _save_agent(project, "close", ["drafter"])
    far = _save_agent(project, "far", ["drafter"])

    unit = [1.0] + [0.0] * 15
    near = [0.9, (1 - 0.81) ** 0.5] + [0.0] * 14  # cosine ~0.9 with unit
    distant = [0.3, (1 - 0.09) ** 0.5] + [0.0] * 14  # cosine ~0.3 with unit

    text_close = semantic_router._agent_capability_text(close)
    text_far = semantic_router._agent_capability_text(far)
    task = _task(["drafter"], description="x")
    task_text = semantic_router._task_query_text(task)

    embedder = StubEmbedder(
        dim=16,
        overrides={
            text_close: near,
            text_far: distant,
            task_text: unit,
        },
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    result = semantic_router.semantic_match(task, project, embedder, threshold=0.5)
    assert result is not None
    agent, score = result
    assert agent.id == "close"
    assert score > 0.85


def test_semantic_match_handles_stale_index_agent_removed(project):
    """Index row references an agent id that's no longer in the roster
    → treat as miss rather than returning a ghost. Keeps dispatch
    honest when the roster evolves between index-builds."""
    a = _save_agent(project, "drafter", ["drafter"])
    unit = [1.0] + [0.0] * 15

    agent_text = semantic_router._agent_capability_text(a)
    task = _task(["drafter"], description="x")
    task_text = semantic_router._task_query_text(task)
    embedder = StubEmbedder(
        dim=16,
        overrides={agent_text: unit, task_text: unit},
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    # Delete the agent file under the roster dir (simulate external
    # roster edit without a re-index).
    (vault.project_dir(project) / "agents" / "drafter.md").unlink()

    result = semantic_router.semantic_match(task, project, embedder, threshold=0.5)
    assert result is None


# ═══ fold: test_semantic_router_preship.py ═══
# Pre-ship 0.9.0 regression tests for ``semantic_router``.
#
# Covers two MEDIUM findings:
#
# - FastEmbedder.dim must reflect the configured embedding model's real
#   output dimension (wizard can override to a non-384-dim model), not a
#   hardcoded 384, so the LanceDB schema width matches the emitted vectors.
# - The shared FastEmbedder must be thread-safe: the lazy model load must
#   happen exactly once under concurrency, and inference must be
#   serialized.
#
# These run without downloading MiniLM — the lazy-load and inference are
# exercised against an in-process fake model, so they stay CI-friendly.


# ── dim derives from the model ────────────────────────────────────────────


def test_embed_dim_for_known_minilm_is_384():
    # The shipped default model is 384-dim.
    assert semantic_router._embed_dim_for_model(
        "sentence-transformers/all-MiniLM-L6-v2"
    ) == 384


def test_embed_dim_for_larger_model_is_not_384():
    # A wizard-overridable larger model (bge-large is 1024-dim) must NOT
    # collapse to the old hardcoded 384 — that was the bug.
    dim = semantic_router._embed_dim_for_model("BAAI/bge-large-en-v1.5")
    assert dim == 1024
    assert dim != 384


def test_embed_dim_unknown_model_falls_back():
    # An unrecognized custom model name degrades to the safe default
    # rather than raising.
    assert semantic_router._embed_dim_for_model(
        "some/never-heard-of-this-model"
    ) == semantic_router._DEFAULT_EMBED_DIM


def test_fastembedder_dim_matches_configured_model(monkeypatch):
    # FastEmbedder.dim must equal the configured model's real dim, not a
    # static constant. We rebuild the module-level derivation under a
    # patched model name to prove dim tracks the model.
    monkeypatch.setattr(
        semantic_router, "_EMBED_MODEL", "BAAI/bge-large-en-v1.5"
    )
    derived = semantic_router._embed_dim_for_model(
        semantic_router._EMBED_MODEL
    )
    assert derived == 1024
    # The class default for the shipped config remains coherent.
    assert FastEmbedder.dim == semantic_router._EMBED_DIM


# ── thread-safe lazy load + serialized inference ─────────────────────────


class _FakeVec:
    def __init__(self, n: int) -> None:
        self._n = n

    def tolist(self) -> list[float]:
        return [0.0] * self._n


class _FakeModel:
    """Records overlapping inference calls to prove serialization."""

    def __init__(self) -> None:
        self.active = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def embed(self, texts):
        with self._lock:
            self.active += 1
            self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            time.sleep(0.01)
            return iter([_FakeVec(4) for _ in texts])
        finally:
            with self._lock:
                self.active -= 1


def test_lazy_load_happens_exactly_once_under_concurrency(monkeypatch):
    # Exercise the REAL _get by stubbing the fastembed module it imports,
    # so we prove the production double-checked lock loads exactly once.
    import sys
    import types

    load_count = {"n": 0}
    fake = _FakeModel()

    fe_mod = types.ModuleType("fastembed")

    def _ctor(model_name, cuda=None):
        time.sleep(0.02)  # widen the race window
        load_count["n"] += 1
        return fake

    fe_mod.TextEmbedding = _ctor
    types_mod = types.ModuleType("fastembed.common.types")
    types_mod.Device = types.SimpleNamespace(CPU="cpu")
    common_mod = types.ModuleType("fastembed.common")
    common_mod.types = types_mod

    monkeypatch.setitem(sys.modules, "fastembed", fe_mod)
    monkeypatch.setitem(sys.modules, "fastembed.common", common_mod)
    monkeypatch.setitem(sys.modules, "fastembed.common.types", types_mod)

    emb = FastEmbedder()
    errors = []

    def worker():
        try:
            assert emb._get() is fake
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert load_count["n"] == 1


def test_inference_is_serialized(monkeypatch):
    emb = FastEmbedder()
    fake = _FakeModel()
    emb._model = fake  # pre-load so _get returns the fake directly

    errors = []

    def worker():
        try:
            emb.embed_text("x")
            emb.embed_texts(["a", "b"])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # The instance lock must prevent overlapping embed() calls.
    assert fake.max_concurrent == 1


# ═══ fold: test_semantic_router_r2_audit.py ═══
# Round-2 audit regression for semantic_router (LOW/integration).
#
# Finding: semantic_router hardcoded ``_CACHE_ROOT = ~/.cache/modulatio/semantic``,
# ignoring ``config.get_cache_root()`` (the wizard ``cache_root`` override and
# ``$XDG_CACHE_HOME``). The cache root is now resolved per-call via
# ``_cache_root()`` so config/wizard overrides land in one place — mirroring
# ``qc_history._cache_root``.


def test_cache_dir_honors_config_cache_root(monkeypatch, tmp_path: Path) -> None:
    """``_cache_dir`` must derive from ``config.get_cache_root()/semantic``,
    not a hardcoded ``~/.cache`` path. Before the fix this returned the
    home-dir cache regardless of the wizard override."""
    override = tmp_path / "wizard-cache"
    monkeypatch.setattr(config, "get_cache_root", lambda: override)

    got = semantic_router._cache_dir("ABC")

    assert got == override / "semantic" / "abc"


def test_cache_root_is_resolved_per_call(monkeypatch, tmp_path: Path) -> None:
    """Per-call resolution: a config change between calls is reflected
    without a process restart (the import-time-constant bug would freeze
    the first value)."""
    first = tmp_path / "one"
    monkeypatch.setattr(config, "get_cache_root", lambda: first)
    assert semantic_router._cache_root() == first / "semantic"

    second = tmp_path / "two"
    monkeypatch.setattr(config, "get_cache_root", lambda: second)
    assert semantic_router._cache_root() == second / "semantic"


def test_cache_root_honors_xdg_cache_home(monkeypatch, tmp_path: Path) -> None:
    """End-to-end through config: setting ``$XDG_CACHE_HOME`` (no wizard
    override) flows into the semantic cache location."""
    # No defaults.json cache_root override -> config falls back to XDG.
    monkeypatch.setattr(config, "_load_defaults", lambda: {})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    root = semantic_router._cache_root()

    assert root == tmp_path / "xdg" / "modulatio" / "semantic"


# ═══ fold: test_semantic_router_resweep_r3.py ═══
# Round-3 re-sweep regression tests for ``semantic_router``.
#
# ``semantic_router`` pinned ``_EMBED_MODEL``
# and ``_EMBED_DIM`` at module import while ``qc_history`` and
# ``team_memory`` resolve ``config.get_embedding_model()`` LIVE per call to
# honor a mid-process wizard model-swap. Since one shared ``FastEmbedder``
# is threaded into all three subsystems, a pinned model meant the embedder
# loaded the OLD model + reported the OLD dim while the others fingerprinted
# the NEW model name → silent disagreement on cache rebuild.
#
# These prove the embedder now resolves the model live at load time,
# re-derives ``dim`` from it, and that the index metadata fingerprints the
# live model so a swap invalidates the cache. They run without downloading
# MiniLM — fastembed is stubbed in-process (same pattern as the preship
# suite).


def _install_fake_fastembed(monkeypatch, dim_by_model: dict[str, int]):
    """Stub the ``fastembed`` module so ``FastEmbedder._get`` and
    ``_embed_dim_for_model`` resolve against an in-process fake catalog."""
    seen: dict[str, str] = {}

    class _FakeVec:
        def __init__(self, n: int) -> None:
            self._n = n

        def tolist(self) -> list[float]:
            return [0.0] * self._n

    class _FakeModel:
        def __init__(self, model_name) -> None:
            self._dim = dim_by_model.get(model_name, 384)

        def embed(self, texts):
            return iter([_FakeVec(self._dim) for _ in texts])

    def _ctor(model_name, cuda=None):
        seen["model"] = model_name
        return _FakeModel(model_name)

    def _list_supported():
        return [{"model": m, "dim": d} for m, d in dim_by_model.items()]

    _ctor.list_supported_models = staticmethod(_list_supported)

    fe_mod = types.ModuleType("fastembed")
    fe_mod.TextEmbedding = _ctor
    types_mod = types.ModuleType("fastembed.common.types")
    types_mod.Device = types.SimpleNamespace(CPU="cpu")
    common_mod = types.ModuleType("fastembed.common")
    common_mod.types = types_mod

    monkeypatch.setitem(sys.modules, "fastembed", fe_mod)
    monkeypatch.setitem(sys.modules, "fastembed.common", common_mod)
    monkeypatch.setitem(sys.modules, "fastembed.common.types", types_mod)
    return seen


def test_fastembedder_loads_live_config_model_not_import_pin(monkeypatch):
    # The instance must load whatever config reports AT LOAD TIME, not the
    # import-time ``_EMBED_MODEL`` snapshot. Point config at a different
    # model than the pinned one and prove the embedder loads the live one.
    live_model = "BAAI/bge-large-en-v1.5"
    assert semantic_router._EMBED_MODEL != live_model  # precondition
    monkeypatch.setattr(config, "get_embedding_model", lambda: live_model)
    seen = _install_fake_fastembed(monkeypatch, {live_model: 1024})

    emb = FastEmbedder()
    emb._get()

    assert seen["model"] == live_model


def test_fastembedder_dim_rederives_from_loaded_model(monkeypatch):
    # After loading, ``instance.dim`` must reflect the live model's real
    # output dimension, not the import-pinned ``_EMBED_DIM``.
    live_model = "BAAI/bge-large-en-v1.5"
    monkeypatch.setattr(config, "get_embedding_model", lambda: live_model)
    _install_fake_fastembed(monkeypatch, {live_model: 1024})

    emb = FastEmbedder()
    # Before load, dim is the safe class default.
    assert emb.dim == semantic_router._EMBED_DIM
    emb._get()
    # After load, dim tracks the live model.
    assert emb.dim == 1024
    assert emb.dim != semantic_router._EMBED_DIM


def test_model_name_property_reflects_live_then_loaded(monkeypatch):
    live_model = "BAAI/bge-large-en-v1.5"
    monkeypatch.setattr(config, "get_embedding_model", lambda: live_model)
    _install_fake_fastembed(monkeypatch, {live_model: 1024})

    emb = FastEmbedder()
    # Before load: resolved live from config.
    assert emb.model_name == live_model
    emb._get()
    # After load: frozen to what was actually loaded.
    assert emb.model_name == live_model


def test_config_hash_uses_live_model(monkeypatch):
    # A model swap must change the config hash so the index invalidates,
    # even though ``_EMBED_MODEL`` was pinned at import.
    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-a")
    hash_a = semantic_router._config_hash([])
    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-b")
    hash_b = semantic_router._config_hash([])
    assert hash_a != hash_b


def test_ensure_agent_vectors_meta_uses_live_model(monkeypatch, tmp_path):
    # Empty-roster path writes metadata; the recorded ``embedding_model``
    # must be the LIVE model, not the import pin, so a later swap is
    # detected as a cache miss.
    from modulatio import roster as _roster

    monkeypatch.setattr(config, "get_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(_roster, "list_agents", lambda code: [])
    swapped = "BAAI/bge-large-en-v1.5"
    monkeypatch.setattr(config, "get_embedding_model", lambda: swapped)

    emb = semantic_router.StubEmbedder(dim=1024)
    rebuilt = semantic_router.ensure_agent_vectors("proj", emb)
    assert rebuilt is True

    meta = semantic_router._load_meta("proj")
    assert meta["embedding_model"] == swapped

    # Calling again with the SAME live model is a cache hit (no rebuild).
    assert semantic_router.ensure_agent_vectors("proj", emb) is False

    # Swapping the live model invalidates → rebuild again.
    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-other")
    assert semantic_router.ensure_agent_vectors("proj", emb) is True
