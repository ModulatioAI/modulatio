# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Embedded LLM prefetch — final wizard step (silent if cache present).

Pre-downloads the embedder used by skill-routing (slice #6e) and
qc-history retrieval so the first task doesn't block on a cold
fetch. Auto-detects cache; if present → skip silently. If missing →
download + report progress.

Single source of truth for the model id is ``config.get_embedding_model()``
(wizard-overridable via ``defaults.json["embedding_model"]``). The
prefetch step asks config; it does NOT carry its own hardcoded model
id.

the prior implementation
hardcoded ``BAAI/bge-small-en-v1.5`` as a fallback and tried to import
a nonexistent ``_ROUTING_MODEL`` from ``semantic_router`` (the real
symbol is ``_EMBED_MODEL``). Setup could report "embedded LLM cached"
while the actual routing model still downloaded later on first use.
The cache-detection heuristic ("any subdir under embeddings/") would
also report cached for any embedder, even if it wasn't the active one.
Both fixed: model id comes from config; cache detection is now slug-
aware (the active model's id must appear in a populated subdir).

Cache location must match the RUNTIME consumer.
``semantic_router`` instantiates ``TextEmbedding(..., cuda=Device.CPU)``
with NO ``cache_dir``, so it lands in fastembed's DEFAULT cache root
(``$FASTEMBED_CACHE_PATH`` or ``<tmpdir>/fastembed_cache``). A prior
implementation prefetched into ``get_cache_root()/embeddings`` — a
directory the runtime never reads — so the prefetch was a no-op (the
first task re-downloaded) and ``is_cached()`` always reported "not
cached" after a real run. ``cache_dir()`` now resolves to fastembed's
OWN default root and ``prefetch()`` pins that same root, so the wizard
writes/checks exactly where the runtime reads.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from modulatio import config, theme


# Mirror of fastembed.common.utils.define_cache_dir's default when no
# explicit cache_dir is passed (which is how semantic_router calls it).
_FASTEMBED_CACHE_ENV = "FASTEMBED_CACHE_PATH"
_FASTEMBED_DEFAULT_LEAF = "fastembed_cache"


def _model_slug(model_id: str) -> str:
    """Leaf name of a HuggingFace-style model id, lowercased.

    For ``sentence-transformers/all-MiniLM-L6-v2`` this is
    ``all-minilm-l6-v2``. Used for slug-aware cache detection: the
    fastembed / HuggingFace cache layout names subdirs after the model
    id (often as ``models--{org}--{name}``), so any directory that
    contains the slug is evidence of a cached copy.
    """
    leaf = model_id.split("/")[-1].strip()
    return leaf.lower()


def cache_dir() -> Path:
    """Where the embedder cache lives — fastembed's OWN default root.

    This MUST match where the runtime consumer (``semantic_router``)
    downloads to. That consumer calls ``TextEmbedding(..., cuda=...)``
    with no ``cache_dir``, so fastembed uses
    ``$FASTEMBED_CACHE_PATH`` or ``<tmpdir>/fastembed_cache``. We
    resolve the identical location so the prefetch populates — and
    ``is_cached()`` inspects — the directory the runtime actually
    reads. (Previously this returned ``get_cache_root()/embeddings``,
    which the runtime never reads, making the prefetch a no-op.)
    """
    env = os.environ.get(_FASTEMBED_CACHE_ENV, "").strip()
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / _FASTEMBED_DEFAULT_LEAF


def is_cached(model_id: str | None = None) -> bool:
    """True iff the cache contains files for the active embedding
    model.

    Slug-aware: checks for a populated subdir whose name contains
    the model's leaf slug. The prior heuristic (``any subdir
    exists``) reported cached for any prior embedder, which is wrong
    once the wizard's ``embedding_model`` override is in play.

    ``model_id`` defaults to ``config.get_embedding_model()``; tests
    can override.
    """
    d = cache_dir()
    if not d.exists() or not d.is_dir():
        return False
    target_slug = _model_slug(model_id or config.get_embedding_model())
    for child in d.iterdir():
        if not child.is_dir():
            continue
        if target_slug not in child.name.lower():
            continue
        # Populated check: at least one regular file under the
        # candidate subdir. An empty dir is not a cached model.
        for entry in child.rglob("*"):
            if entry.is_file():
                return True
    return False


def prefetch(model_id: str | None = None) -> bool:
    """Download the active embedder via fastembed, populating the
    cache. Returns True on success.

    ``model_id`` defaults to ``config.get_embedding_model()`` so the
    wizard's override (or the package fallback) drives what's
    fetched.
    """
    cache_path = cache_dir()
    cache_path.mkdir(parents=True, exist_ok=True)
    try:
        from fastembed import TextEmbedding
        from fastembed.common.types import Device
    except ImportError:
        theme.warn("fastembed not installed — skipping embedded LLM prefetch.")
        return False

    resolved_id = model_id or config.get_embedding_model()
    theme.info(
        f"Pre-downloading routing embedder: {resolved_id} (CPU-only)..."
    )
    try:
        TextEmbedding(resolved_id, cuda=Device.CPU, cache_dir=str(cache_path))
        theme.success(f"Embedded LLM cached ({resolved_id}).")
        return True
    except Exception as e:
        theme.error(f"Prefetch failed: {e}")
        theme.muted("Will be downloaded on first task instead.")
        # Capture the handled setup failure as an error log so it surfaces in the
        # LOGS tab / `modulatio logs` — the install-time pain the user can report.
        # Best-effort: a capture failure must never break setup.
        try:
            from modulatio import logstore

            logstore.write_error_log(
                f"setup: embedded-LLM prefetch failed ({resolved_id})",
                exc=e,
                context={"surface": "setup wizard — embedded LLM prefetch",
                         "model": resolved_id},
            )
        except Exception:  # noqa: BLE001 — capture is best-effort, never fatal
            pass
        return False


def run(state: dict) -> Any:
    """Execute the embedded LLM prefetch step. Mutates state with
    ``embedded_llm_cached`` flag. Silent if already cached for the
    active model id.

    The routing embedder is REQUIRED — skill-routing and qc-history
    retrieval don't work without it — so this step is NOT skippable:
    when the cache is cold it always attempts the one-time, CPU-only
    fetch. A genuine fetch failure (offline / fastembed missing) is
    surfaced loudly and logged, but doesn't block finishing setup —
    the first task (and ``modulatio doctor``) will retry the download.
    """
    active_model = config.get_embedding_model()
    if is_cached(active_model):
        theme.success(f"Embedded LLM ({active_model}) already cached.")
        state["embedded_llm_cached"] = True
        return "cached"

    print()
    theme.info(
        f"The routing embedder ({active_model}) is required — fetching now "
        f"(one-time, CPU-only). Modulatio can't route tasks without it."
    )
    success = prefetch(active_model)
    state["embedded_llm_cached"] = success
    state["embedded_llm_skipped"] = not success
    return "prefetched" if success else "failed"


__all__ = ["is_cached", "cache_dir", "prefetch", "run"]
