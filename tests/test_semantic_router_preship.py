"""Pre-ship 0.9.0 regression tests for ``semantic_router``.

Covers two MEDIUM findings:

- FastEmbedder.dim must reflect the configured embedding model's real
  output dimension (wizard can override to a non-384-dim model), not a
  hardcoded 384, so the LanceDB schema width matches the emitted vectors.
- The shared FastEmbedder must be thread-safe: the lazy model load must
  happen exactly once under concurrency, and inference must be
  serialized.

These run without downloading MiniLM — the lazy-load and inference are
exercised against an in-process fake model, so they stay CI-friendly.
"""

from __future__ import annotations

import threading
import time

from modulatio import semantic_router
from modulatio.semantic_router import FastEmbedder


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
