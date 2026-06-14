"""0.9.0 pre-ship re-sweep regression for src/modulatio/memory/team_memory.py.

Finding 1 [MEDIUM/correctness]: recall's semantic branch builds the LanceDB
table from the FULL record pool, runs a GLOBAL top-k*4 similarity search, then
applies the metadata filter in Python AFTER. When the pool is larger than that
slice and the metadata-matched entries rank below non-matching bodies, the
post-filter drops every valid precedent even though those matches clear
min_similarity. The fix sizes the search limit to keep the filtered hits
reachable: (pool - filtered) + top_k candidates (capped at pool, floored at the
original top_k*4).
"""

from __future__ import annotations

import math

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory


PROJECT_CODE = "RS"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "cache_root": str(tmp_path / "cache"),
    })
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


class _ControlledEmbedder:
    """Deterministic 2-D embedder that lets the test place each body at a chosen
    cosine angle to the query. Bodies are tagged with a token of the form
    ``a=<float>`` giving the angle (radians) from the query direction (1, 0).
    Non-matching ("noise") bodies sit at a *smaller* angle than the matching
    ones, so the global top-k*4 search ranks all of them ahead of the matches —
    exactly the condition that drops valid precedent under the old code.
    """

    dim = 2

    def _angle(self, text: str) -> float:
        for tok in text.split():
            if tok.startswith("a="):
                return float(tok[2:])
        return 0.0

    def _vec(self, text: str):
        a = self._angle(text)
        return [math.cos(a), math.sin(a)]

    def embed_text(self, text: str):
        return self._vec(text)

    def embed_texts(self, texts):
        return [self._vec(t) for t in texts]


def test_recall_semantic_keeps_metadata_match_outside_global_topk_slice():
    """The metadata-matched entries clear min_similarity but rank below a large
    block of non-matching bodies. Under the pre-fix global top_k*4 search they
    fall outside the candidate slice and recall wrongly returns nothing; the fix
    sizes the search to keep them reachable."""
    embedder = _ControlledEmbedder()
    top_k = 5

    # A wall of non-matching ("draft" kind) bodies that rank HIGHEST in global
    # similarity (tiny angle → cosine ~1.0). Far more than top_k*4 so the slice
    # is saturated by non-matches under the old code.
    noise = top_k * 4 + 10
    for i in range(noise):
        team_memory.write(
            writer_id="qc-1", writer_tier="qc",
            body=f"noise body {i} a=0.02",
            project_code=PROJECT_CODE, artifact_kind="draft",
        )

    # The metadata-matched ("report" kind) entries sit at a larger angle, so they
    # rank BELOW every noise body, but still well above min_similarity
    # (cos(0.3) ~= 0.955 > 0.5).
    for i in range(3):
        team_memory.write(
            writer_id="qc-1", writer_tier="qc",
            body=f"real precedent {i} a=0.30",
            project_code=PROJECT_CODE, artifact_kind="report",
        )

    hits = team_memory.recall(
        project_code=PROJECT_CODE,
        artifact_kind="report",
        task_description="query a=0.0",
        embedder=embedder,
        top_k=top_k,
        min_similarity=0.5,
    )

    # Pre-fix: 0 hits (every "report" ranked outside the top_k*4=20 noise slice).
    # Post-fix: all 3 metadata-matched precedents surface.
    kinds = {rec.artifact_kind for rec, _ in hits}
    assert kinds == {"report"}, f"non-matching kinds leaked or matches dropped: {kinds}"
    assert len(hits) == 3, f"expected all 3 metadata-matched precedents, got {len(hits)}"
    for _, sim in hits:
        assert sim is not None and sim >= 0.5


def test_recall_semantic_small_pool_unchanged():
    """Guard: the fix must not change behavior for small pools (the common
    case). A handful of matching entries still come back semantically scored."""
    embedder = _ControlledEmbedder()
    for i in range(3):
        team_memory.write(
            writer_id="qc-1", writer_tier="qc",
            body=f"report precedent {i} a=0.10",
            project_code=PROJECT_CODE, artifact_kind="report",
        )

    hits = team_memory.recall(
        project_code=PROJECT_CODE,
        artifact_kind="report",
        task_description="query a=0.0",
        embedder=embedder,
        top_k=5,
        min_similarity=0.5,
    )
    assert len(hits) == 3
    assert all(sim is not None for _, sim in hits)
