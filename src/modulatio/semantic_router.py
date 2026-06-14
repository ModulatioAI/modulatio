# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Semantic routing fallback — slice #6e.

Decoupled from any memory/knowledge stack per v1's lesson
(``SPEC-routing-embedder.md``): the routing embedder is its own thing,
not shared with crew memory, not shared with agent memory, not shared
with anything else. Build it small, keep it small.

Stack:
- **MiniLM** (``all-MiniLM-L6-v2``, 384-dim, ~80MB) via fastembed
  (ONNX, CPU-only).
- **LanceDB** for the agent-skill index, one table per project.

The index is a derived cache. Delete ``~/.cache/modulatio/semantic/<code>/``
and the next ``ensure_agent_vectors`` call rebuilds from vault content.
Filesystem is source of truth; the vector store is a cache with a hash
gate so we don't re-embed on every orchestrator kickoff.

Abstract ``Embedder`` protocol lets tests pass a deterministic stub
without downloading MiniLM — the real ``FastEmbedder`` concrete impl is
behind the same surface.

Scope discipline (locked in ``quality-architecture.md`` §"Slice #6"):
this module is routing ONLY. No unified crew memory, no agent episodic
memory, no auto-indexing on writes, no auto-promotion. Other embedding
uses (qc-history similarity, research cache lookup, standards dedup)
are separate slices with their own dedicated indexes.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable, Optional, Protocol

from modulatio import roster
from modulatio.roster import Agent
from modulatio.types import Task

from modulatio import config as _config

def _cache_root() -> Path:
    """Resolve the semantic-routing cache root from current config.
    Honors the wizard's ``cache_root`` override and ``$XDG_CACHE_HOME``;
    falls back to the package default. Per-call resolution means tests +
    runtime config changes both work without restart (mirrors
    ``qc_history._cache_root``)."""
    return _config.get_cache_root() / "semantic"


# Single source of truth lives in config.get_embedding_model() so
# wizards / overrides land in one place. Computed at import is fine —
# the value is stable for the process lifetime.
_EMBED_MODEL = _config.get_embedding_model()

# Fallback dimension for the default MiniLM model — used only when
# fastembed isn't importable or the model isn't in its catalog (e.g. a
# wizard-set custom model fastembed doesn't recognize). The real value
# is derived from the model itself in ``_embed_dim_for_model`` so the
# LanceDB schema width always matches the vectors the embedder emits.
_DEFAULT_EMBED_DIM = 384


def _embed_dim_for_model(model_name: str) -> int:
    """Resolve the output vector length for ``model_name`` from
    fastembed's catalog. The wizard can override the embedding model to
    any supported model (e.g. a 768-dim bge model); the static 384 was a
    silent mismatch that produced a LanceDB schema narrower/wider than
    the actual vectors. Falls back to the MiniLM default if fastembed is
    unavailable or the model is unknown."""
    try:
        from fastembed import TextEmbedding

        for m in TextEmbedding.list_supported_models():
            if m.get("model") == model_name:
                dim = m.get("dim")
                if isinstance(dim, int) and dim > 0:
                    return dim
                break
    except Exception:
        pass
    return _DEFAULT_EMBED_DIM


_EMBED_DIM = _embed_dim_for_model(_EMBED_MODEL)


# ── Embedder protocol + impls ──────────────────────────────────────────────

class Embedder(Protocol):
    """Abstract embedder. ``dim`` is the output vector length; stays
    stable across calls so the LanceDB schema can be fixed at table
    creation time.
    """

    dim: int

    def embed_text(self, text: str) -> list[float]: ...

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """Production embedder — lazy-loads MiniLM the first time it's
    asked to embed something. Keeps CLI startup fast for runs that
    don't route semantically (stub smoke tests, deterministic-match
    paths).

    **CPU-pinned.** MiniLM is 80MB and 384-dim; it has no business on
    a GPU. Modulatio's deployment targets routinely include GPU-rich
    hosts where the GPUs are fully dedicated to the real LLMs — routing
    cycles must not compete for VRAM. ``fastembed``'s default
    ``Device.AUTO`` picks CUDA when available; we override to
    ``Device.CPU`` explicitly (same pattern v1 used in
    ``SPEC-routing-embedder.md``).
    """

    dim = _EMBED_DIM

    def __init__(self) -> None:
        self._model = None
        # A single FastEmbedder is shared across all vector subsystems
        # (routing matcher, qc_history, team_memory) and touched
        # concurrently by wave workers in a ThreadPoolExecutor. Two
        # locks: ``_load_lock`` makes the lazy model load happen exactly
        # once (double-checked), and ``_infer_lock`` serializes ONNX
        # inference so concurrent embed calls don't race the same
        # underlying session.
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    def _get(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from fastembed import TextEmbedding
                    from fastembed.common.types import Device
                    self._model = TextEmbedding(_EMBED_MODEL, cuda=Device.CPU)
        return self._model

    def embed_text(self, text: str) -> list[float]:
        model = self._get()
        with self._infer_lock:
            return next(model.embed([text])).tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        model = self._get()
        with self._infer_lock:
            return [v.tolist() for v in model.embed(texts)]


class StubEmbedder:
    """Deterministic stub for tests — no model download, no ONNX.

    Default behavior: hash each text into a stable pseudo-random
    unit-norm vector. Same text → same vector across calls. Different
    texts → approximately orthogonal vectors (won't preserve true
    semantic similarity).

    For tests that need controlled similarity (e.g. "task A should
    match agent X with cosine 0.9"), pass an ``overrides`` dict mapping
    text → explicit vector. Any text not in ``overrides`` falls back
    to the hash-based default.
    """

    def __init__(
        self,
        dim: int = _EMBED_DIM,
        overrides: dict[str, list[float]] | None = None,
    ) -> None:
        self.dim = dim
        self._overrides = dict(overrides or {})

    def set(self, text: str, vector: list[float]) -> None:
        """Register an explicit vector for a given text."""
        self._overrides[text] = vector

    def _hash_vector(self, text: str) -> list[float]:
        # Deterministic pseudo-random vector from SHA-256, unit-normed
        # so cosine similarity behaves sensibly.
        import random
        rng = random.Random(hashlib.sha256(text.encode()).digest())
        v = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = sum(x * x for x in v) ** 0.5
        if norm == 0:
            return v
        return [x / norm for x in v]

    def embed_text(self, text: str) -> list[float]:
        if text in self._overrides:
            return list(self._overrides[text])
        return self._hash_vector(text)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(t) for t in texts]


# ── Index (LanceDB) ────────────────────────────────────────────────────────

def _cache_dir(project_code: str) -> Path:
    return _cache_root() / project_code.lower()


def _meta_path(project_code: str) -> Path:
    return _cache_dir(project_code) / "meta.json"


def _db_path(project_code: str) -> Path:
    return _cache_dir(project_code) / "lance.db"


def _agent_capability_text(agent: Agent) -> str:
    """Compose what we embed per agent. Mixes structured fields
    (skills, tags) with the identity prose so semantic match picks up
    both literal skill-name hits and broader capability."""
    parts = [agent.name, "Skills: " + ", ".join(agent.skills)]
    if agent.capability_tags:
        parts.append("Tags: " + ", ".join(agent.capability_tags))
    body = agent.identity.strip()
    if body:
        parts.append(body)
    return "\n".join(parts)


def _task_query_text(task: Task) -> str:
    parts: list[str] = []
    if task.required_skills:
        parts.append("Required skills: " + ", ".join(task.required_skills))
    parts.append(task.description)
    return "\n".join(parts)


def _config_hash(agents: list[Agent]) -> str:
    """Hash that invalidates the index when agent capabilities change.
    Includes the embedding model name — switching models triggers a
    rebuild (vector-dim changes would otherwise leave stale rows)."""
    parts = []
    for a in sorted(agents, key=lambda x: x.id):
        parts.append(
            f"{a.id}|{a.name}|"
            f"{','.join(sorted(a.skills))}|"
            f"{','.join(sorted(a.capability_tags))}|"
            f"{a.identity.strip()}"
        )
    payload = "||".join(parts) + f"|model={_EMBED_MODEL}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _load_meta(project_code: str) -> dict:
    path = _meta_path(project_code)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _save_meta(project_code: str, meta: dict) -> None:
    _cache_dir(project_code).mkdir(parents=True, exist_ok=True)
    _meta_path(project_code).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def ensure_agent_vectors(project_code: str, embedder: Embedder) -> bool:
    """Build/refresh the project's agent-skill vector index.

    Returns ``True`` if the index was (re)built, ``False`` if the cached
    index was still current. The cache gate compares a config hash of
    the roster (agents' skills/tags/identity) plus the embedding model
    name — change either and we rebuild.

    Empty roster writes the hash but skips table creation. Safe to call
    before an agent ever exists.
    """
    import lancedb
    import pyarrow as pa

    agents = roster.list_agents(project_code)
    new_hash = _config_hash(agents)
    meta = _load_meta(project_code)

    if (
        meta.get("agents_hash") == new_hash
        and meta.get("embedding_model") == _EMBED_MODEL
        and meta.get("embed_dim") == embedder.dim
    ):
        return False

    _cache_dir(project_code).mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(_db_path(project_code))

    if "agent_skills" in db.list_tables().tables:
        db.drop_table("agent_skills")

    if not agents:
        _save_meta(project_code, {
            "agents_hash": new_hash,
            "embedding_model": _EMBED_MODEL,
            "embed_dim": embedder.dim,
            "agent_count": 0,
        })
        return True

    texts = [_agent_capability_text(a) for a in agents]
    vectors = embedder.embed_texts(texts)

    rows = [
        {"vector": v, "agent_id": a.id, "skill_text": t}
        for a, v, t in zip(agents, vectors, texts)
    ]

    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), embedder.dim)),
        pa.field("agent_id", pa.string()),
        pa.field("skill_text", pa.string()),
    ])
    db.create_table("agent_skills", data=rows, schema=schema)

    _save_meta(project_code, {
        "agents_hash": new_hash,
        "embedding_model": _EMBED_MODEL,
        "embed_dim": embedder.dim,
        "agent_count": len(agents),
    })
    return True


def semantic_match(
    task: Task,
    project_code: str,
    embedder: Embedder,
    threshold: float = 0.5,
) -> tuple[Agent, float] | None:
    """Find the best agent for ``task`` by cosine similarity, or ``None``
    if the top hit falls below ``threshold``.

    Returns ``(agent, similarity)`` where similarity is in [0, 1] (1.0
    is identical). Threshold defaults to 0.5 pending live calibration
    against MiniLM's actual score distribution — this is the knob to
    tune once real traffic exercises the router.

    Assumes ``ensure_agent_vectors`` has been called; if the index is
    missing or empty, returns ``None`` so the orchestrator can fall
    through to ROSTER_GAP handling.
    """
    import lancedb

    path = _db_path(project_code)
    if not path.exists():
        return None

    db = lancedb.connect(path)
    if "agent_skills" not in db.list_tables().tables:
        return None

    table = db.open_table("agent_skills")
    if table.count_rows() == 0:
        return None

    query_vec = embedder.embed_text(_task_query_text(task))
    results = (
        table.search(query_vec)
        .metric("cosine")
        .limit(1)
        .to_list()
    )
    if not results:
        return None

    top = results[0]
    # LanceDB cosine distance = 1 - cosine_similarity.
    distance = float(top.get("_distance", 1.0))
    similarity = 1.0 - distance
    if similarity < threshold:
        return None

    agent = roster.load(top["agent_id"], project_code)
    if agent is None:
        # Index referenced an agent no longer in the roster — treat as miss.
        return None
    return (agent, similarity)


def default_matcher(
    project_code: str,
    embedder: Embedder | None = None,
    threshold: float = 0.5,
) -> Callable[[Task], Optional[tuple[Agent, float]]]:
    """Return a closure the orchestrator calls per-task to do semantic
    matching. First invocation builds/refreshes the LanceDB index via
    ``ensure_agent_vectors`` (hash-gated, so subsequent calls are
    near-free when the roster hasn't changed); the closure then queries
    the index via ``semantic_match``.

    ``embedder`` defaults to ``FastEmbedder()`` for production. Tests
    that want a fake matcher should NOT call this — they pass a plain
    callable with the same ``Callable[[Task], tuple[Agent, float] | None]``
    signature directly to the orchestrator, skipping the index entirely.
    """
    if embedder is None:
        embedder = FastEmbedder()

    def matcher(task: Task) -> Optional[tuple[Agent, float]]:
        ensure_agent_vectors(project_code, embedder)
        return semantic_match(task, project_code, embedder, threshold)

    return matcher


__all__ = [
    "Embedder",
    "FastEmbedder",
    "StubEmbedder",
    "default_matcher",
    "ensure_agent_vectors",
    "semantic_match",
]
