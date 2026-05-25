"""Tests for qc_history (slice #8.1).

QC precedent log: every verdict appends to
``<project_vault>/qc-history/<domain>.md`` (markdown, human-readable,
source of truth). A LanceDB cache under
``<config.get_cache_root()>/qc-history/<project>/<domain>/`` derives
embedded vectors from the markdown for top-K similarity retrieval.

Tests use ``StubEmbedder`` with deterministic vectors — no MiniLM
download. Cache root + embedding model are routed through ``config``
(audit Wave 2, F5), so tests monkeypatch the config getters
rather than module-level constants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import config, qc_history, vault
from modulatio.semantic_router import StubEmbedder


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    # cache root is now config-driven; override
    # the config getter so qc_history's internal _cache_root() resolves
    # to the test path. Same canonical pattern other modules use.
    monkeypatch.setattr(config, "get_cache_root", lambda: tmp_path / "cache")
    vault.init_project(PROJECT_CODE, "Test", "test")
    return PROJECT_CODE


def _record(
    entry_id: str = "v-1",
    verdict: str = "fail",
    defect_type: str | None = "substantive",
    rationale: str = "Missing required topic.",
    artifact_body: str = "This draft is about X, not Y.",
    task_id: str = "TST-T-001",
    timestamp: str = "2026-04-21T12:00:00Z",
) -> "qc_history.VerdictRecord":
    return qc_history.VerdictRecord(
        entry_id=entry_id,
        timestamp=timestamp,
        task_id=task_id,
        producer_agent="drafter",
        qc_agent="kimi",
        verdict=verdict,
        defect_type=defect_type,
        rationale=rationale,
        artifact_body=artifact_body,
    )


# ── append + load round-trip ───────────────────────────────────────────────

def test_append_and_load_round_trips_single_verdict(project):
    """Appending one verdict and loading it back yields an equal record.
    Markdown file is the source of truth — structure must survive the
    write → parse cycle."""
    rec = _record()
    qc_history.append_verdict("essay", project, rec)

    loaded = qc_history.load_verdicts("essay", project)
    assert len(loaded) == 1
    assert loaded[0] == rec


def test_load_verdicts_returns_empty_list_when_no_history(project):
    """No history file → empty list, not an exception. Retrieval on a
    fresh project must degrade cleanly to 'no precedent'."""
    assert qc_history.load_verdicts("essay", project) == []


def test_multiple_appends_preserve_order(project):
    """Verdicts accumulate append-order. History is chronological; the
    retrieval layer adds relevance ranking on top but the log itself is
    strictly append-only."""
    for i in range(3):
        qc_history.append_verdict(
            "essay",
            project,
            _record(entry_id=f"v-{i}", task_id=f"TST-T-{i:03d}"),
        )

    loaded = qc_history.load_verdicts("essay", project)
    assert [r.entry_id for r in loaded] == ["v-0", "v-1", "v-2"]


def test_domains_are_isolated_per_project(project):
    """Two domains on the same project write to separate history files.
    'essay' verdicts do not leak into 'code' history or vice versa."""
    qc_history.append_verdict("essay", project, _record(entry_id="essay-1"))
    qc_history.append_verdict("code", project, _record(entry_id="code-1"))

    assert [r.entry_id for r in qc_history.load_verdicts("essay", project)] == ["essay-1"]
    assert [r.entry_id for r in qc_history.load_verdicts("code", project)] == ["code-1"]


def test_append_preserves_multiline_artifact_body(project):
    """Artifact bodies contain newlines, frontmatter delimiters, and
    arbitrary punctuation. The log format must not corrupt them on
    write or on parse."""
    body = (
        "---\ntitle: Draft\n---\n\n"
        "Para 1 with 'quotes' and >>>markers<<<.\n\n"
        "Para 2 after blank line.\n"
    )
    qc_history.append_verdict("essay", project, _record(artifact_body=body))

    loaded = qc_history.load_verdicts("essay", project)
    assert loaded[0].artifact_body == body


def test_append_handles_null_defect_type_on_pass(project):
    """Passed verdicts have defect_type=None. Round-trip must preserve
    the distinction between 'None' (passed) and a concrete class."""
    qc_history.append_verdict(
        "essay", project, _record(verdict="pass", defect_type=None, rationale="ok"),
    )
    loaded = qc_history.load_verdicts("essay", project)
    assert loaded[0].verdict == "pass"
    assert loaded[0].defect_type is None


# ── similarity retrieval ───────────────────────────────────────────────────

def test_similar_verdicts_returns_empty_when_history_is_empty(project):
    """Empty domain → empty result list. Orchestrator can inject
    'no precedent' into the QC prompt without checking for None."""
    embedder = StubEmbedder(dim=16)
    hits = qc_history.similar_verdicts(
        "essay", project, artifact_body="anything", embedder=embedder, k=5,
    )
    assert hits == []


def test_similar_verdicts_returns_top_k_closest_records(project):
    """With deterministic overrides, the closest (highest cosine)
    records come first. k bounds the result size."""
    vectors = {
        "target": [1.0] + [0.0] * 15,
        "near": [0.99, 0.1] + [0.0] * 14,   # very close
        "mid": [0.5, 0.0, 0.87] + [0.0] * 13,
        "far": [0.0, 0.0, 0.0, 1.0] + [0.0] * 12,
    }
    # Unit-norm the overrides so cosine comparisons are well-defined.
    def unit(v: list[float]) -> list[float]:
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v]
    overrides = {k: unit(v) for k, v in vectors.items()}
    embedder = StubEmbedder(dim=16, overrides=overrides)

    for key in ("near", "mid", "far"):
        qc_history.append_verdict(
            "essay", project,
            _record(entry_id=key, task_id=f"TST-T-{key}", artifact_body=key),
        )

    hits = qc_history.similar_verdicts(
        "essay", project, artifact_body="target", embedder=embedder, k=2,
    )
    assert len(hits) == 2
    ids = [rec.entry_id for rec, _ in hits]
    assert ids == ["near", "mid"]
    # Similarity scores should be descending.
    assert hits[0][1] >= hits[1][1]


def test_similar_verdicts_rebuilds_index_when_new_entries_appended(project):
    """Appending new verdicts after a retrieval must be reflected on the
    next retrieval — the index's config hash includes entry count."""
    embedder = StubEmbedder(dim=16)
    qc_history.append_verdict("essay", project, _record(entry_id="first"))
    first_hits = qc_history.similar_verdicts(
        "essay", project, artifact_body="first", embedder=embedder, k=5,
    )
    assert {r.entry_id for r, _ in first_hits} == {"first"}

    qc_history.append_verdict("essay", project, _record(entry_id="second"))
    second_hits = qc_history.similar_verdicts(
        "essay", project, artifact_body="second", embedder=embedder, k=5,
    )
    assert {r.entry_id for r, _ in second_hits} == {"first", "second"}


def test_similar_verdicts_bounded_by_k(project):
    """More history than k → only k results. Verifies k is an actual cap,
    not a suggestion."""
    embedder = StubEmbedder(dim=16)
    for i in range(10):
        qc_history.append_verdict(
            "essay", project, _record(entry_id=f"v-{i}", artifact_body=f"body-{i}"),
        )
    hits = qc_history.similar_verdicts(
        "essay", project, artifact_body="query", embedder=embedder, k=3,
    )
    assert len(hits) == 3


def test_similar_verdicts_scoped_to_domain(project):
    """Retrieval for 'essay' must not return 'code' verdicts even when
    the artifact body is identical."""
    embedder = StubEmbedder(dim=16)
    qc_history.append_verdict(
        "essay", project, _record(entry_id="essay-v", artifact_body="shared body"),
    )
    qc_history.append_verdict(
        "code", project, _record(entry_id="code-v", artifact_body="shared body"),
    )
    hits = qc_history.similar_verdicts(
        "essay", project, artifact_body="shared body", embedder=embedder, k=5,
    )
    assert [r.entry_id for r, _ in hits] == ["essay-v"]


# ── config-routed cache root + embed model ───────


def test_cache_root_resolves_via_config(project, tmp_path, monkeypatch):
    """qc_history's cache root must come from config.get_cache_root(),
    not a module-level hardcode. The fixture already overrides
    config.get_cache_root to ``tmp_path / 'cache'``; assert that the
    LanceDB index actually lands under that root."""
    embedder = StubEmbedder(dim=16)
    qc_history.append_verdict(
        "essay", project, _record(entry_id="v-config", artifact_body="x"),
    )
    qc_history.similar_verdicts(
        "essay", project, artifact_body="x", embedder=embedder, k=1,
    )
    # The cache landed under the override root, not under ~/.cache.
    expected_root = tmp_path / "cache" / "qc-history"
    assert expected_root.exists(), (
        f"qc-history cache should live under {expected_root}; instead "
        f"the contents of tmp_path are: {list(tmp_path.iterdir())}"
    )
    # And under that, the project/domain dir.
    project_domain = expected_root / project.lower() / "essay"
    assert project_domain.exists()


def test_embedding_model_resolves_via_config(project, monkeypatch):
    """The persisted metadata's `embedding_model` field must reflect
    the live config value. Swapping config mid-test must trigger a
    rebuild on next access (the meta hash mismatches the new model
    fingerprint)."""
    embedder = StubEmbedder(dim=16)
    monkeypatch.setattr(
        config, "get_embedding_model", lambda: "test-org/embed-A"
    )
    qc_history.append_verdict(
        "essay", project, _record(entry_id="v-A", artifact_body="x"),
    )
    qc_history.similar_verdicts(
        "essay", project, artifact_body="x", embedder=embedder, k=1,
    )
    # Read back the persisted meta.json and confirm the model field.
    import json as _json
    meta_path = (
        config.get_cache_root() / "qc-history"
        / project.lower() / "essay" / "meta.json"
    )
    meta = _json.loads(meta_path.read_text())
    assert meta["embedding_model"] == "test-org/embed-A"

    # Swap config; trigger another retrieval; the meta must rewrite.
    monkeypatch.setattr(
        config, "get_embedding_model", lambda: "test-org/embed-B"
    )
    qc_history.similar_verdicts(
        "essay", project, artifact_body="x", embedder=embedder, k=1,
    )
    meta = _json.loads(meta_path.read_text())
    assert meta["embedding_model"] == "test-org/embed-B"


def test_no_module_level_constants(project):
    """the prior _CACHE_ROOT and _EMBED_MODEL
    module-level constants are gone. This test pins the contract so
    a future regression that re-introduces them is caught — module-
    level state would un-route from config and re-create the drift
    Wild Bill flagged."""
    assert not hasattr(qc_history, "_CACHE_ROOT"), (
        "qc_history._CACHE_ROOT must NOT exist; cache root is "
        "config-derived via the _cache_root() helper."
    )
    assert not hasattr(qc_history, "_EMBED_MODEL"), (
        "qc_history._EMBED_MODEL must NOT exist; embedding model is "
        "config-derived via the _embed_model() helper."
    )
