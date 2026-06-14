"""Tests for the #81 recovery feed — the win-codification source.

A QC recovery (the smart QC rescues a cheap producer by writing the patch it
couldn't) is witnessed here as a RecoveryRecord. The win loop clusters these by a
deterministic, false-merge-resistant signature behind an engine recurrence floor.
"""

from __future__ import annotations

from modulatio import recoveries


# ── change_shape — the artifact-kind-aware diff fingerprint (Hero R3) ──────────


def test_change_shape_code_is_deterministic():
    before = "def f(x):\n    return x + 1\n"
    after = "def f(x):\n    if x is None:\n        return 0\n    return x + 1\n"
    a = recoveries.change_shape(before, after, "python_code")
    b = recoveries.change_shape(before, after, "python_code")
    assert a is not None and a == b and a.startswith("code:")


def test_change_shape_unknown_kind_returns_none():
    """media / an unclassifiable kind → None, so record_recovery substitutes a
    unique sentinel (a permanent singleton that can never false-merge)."""
    assert recoveries.change_shape(b"\x00", b"\x01", "video") is None
    assert recoveries.change_shape("x", "y", "some-exotic-kind") is None


def test_change_shape_false_merge_guard():
    """Hero R3 regression: three GENUINELY different code fixes must NOT share a
    fingerprint (else the false-split bias inverts to dangerous false-merge)."""
    # 1. added guard — control-flow added, nothing removed
    guard_b = "def f(x):\n    return x + 1\n"
    guard_a = "def f(x):\n    if x is None:\n        return 0\n    return x + 1\n"
    # 2. large refactor — many lines added
    refac_b = "def g():\n    return 1\n"
    refac_a = (
        "def g():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n"
        "    e = 5\n    f = 6\n    return a + b + c + d + e + f\n"
    )
    # 3. literal tweak — one line swapped, no control flow
    lit_b = "x = '5'\n"
    lit_a = "x = 5\n"
    s1 = recoveries.change_shape(guard_b, guard_a, "python_code")
    s2 = recoveries.change_shape(refac_b, refac_a, "python_code")
    s3 = recoveries.change_shape(lit_b, lit_a, "python_code")
    assert s1 != s2 and s1 != s3 and s2 != s3


def test_change_shape_same_shape_matches():
    """Two different-but-same-shape guard additions share a fingerprint (so they
    CAN cluster when their rationale-keys also agree)."""
    a = recoveries.change_shape(
        "def f(x):\n    return x\n",
        "def f(x):\n    if x is None:\n        return 0\n    return x\n",
        "python_code",
    )
    b = recoveries.change_shape(
        "def h(y):\n    return y\n",
        "def h(y):\n    if y is None:\n        return 0\n    return y\n",
        "python_code",
    )
    assert a is not None and a == b


def test_change_shape_document_kind():
    a = recoveries.change_shape("One sentence.", "One sentence. Two sentences.", "essay")
    assert a is not None and a.startswith("doc:")


# ── record_recovery — witness with write-time truncation (Nemo #6) ─────────────


def test_record_recovery_writes_and_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path)
    rec = recoveries.record_recovery(
        "P", kind="qc_authored", artifact_kind="python_code",
        defect_type="substantive", task_id="T-1",
        defects="missing null guard", before="def f(x): return x",
        after="def f(x):\n    if x is None: return 0\n    return x",
        qc_rationale="null input crashes",
    )
    assert rec.kind == "qc_authored" and rec.task_id == "T-1"
    assert rec.signature and rec.signature.count(":") >= 1
    loaded = recoveries.load_recoveries("P")
    assert len(loaded) == 1 and loaded[0].entry_id == rec.entry_id


def test_record_recovery_truncates_every_text_field(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path)
    cap = recoveries.MAX_RECOVERY_EXCERPT_CHARS
    big = "x" * (cap * 3)
    rec = recoveries.record_recovery(
        "P", kind="qc_authored", artifact_kind="python_code", defect_type="substantive",
        task_id="T", defects=big, before=big, after=big, qc_rationale=big,
    )
    assert len(rec.defects) <= cap
    assert len(rec.before_excerpt) <= cap
    assert len(rec.after_excerpt) <= cap
    assert len(rec.qc_rationale) <= cap
    # and the on-disk record is bounded (≈4 capped text fields + signature + JSON
    # overhead) — far below the untruncated 4×(3·cap) input it was fed.
    log = recoveries._log_path("P")
    assert log.stat().st_size <= 8 * cap
    assert log.stat().st_size < 4 * len(big)  # truncation provably happened


def test_record_recovery_unknown_kind_gets_unique_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path)
    r1 = recoveries.record_recovery(
        "P", kind="qc_authored", artifact_kind="video", defect_type="substantive",
        task_id="T1", defects="d", before="a", after="b", qc_rationale="same words here",
    )
    r2 = recoveries.record_recovery(
        "P", kind="qc_authored", artifact_kind="video", defect_type="substantive",
        task_id="T2", defects="d", before="a", after="b", qc_rationale="same words here",
    )
    # identical rationale + kind, but the unclassified sentinel embeds the id →
    # the two signatures DIFFER → they can never cluster.
    assert r1.signature != r2.signature
    assert "unclassified:" in r1.signature and "unclassified:" in r2.signature


# ── ledger: unconsumed_recoveries + the SEPARATE consumed file (Nemo #2) ───────


def _rec(monkeypatch, tmp_path, project, **kw):
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    base = dict(kind="qc_authored", artifact_kind="python_code", defect_type="substantive",
                task_id="T", defects="d", before="a", after="b", qc_rationale="r")
    base.update(kw)
    return recoveries.record_recovery(project, **base)


def test_unconsumed_excludes_consumed_and_caps(tmp_path, monkeypatch):
    ids = [_rec(monkeypatch, tmp_path, "P", task_id=f"T{i}").entry_id for i in range(5)]
    recoveries.mark_consumed("P", ids[:2])
    out = recoveries.unconsumed_recoveries("P")
    got = {r.entry_id for r in out}
    assert ids[0] not in got and ids[1] not in got
    assert len(got) == 3
    # capped
    assert len(recoveries.unconsumed_recoveries("P", limit=1)) == 1


def test_consumed_ledger_is_separate_from_lessons(tmp_path, monkeypatch):
    """Nemo #2: recovery ids land in the recoveries consumed ledger, NEVER in the
    lessons fail ledger."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    recoveries.mark_consumed("P", ["rec-1"])
    assert "rec-1" in recoveries.consumed_ids("P")
    lessons_consumed = tmp_path / "P" / "lessons" / "_consumed"
    assert not lessons_consumed.exists()  # the fail ledger is untouched
    rec_consumed = tmp_path / "P" / "recoveries" / "_consumed_recoveries"
    assert rec_consumed.exists()


# ── cluster_recoveries — the engine recurrence floor ──────────────────────────


def test_cluster_floor_and_false_merge(tmp_path, monkeypatch):
    """Below floor → no cluster; same-signature ≥floor → one cluster; three
    different change-shapes sharing a rationale-key → NO cluster (false-merge guard)."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    # 3 same-shape guard additions, same rationale → one cluster of 3
    same = [
        _rec(monkeypatch, tmp_path, "P", task_id=f"S{i}",
             before="def f(x):\n    return x\n",
             after="def f(x):\n    if x is None:\n        return 0\n    return x\n",
             qc_rationale="missing null guard on input")
        for i in range(3)
    ]
    clusters = recoveries.cluster_recoveries(same, floor=3)
    assert len(clusters) == 1 and len(clusters[0]) == 3

    # below floor → nothing surfaces
    assert recoveries.cluster_recoveries(same[:2], floor=3) == []

    # three DIFFERENT shapes, same rationale-key → no ≥floor cluster
    diff = [
        _rec(monkeypatch, tmp_path, "Q", task_id="D1",
             before="def f(x):\n    return x\n",
             after="def f(x):\n    if x is None:\n        return 0\n    return x\n",
             qc_rationale="missing edge case handling here"),
        _rec(monkeypatch, tmp_path, "Q", task_id="D2",
             before="def g():\n    return 1\n",
             after="def g():\n    a=1\n    b=2\n    c=3\n    d=4\n    e=5\n    return a+b+c+d+e\n",
             qc_rationale="missing edge case handling here"),
        _rec(monkeypatch, tmp_path, "Q", task_id="D3",
             before="x = '5'\n", after="x = 5\n",
             qc_rationale="missing edge case handling here"),
    ]
    assert recoveries.cluster_recoveries(diff, floor=3) == []


def test_signature_rationale_key_uses_truncated_rationale(tmp_path, monkeypatch):
    """Nemo code #2: the signature's rationale-key must derive from the TRUNCATED
    rationale — a meaningful token sitting past the cap must NOT leak into the key."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path)
    cap = recoveries.MAX_RECOVERY_EXCERPT_CHARS
    rationale = ("the " * cap) + "SENTINELPASTCAP"  # first real token is past the cap
    rec = recoveries.record_recovery(
        "P", kind="qc_authored", artifact_kind="python_code", defect_type="substantive",
        task_id="T", defects="d", before="a", after="b", qc_rationale=rationale,
    )
    assert "sentinelpastcap" not in rec.signature.lower()
    assert "sentinelpastcap" not in rec.qc_rationale.lower()


def test_load_recoveries_fails_open_on_non_utf8_log(tmp_path, monkeypatch):
    """Pre-ship LOW: a non-UTF-8 recovery log (truncated multibyte write / foreign
    locale) must fail OPEN to [] — UnicodeDecodeError is a ValueError subclass, not
    an OSError, so the read must catch it explicitly and not propagate."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path)
    p = recoveries._log_path("P")
    p.parent.mkdir(parents=True, exist_ok=True)
    # A lone 0xFF byte is invalid UTF-8 and raises UnicodeDecodeError on read_text().
    p.write_bytes(b'{"entry_id": "e1"}\n\xff\xfe garbage\n')
    assert recoveries.load_recoveries("P") == []


def test_consumed_ids_fails_open_on_non_utf8_ledger(tmp_path, monkeypatch):
    """Pre-ship LOW: a non-UTF-8 consumed ledger must fail OPEN to set(), not block
    the win loop with an uncaught UnicodeDecodeError."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path)
    p = recoveries._consumed_path("P")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"e1\n\xff\xfe\ne2\n")
    assert recoveries.consumed_ids("P") == set()
