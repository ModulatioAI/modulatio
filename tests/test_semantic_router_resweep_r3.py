"""Round-3 re-sweep regression tests for ``semantic_router``.

Finding 1 (LOW/correctness): ``semantic_router`` pinned ``_EMBED_MODEL``
and ``_EMBED_DIM`` at module import while ``qc_history`` and
``team_memory`` resolve ``config.get_embedding_model()`` LIVE per call to
honor a mid-process wizard model-swap. Since one shared ``FastEmbedder``
is threaded into all three subsystems, a pinned model meant the embedder
loaded the OLD model + reported the OLD dim while the others fingerprinted
the NEW model name → silent disagreement on cache rebuild.

These prove the embedder now resolves the model live at load time,
re-derives ``dim`` from it, and that the index metadata fingerprints the
live model so a swap invalidates the cache. They run without downloading
MiniLM — fastembed is stubbed in-process (same pattern as the preship
suite).
"""

from __future__ import annotations

import sys
import types

from modulatio import config, semantic_router
from modulatio.semantic_router import FastEmbedder


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
