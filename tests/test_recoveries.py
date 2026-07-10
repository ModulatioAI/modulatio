"""Tests for the #81 recovery feed — the win-codification source.

A QC recovery (the smart QC rescues a cheap producer by writing the patch it
couldn't) is witnessed here as a RecoveryRecord. The win loop clusters these by a
deterministic, false-merge-resistant signature behind an engine recurrence floor.
"""

from __future__ import annotations

from modulatio import recoveries
import json
from dataclasses import fields
from modulatio.recoveries import RecoveryRecord, change_shape, load_recoveries


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


# ═══ fold: test_recoveries_low_audit.py ═══
# LOW-audit regressions for recoveries.py (#80 no-delta singleton, #81 schema-evo).


# ── #80: a no-delta "recovery" must fail open to a unique singleton ────────────


def test_change_shape_empty_before_empty_after_is_none():
    # Empty→empty taught nothing; must not produce a stable shape that false-merges.
    assert change_shape("", "", "python_code") is None
    assert change_shape("", "", "essay") is None
    assert change_shape("", "", "csv") is None


def test_change_shape_identical_before_after_is_none():
    same = "def f():\n    return 1\n"
    assert change_shape(same, same, "python_code") is None
    assert change_shape("One sentence.", "One sentence.", "essay") is None


def test_no_delta_recoveries_never_cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda code: tmp_path / code)
    # Three identical no-op recoveries with the SAME kind/defect/rationale: before the
    # fix they shared a stable change-shape and clustered at the floor (false merge).
    for _ in range(3):
        recoveries.record_recovery(
            "proj",
            kind="qc_authored",
            artifact_kind="python_code",
            defect_type="mechanical",
            task_id="t",
            defects="d",
            before="",
            after="",
            qc_rationale="same rationale every time here",
        )
    recs = load_recoveries("proj")
    assert len(recs) == 3
    # Each got a unique unclassified:<id> signature → permanent singletons.
    assert all(r.signature.split("|")[-1].startswith("unclassified:") for r in recs)
    assert len({r.signature for r in recs}) == 3
    assert recoveries.cluster_recoveries(recs, floor=3) == []


# ── #81: schema evolution must not discard the historical feed ─────────────────


def test_record_defaults_allow_construction_with_missing_fields():
    # Simulates loading a historical line written before a (hypothetical) new field
    # existed: a record missing keys must construct, not raise.
    rec = RecoveryRecord(entry_id="x")
    assert rec.entry_id == "x"
    assert rec.signature == ""


def test_load_recoveries_survives_unknown_extra_key(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda code: tmp_path / code)
    p = recoveries._log_path("proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    # A line carrying a since-removed/future field. Pre-fix: TypeError → whole feed
    # line dropped. Post-fix: unknown key ignored, record survives.
    payload = {f.name: "v" for f in fields(RecoveryRecord)}
    payload["a_future_field"] = "boom"
    with p.open("a") as f:
        f.write(json.dumps(payload) + "\n")
    recs = load_recoveries("proj")
    assert len(recs) == 1
    assert recs[0].entry_id == "v"


def test_load_recoveries_survives_missing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda code: tmp_path / code)
    p = recoveries._log_path("proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    # A historical line lacking a (newer) field: must load with the field defaulted.
    with p.open("a") as f:
        f.write(json.dumps({"entry_id": "old1", "kind": "qc_authored"}) + "\n")
    recs = load_recoveries("proj")
    assert len(recs) == 1
    assert recs[0].entry_id == "old1"
    assert recs[0].signature == ""  # defaulted, not dropped


# ═══ fold: test_recoveries_r2_audit.py ═══
# R2 full-debug audit regressions for the recovery feed.
#
# Covers two LOW findings:
#   * recoveries.py:361 — unconsumed_recoveries hard cap of 30 starved a genuinely
#     recurring technique below the cluster floor.
#   * recoveries.py:285 — write-time head-truncation flipped a real tail-only fix to an
#     unclassified singleton that never clusters.




# ── R2 #1: cap must not starve a recurring technique below the floor ───────────


def test_unconsumed_defaults_to_full_feed(tmp_path, monkeypatch):
    """No explicit limit → the FULL unconsumed feed (not a 30-row slice)."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    for i in range(40):
        _rec(monkeypatch, tmp_path, "P", task_id=f"T{i}",
             before=f"x{i}", after=f"y{i}")
    assert len(recoveries.unconsumed_recoveries("P")) == 40
    # an explicit positive limit still caps (preview callers)
    assert len(recoveries.unconsumed_recoveries("P", limit=5)) == 5


def test_recurring_technique_not_starved_by_diverse_newer_recoveries(tmp_path, monkeypatch):
    """A genuine 3-member recurring cluster that is OLDER than 30 diverse one-offs must
    still reach the floor. Before the fix, the default 30-row cap dropped the oldest
    matching members so the cluster fell below floor."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    # 3 same-signature recoveries (oldest), then 40 diverse one-offs (newer).
    same_kwargs = dict(
        artifact_kind="python_code",
        before="def f(x):\n    return x\n",
        after="def f(x):\n    if x is None:\n        return 0\n    return x\n",
        qc_rationale="missing null guard on input",
    )
    for i in range(3):
        _rec(monkeypatch, tmp_path, "P", task_id=f"S{i}",
             timestamp=f"2026-06-13T00:00:0{i}+00:00", **same_kwargs)
    for i in range(40):
        # distinct rationale + change → distinct signature each (one-offs)
        _rec(monkeypatch, tmp_path, "P", task_id=f"N{i}",
             timestamp=f"2026-06-13T10:{i:02d}:00+00:00",
             artifact_kind="python_code",
             before=f"a{i}\n", after=f"a{i}\nb{i}\n",
             qc_rationale=f"distinct one off rationale number {i}")

    recs = recoveries.unconsumed_recoveries("P")
    clusters = recoveries.cluster_recoveries(recs, floor=3)
    assert any(len(c) == 3 for c in clusters), (
        "the recurring 3-member technique was starved out of the window"
    )


# ── R2 #2: tail-only fix on a large artifact must still get a real shape ───────


def test_tail_fix_on_large_artifact_clusters(tmp_path, monkeypatch):
    """A real fix in the TAIL of a >MAX_RECOVERY_EXCERPT_CHARS artifact must produce a
    classified change-shape (not an unclassified singleton), so three such recoveries
    cluster. Before the fix the head-truncated before/after were identical → None →
    unclassified:<id> → permanent singleton."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    head = "common line\n" * 400  # well past MAX_RECOVERY_EXCERPT_CHARS (2000 chars)
    assert len(head) > recoveries.MAX_RECOVERY_EXCERPT_CHARS
    before = head + "def f(x):\n    return x\n"
    after = head + "def f(x):\n    if x is None:\n        return 0\n    return x\n"

    recs = [
        _rec(monkeypatch, tmp_path, "P", task_id=f"L{i}",
             artifact_kind="python_code",
             before=before, after=after,
             qc_rationale="missing null guard on input")
        for i in range(3)
    ]
    # none degraded to an unclassified singleton
    for r in recs:
        assert not r.signature.split("|")[-1].startswith("unclassified:"), r.signature
    # all three share one signature → one cluster of 3
    clusters = recoveries.cluster_recoveries(recs, floor=3)
    assert any(len(c) == 3 for c in clusters)


def test_delta_window_strips_common_prefix_and_suffix():
    """The fingerprint window isolates the changed region; identical strings stay a
    no-delta (so change_shape still fails open to a singleton)."""
    b = "AAAA\nMID_OLD\nZZZZ\n"
    a = "AAAA\nMID_NEW\nZZZZ\n"
    bw, aw = recoveries._delta_window(b, a)
    # char-level common prefix ("AAAA\nMID_") and suffix ("\nZZZZ\n") are stripped,
    # leaving only the genuinely-changed residue.
    assert "AAAA" not in bw and "ZZZZ" not in bw
    assert bw == "OLD" and aw == "NEW"
    # truly identical → unchanged, and change_shape returns None (no-delta)
    assert recoveries._delta_window("same", "same") == ("same", "same")
    assert recoveries.change_shape("same", "same", "python_code") is None


def test_head_truncated_tail_fix_would_have_collapsed():
    """Guard the premise: the OLD behaviour (shape from head-truncated excerpts) loses
    the tail delta — proving the fix is load-bearing."""
    head = "x" * (recoveries.MAX_RECOVERY_EXCERPT_CHARS + 50)
    before = head + "old"
    after = head + "new"
    # head-truncated copies are identical → change_shape sees no delta
    bt = before[: recoveries.MAX_RECOVERY_EXCERPT_CHARS]
    at = after[: recoveries.MAX_RECOVERY_EXCERPT_CHARS]
    assert bt == at
    assert recoveries.change_shape(bt, at, "python_code") is None
    # but the delta-windowed full strings DO yield a shape
    bw, aw = recoveries._delta_window(before, after)
    assert recoveries.change_shape(bw, aw, "python_code") is not None
