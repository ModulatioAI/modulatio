# SPDX-License-Identifier: Apache-2.0
"""R2 audit regression: embedded_llm_step cache-dir / runtime agreement.

Finding (MEDIUM/integration): the wizard prefetched the routing embedder
into ``get_cache_root()/embeddings`` — a directory the runtime consumer
(``semantic_router``) NEVER reads. ``semantic_router`` calls
``TextEmbedding(..., cuda=Device.CPU)`` with no ``cache_dir``, so it lands
in fastembed's DEFAULT root (``$FASTEMBED_CACHE_PATH`` or
``<tmpdir>/fastembed_cache``). Net effect: the prefetch was a no-op (the
first task re-downloaded) and ``is_cached()`` reported "not cached" even
after a real run had populated fastembed's default cache.

These tests pin the bug: ``cache_dir()`` MUST resolve to fastembed's own
default root so the prefetch populates — and ``is_cached()`` inspects —
the directory the runtime actually reads.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from modulatio import config
from modulatio.setup_wizard import embedded_llm_step


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
