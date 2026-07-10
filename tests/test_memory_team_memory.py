"""Round-2 full-debug audit regressions for team_memory (3 LOW findings).

Each test fails against the pre-fix code and passes after:

  1. error-path: _parse() catches OSError but NOT UnicodeDecodeError, so a
     binary / non-UTF-8 .md bricks list_entries()/recall() for every valid
     entry. UnicodeDecodeError is a ValueError subclass, not an OSError.
  2. correctness: the embedding model was captured at import (_EMBED_MODEL),
     so a wizard model-swap mid-process never changed the LanceDB metadata
     fingerprint → no rebuild → stale vectors. Resolve per-call instead.
  3. correctness: the recency fallback labelled unscored entries sim 1.00,
     which bypasses min_similarity and misrepresents non-semantic precedent
     to the producer. Use a None sentinel rendered as "recency".
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory
import math


PROJECT_CODE = "R2"


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


# === Finding 1: binary / non-UTF-8 entry must not brick the listing ===

def test_parse_skips_non_utf8_entry_instead_of_crashing():
    # A valid entry plus a corrupt (binary / non-UTF-8) .md in the same dir.
    good = team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="valid precedent body",
        project_code=PROJECT_CODE, artifact_kind="report",
    )
    team_dir = team_memory._team_dir(PROJECT_CODE)
    (team_dir / "20260101__corrupt.md").write_bytes(b"\xff\xfe binary garbage \x00")

    # Pre-fix: UnicodeDecodeError propagates out of _parse → list_entries dies.
    entries = team_memory.list_entries(PROJECT_CODE)
    ids = {e.entry_id for e in entries}
    assert good.entry_id in ids  # valid entry still surfaced
    # The corrupt file is quarantined (skipped), not crashing the listing.


def test_parse_returns_none_on_undecodable_file():
    team_dir = team_memory._team_dir(PROJECT_CODE)
    team_dir.mkdir(parents=True, exist_ok=True)
    bad = team_dir / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00\x80\x81")
    assert team_memory._parse(bad) is None


# === Finding 2: embedding model resolved per-call, not captured at import ===

def test_embed_model_resolves_current_config(monkeypatch):
    # The module must NOT serve a value frozen at import. Swap the config
    # value at runtime and confirm _embed_model() reflects it.
    monkeypatch.setattr(config, "get_embedding_model", lambda: "swapped-model-x")
    assert team_memory._embed_model() == "swapped-model-x"


def test_config_hash_tracks_live_model_swap(monkeypatch):
    rec = team_memory.write(
        writer_id="qc-1", writer_tier="qc", body="b",
        project_code=PROJECT_CODE, artifact_kind="report",
    )
    records = team_memory.list_entries(PROJECT_CODE)
    assert records  # sanity

    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-a")
    hash_a = team_memory._config_hash(records)
    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-b")
    hash_b = team_memory._config_hash(records)
    # Pre-fix the hash was pinned to the import-time _EMBED_MODEL and never
    # changed; the rebuild gate would therefore miss a model swap.
    assert hash_a != hash_b
    _ = rec


# === Finding 3: recency fallback must not claim sim 1.00 ===

def _seed_one():
    team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="precedent body text",
        project_code=PROJECT_CODE, artifact_kind="report",
    )


def test_recency_fallback_score_is_none_not_one():
    _seed_one()
    # No embedder / no task_description → recency fallback path.
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="report")
    assert hits
    rec, sim = hits[0]
    assert sim is None  # unscored sentinel, not a fabricated 1.0


def test_render_for_prompt_labels_recency_not_sim_one():
    _seed_one()
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="report")
    rendered = team_memory.render_for_prompt(hits)
    assert "| recency |" in rendered
    assert "sim 1.00" not in rendered


def test_render_for_prompt_still_shows_numeric_sim_for_scored_hits():
    # A genuinely-scored hit (float) must still render as "sim X.XX".
    team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="scored precedent body",
        project_code=PROJECT_CODE, artifact_kind="report",
    )
    entry = team_memory.list_entries(PROJECT_CODE)[0]
    rendered = team_memory.render_for_prompt([(entry, 0.83)])
    assert "sim 0.83" in rendered
    # The score field must read "sim ...", never the recency sentinel.
    assert "| recency |" not in rendered


# ═══ fold: test_memory_team_memory_low_audit.py ═══
# LOW-audit regression tests for team_memory (findings #66, #67).
#
# Uniquely-named file to avoid colliding with the agents concurrently editing
# the shared team_memory test module.
#
# #66 — _new_id() truncated microseconds to 10us resolution; two ids minted in
#      the same window collided, and write() silently overwrote the earlier file.
# #67 — recall()'s empty/missing LanceDB-table branches returned [] instead of
#      the metadata recency fallback that the path-missing sibling branch uses,
#      dropping real precedent from the markdown source of truth.






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


# ═══ fold: test_memory_team_memory_resweep.py ═══
# 0.9.0 pre-ship re-sweep regression for src/modulatio/memory/team_memory.py.
#
# Finding 1 [MEDIUM/correctness]: recall's semantic branch builds the LanceDB
# table from the FULL record pool, runs a GLOBAL top-k*4 similarity search, then
# applies the metadata filter in Python AFTER. When the pool is larger than that
# slice and the metadata-matched entries rank below non-matching bodies, the
# post-filter drops every valid precedent even though those matches clear
# min_similarity. The fix sizes the search limit to keep the filtered hits
# reachable: (pool - filtered) + top_k candidates (capped at pool, floored at the
# original top_k*4).






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


# ═══ fold: test_memory_team_memory_resweep_r3.py ═══
# 0.9.0 pre-ship re-sweep (round 3) regression for team_memory.py.
#
# Finding 1 [LOW/security]: approve_proposal/reject_proposal located the proposal
# file via a substring glob ``dir_.glob(f"*{proposal_id}*.json")`` over a
# caller/operator-supplied ``proposal_id``. A glob metacharacter (``*``/``?``/
# ``[``) made the pattern match ALL/arbitrary proposals (acting on a
# non-deterministic ``matching[0]``), and a bare substring matched an unintended
# proposal whose id merely CONTAINS the supplied one. The fix matches on the
# canonical ``proposal_id`` embedded in the JSON for exact equality.






def _stage(body: str) -> str:
    p = team_memory.propose(
        proposer_id="alice",
        body=body,
        project_code=PROJECT_CODE,
    )
    return p.proposal_id


def test_reject_glob_metachar_does_not_match_other_proposals():
    # Two unrelated proposals staged.
    keep_id = _stage("keep me")
    other_id = _stage("also keep me")
    assert keep_id != other_id

    # A wildcard id must NOT be treated as a glob and delete an arbitrary file.
    assert team_memory.reject_proposal("*", project_code=PROJECT_CODE) is False

    remaining = {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)}
    assert remaining == {keep_id, other_id}


def test_reject_substring_of_real_id_does_not_match():
    real_id = _stage("real proposal")
    # A strict substring of a genuine id must not resolve to that proposal.
    substring = real_id[:-3]
    assert substring != real_id
    assert team_memory.reject_proposal(substring, project_code=PROJECT_CODE) is False
    assert {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)} == {real_id}


def test_reject_exact_id_still_works():
    target = _stage("delete this one")
    keep = _stage("keep this one")
    assert team_memory.reject_proposal(target, project_code=PROJECT_CODE) is True
    assert {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)} == {keep}


def test_approve_glob_metachar_raises_not_found():
    _stage("body a")
    _stage("body b")
    with pytest.raises(KeyError):
        team_memory.approve_proposal(
            "?",
            project_code=PROJECT_CODE,
            approver_id="qc1",
            approver_tier="qc",
        )
    # Nothing consumed.
    assert len(team_memory.list_proposals(PROJECT_CODE)) == 2


def test_approve_exact_id_consumes_only_target():
    target = _stage("approve me")
    keep = _stage("not me")
    entry = team_memory.approve_proposal(
        target,
        project_code=PROJECT_CODE,
        approver_id="qc1",
        approver_tier="qc",
    )
    assert entry.body == "approve me"
    assert {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)} == {keep}
