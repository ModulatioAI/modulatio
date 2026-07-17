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
    level state would un-route from config and re-create the drift."""
    assert not hasattr(qc_history, "_CACHE_ROOT"), (
        "qc_history._CACHE_ROOT must NOT exist; cache root is "
        "config-derived via the _cache_root() helper."
    )
    assert not hasattr(qc_history, "_EMBED_MODEL"), (
        "qc_history._EMBED_MODEL must NOT exist; embedding model is "
        "config-derived via the _embed_model() helper."
    )


# ── Concurrency — LanceDB rebuild/read race ───────────────────────────────────

def test_similar_verdicts_concurrent_is_thread_safe(project):
    """QC runs per-task on concurrent wave workers; each calls
    similar_verdicts -> _ensure_verdict_vectors which does a destructive
    drop_table+create_table rebuild then reads. Without the per-(project,domain)
    lock, two workers of the same domain race the rebuild (create_table raises
    'Table already exists') or one drops the table mid-search. Fire many
    barrier-synchronized calls and require every one to succeed with the same
    hit count. (Mirrors team_memory's test_recall_concurrent_semantic_path.)"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    embedder = StubEmbedder(dim=16)
    for i in range(6):
        qc_history.append_verdict(
            "essay", project,
            _record(entry_id=f"v-{i}", task_id=f"TST-T-{i:03d}",
                    artifact_body=f"draft number {i} about the seam"),
        )

    n = 12
    barrier = threading.Barrier(n)
    errors: list[Exception] = []
    counts: list[int] = []
    lock = threading.Lock()

    def _do():
        barrier.wait()  # release together to maximize the rebuild collision
        try:
            hits = qc_history.similar_verdicts(
                "essay", project, artifact_body="the seam draft",
                embedder=embedder, k=5,
            )
            with lock:
                counts.append(len(hits))
        except Exception as exc:  # noqa: BLE001 — the race surfaces as a raise
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as ex:
        for f in [ex.submit(_do) for _ in range(n)]:
            f.result(timeout=60)

    assert not errors, f"concurrent similar_verdicts raised: {errors[:3]}"
    assert len(counts) == n
    assert len(set(counts)) == 1, f"inconsistent results across threads: {set(counts)}"


# ═══ fold: test_qc_history_r2_audit.py ═══
# Round-2 audit regression tests for qc_history error-path resilience.
#
# Covers the MEDIUM finding: `_parse_verdict_file` did a bare `read_text()`
# with no encoding/errors arg and no guard, so a single non-UTF-8 / mangled
# history file raised UnicodeDecodeError (a ValueError) out of
# `load_verdicts`, bricking the whole domain's precedent — and through it the
# QC read path (`similar_verdicts` -> `_qc_review`) and the lessons
# codification loop that consume it.
#
# The docstring explicitly invites humans to hand-prune / drop in history
# files, so a mangled drop-in is plausible; one bad file must degrade to a
# best-effort parse, never crash the listing.
#
# Uniquely-named file to avoid colliding with a sibling agent editing
# tests/test_qc_history.py concurrently. Mirrors that file's fixtures.






def test_load_verdicts_does_not_crash_on_non_utf8_file(project):
    """A non-UTF-8 / binary .md in the domain dir must not raise
    UnicodeDecodeError out of load_verdicts. Before the fix the bare
    read_text() raised and aborted the whole listing."""
    qc_history.append_verdict("essay", project, _record("good-1"))

    domain_dir = qc_history._domain_dir("essay", project)
    # A file whose bytes are not valid UTF-8 (truncated multibyte / binary
    # blob saved with a .md name). Use a sortable-later name so the good
    # record still loads regardless of iteration order.
    (domain_dir / "zzz__binary.md").write_bytes(b"\xff\xfe binary garbage \x80\x81")

    # Must not raise.
    loaded = qc_history.load_verdicts("essay", project)

    # The valid record is still returned; the bad file degraded to a
    # best-effort parse (errors="replace") rather than bricking the listing.
    assert "good-1" in [r.entry_id for r in loaded]


def test_load_verdicts_skips_file_that_vanishes_mid_read(project, monkeypatch):
    """A transient OSError (file vanished between glob and read, permission
    hiccup) on one file must be skipped, not propagated — the rest of the
    domain's precedent must still load."""
    qc_history.append_verdict("essay", project, _record("keep-1"))
    qc_history.append_verdict("essay", project, _record("keep-2"))

    real_parse = qc_history._parse_verdict_file
    calls = {"n": 0}

    def flaky_parse(path: Path):
        calls["n"] += 1
        # Fail the first file with an OSError; parse the rest normally.
        if calls["n"] == 1:
            raise FileNotFoundError(path)
        return real_parse(path)

    monkeypatch.setattr(qc_history, "_parse_verdict_file", flaky_parse)

    loaded = qc_history.load_verdicts("essay", project)
    # One file was dropped; the other still loaded — no crash.
    assert len(loaded) == 1
    assert loaded[0].entry_id in {"keep-1", "keep-2"}


def _record_with(entry_id: str, timestamp: str) -> "qc_history.VerdictRecord":
    rec = _record(entry_id)
    return qc_history.VerdictRecord(
        entry_id=rec.entry_id,
        timestamp=timestamp,
        task_id=rec.task_id,
        producer_agent=rec.producer_agent,
        qc_agent=rec.qc_agent,
        verdict=rec.verdict,
        defect_type=rec.defect_type,
        rationale=rec.rationale,
        artifact_body=rec.artifact_body,
    )


def test_same_second_verdicts_load_in_append_order(project):
    """LOW finding: timestamps are stamped at second resolution, so verdicts
    appended within the same second share a filename prefix. The entry_id tail
    is a random uuid, which sorts arbitrarily — not in append order. The
    monotonic per-append sequence wedged into the filename must make
    load_verdicts (sorted glob) reflect true append order within a second.

    The entry_ids here are deliberately chosen so that a plain
    ``{timestamp}__{entry_id}`` sort would invert append order: the first
    appended record has the lexicographically-largest tail.
    """
    ts = "2026-04-21T12:00:00Z"  # identical second for all three
    append_order = ["zzz-first", "mmm-second", "aaa-third"]
    for eid in append_order:
        qc_history.append_verdict("essay", project, _record_with(eid, ts))

    loaded = qc_history.load_verdicts("essay", project)
    assert [r.entry_id for r in loaded] == append_order


def test_verdict_filename_has_sortable_seq_tiebreaker(project):
    """The monotonic sequence segment must be present and strictly increasing
    across appends so filenames sort by append order, independent of the
    entry_id tail."""
    ts = "2026-04-21T12:00:00Z"
    qc_history.append_verdict("essay", project, _record_with("zzz", ts))
    qc_history.append_verdict("essay", project, _record_with("aaa", ts))

    names = sorted(p.name for p in qc_history._domain_dir("essay", project).glob("*.md"))
    # Filenames sort with the zzz-tailed (first appended) file first because
    # its seq segment is smaller, despite "zzz" > "aaa" lexically.
    assert names[0].endswith("zzz.md")
    assert names[1].endswith("aaa.md")
