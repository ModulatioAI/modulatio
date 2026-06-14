"""LOW-audit regression tests for team_memory (findings #66, #67).

Uniquely-named file to avoid colliding with the agents concurrently editing
the shared team_memory test module.

#66 — _new_id() truncated microseconds to 10us resolution; two ids minted in
     the same window collided, and write() silently overwrote the earlier file.
#67 — recall()'s empty/missing LanceDB-table branches returned [] instead of
     the metadata recency fallback that the path-missing sibling branch uses,
     dropping real precedent from the markdown source of truth.
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory


PROJECT_CODE = "TMLOW"


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


# === #66 — id uniqueness ===

def test_new_id_unique_within_same_microsecond_window(monkeypatch):
    """Freeze the clock so every _new_id() call sees the SAME timestamp; the
    sequence suffix must still make every id distinct. Before the fix the
    frozen prefix was the whole id → all calls collided."""
    from datetime import datetime, timezone

    frozen = datetime(2026, 6, 13, 12, 0, 0, 123456, tzinfo=timezone.utc)

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            return frozen

    monkeypatch.setattr(team_memory, "datetime", _FrozenDateTime)

    ids = [team_memory._new_id() for _ in range(500)]
    assert len(set(ids)) == len(ids), "ids collided under a frozen clock"


def test_rapid_writes_do_not_overwrite_each_other():
    """Many back-to-back writes (same truncated-us window in practice) must all
    survive on disk — no silent overwrite from a colliding filename."""
    bodies = [f"Defect rule #{i}: guard the seam." for i in range(50)]
    written = [
        team_memory.write(
            writer_id="qc-1",
            writer_tier="qc",
            body=b,
            project_code=PROJECT_CODE,
            artifact_kind="report",
        )
        for b in bodies
    ]
    assert len({e.entry_id for e in written}) == len(written)

    loaded = team_memory.list_entries(PROJECT_CODE)
    assert len(loaded) == len(written), "a write was silently overwritten"
    assert {e.body for e in loaded} == set(bodies)


# === #67 — recall empty/missing-table recency fallback ===

class _FakeEmbedder:
    """Deterministic embedder so the semantic recall path runs without the
    real model."""

    dim = 8

    def _vec(self, text: str):
        h = abs(hash(text))
        return [float((h >> (i * 4)) & 0xF) for i in range(self.dim)]

    def embed_text(self, text: str):
        return self._vec(text)

    def embed_texts(self, texts):
        return [self._vec(t) for t in texts]


def test_recall_falls_back_to_recency_when_vector_table_empty(monkeypatch):
    """Markdown source of truth has matching entries but the LanceDB cache
    holds an EMPTY ``team_memory`` table. recall() must surface the markdown
    entries by recency (as the path-missing sibling branch already does)
    rather than returning [] — that was the #67 inconsistency.

    Builds a real, genuinely-empty table at the db path so the
    ``table.count_rows() == 0`` branch is exercised, then no-ops
    ``_ensure_vectors`` so it doesn't rebuild over it.
    """
    import lancedb
    import pyarrow as pa

    for i in range(3):
        team_memory.write(
            writer_id="qc-1",
            writer_tier="qc",
            body=f"Pattern {i}: mind the seam.",
            project_code=PROJECT_CODE,
            artifact_kind="report",
        )

    embedder = _FakeEmbedder()

    # Stand up an empty team_memory table at the cache path.
    db_path = team_memory._db_path(PROJECT_CODE)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(db_path)
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), embedder.dim)),
        pa.field("entry_id", pa.string()),
    ])
    db.create_table("team_memory", schema=schema)
    assert db.open_table("team_memory").count_rows() == 0

    # Don't let recall rebuild the (non-empty markdown) index over our empty
    # table — we want the empty-table branch specifically.
    monkeypatch.setattr(
        team_memory, "_ensure_vectors", lambda project_code, embedder: []
    )

    hits = team_memory.recall(
        project_code=PROJECT_CODE,
        artifact_kind="report",
        task_description="seam pattern",
        embedder=embedder,
        top_k=5,
    )

    assert hits, "empty-table branch dropped real markdown precedent (#67)"
    assert len(hits) == 3
    # Recency-sorted fallback hits are UNSCORED — score is the None sentinel,
    # not a fabricated 1.0 (r2 audit: a 1.0 would falsely claim a perfect
    # semantic match and bypass the min_similarity contract).
    assert all(score is None for _, score in hits)
    bodies = {rec.body for rec, _ in hits}
    assert bodies == {f"Pattern {i}: mind the seam." for i in range(3)}
